/// キャプチャ → 変換 → 音声認識 をつなぐパイプライン
///
/// Phase 1（システム音声キャプチャ）と Phase 2（ストリーミング音声認識）の結線点。
/// Phase 3 で `segments` の先に Claude API 翻訳が入る。
import AVFoundation
import Foundation
import OSLog
import os

/// 音声レベル（デバッグ経路）
@available(macOS 26.0, *)
struct AudioLevel: Sendable {
    /// 直近 1 秒の RMS
    let rms: Float
    /// 直近 1 秒の最大絶対振幅
    let peak: Float
    /// 直近 1 秒に処理したフレーム数（0 ならタップからフレームが来ていない）
    let frames: Int
}

/// システム音声を取り込んで英語テキストへ変換し続けるパイプライン
@available(macOS 26.0, *)
final class CapturePipeline: @unchecked Sendable {

    private let logger = makeCaptionLogger("CapturePipeline")
    private let recognizer: SpeechRecognizer
    private let levelQueue = DispatchQueue(label: "com.voicekey.caption.pipeline.level")

    private var tap: SystemAudioTap?
    private var converter: AudioFormatConverter?
    private var levelTimer: DispatchSourceTimer?
    private var segmentTask: Task<Void, Never>?
    /// 最前面アプリ追従（「最前面のアプリだけ」モードのときだけ作る）
    private var scopeTracker: CaptureScopeTracker?

    /// キャプチャ対象のモード
    private let scopeMode: CaptureScopeMode

    /// いま対象にしているアプリ名（メニュー表示用。すべてモードでは nil）
    var currentTargetName: String? { scopeTracker?.target?.name }

    /// タップを作り直した回数（回帰ハーネスの判定材料。停止すると 0 に戻る）
    var tapRebuildCount: Int { tap?.rebuildCount ?? 0 }

    /// 解析を開始した実時刻
    ///
    /// 認識結果が持つ音声タイムライン（`Segment.range`）は解析開始を 0 とするため、
    /// 「発話が終わってから何秒遅れて字幕が出たか」を出すにはこの基準点が要る。
    private(set) var analysisStartedAt: CFAbsoluteTime = 0

    /// 最初のフレームが届いた時刻（遅延計測の基準点。IO キューが書く）
    private let firstAudioAt = OSAllocatedUnfairLock<CFAbsoluteTime?>(initialState: nil)

    /// 毎秒の音声レベル通知（デバッグ経路）
    var onLevel: (@Sendable (AudioLevel) -> Void)?
    /// 認識結果の通知（確定・途中経過の両方）
    var onSegment: (@Sendable (SpeechRecognizer.Segment) -> Void)?
    /// 対象アプリが変わったときの通知（メニュー更新用）
    var onTargetChanged: (@Sendable (String?) -> Void)?
    /// 対象がまだ決まらず待っている間の通知（アプリ名。決まったら nil）
    var onWaitingForAudio: (@Sendable (String?) -> Void)?
    /// 最初のフレームが届いた時刻の通知（遅延計測の基準点）
    ///
    /// 認識結果の音声タイムラインは「最初に音を渡した時刻」を 0 とする。
    /// パイプライン開始時刻を基準にすると、対象の再生を待っていた時間まで
    /// 遅延に混ざる（実測で 35 秒という実態とかけ離れた値になった）。
    var onFirstAudio: (@Sendable (CFAbsoluteTime) -> Void)?

    /// 直近 1 秒の集計。IO キューが加算し、レベルタイマーが読み出してリセットする。
    private struct LevelAccumulator {
        var sumOfSquares: Double = 0
        var peak: Float = 0
        var frames: Int = 0
    }
    private let accumulator = OSAllocatedUnfairLock(initialState: LevelAccumulator())

    /// - Parameters:
    ///   - locale: 認識対象の言語（既定 en-US）
    ///   - scopeMode: キャプチャ対象のモード（既定は設定・環境変数から解決）
    init(
        locale: Locale = Locale(identifier: "en-US"),
        scopeMode: CaptureScopeMode = CaptionSettings.effectiveCaptureScopeMode
    ) {
        self.recognizer = SpeechRecognizer(locale: locale)
        self.scopeMode = scopeMode
    }

    /// パイプラインを開始する
    ///
    /// 音声認識の準備（アセット確認・モデルロード）を先に済ませてからタップを張る。
    /// 逆順にすると、モデル準備中の音声を捨てることになるため。
    ///
    /// - Throws: 音声認識の準備に失敗した場合
    func start() async throws {
        let analyzerFormat = try await recognizer.start()
        analysisStartedAt = CFAbsoluteTimeGetCurrent()

        let converter = AudioFormatConverter(outputFormat: analyzerFormat)
        self.converter = converter

        segmentTask = Task { [weak self] in
            guard let self else { return }
            for await segment in self.recognizer.segments {
                self.onSegment?(segment)
            }
        }

        // 「最前面のアプリだけ」モードでは、対象が決まるまで何も拾わないタップから始める
        // （裏で鳴っている音楽を一瞬でも拾わないため）。対象は追従側が即座に埋める。
        let initialScope: CaptureScope = scopeMode == .frontmost ? .processes([]) : .allExcludingSelf
        let tap = SystemAudioTap(scope: initialScope) { [weak self] buffer in
            self?.handleCapturedBuffer(buffer)
        }
        self.tap = tap

        if scopeMode == .frontmost {
            let tracker = CaptureScopeTracker(fixedPID: CaptionSettings.fixedCaptureTargetPID)
            tracker.onScopeChanged = { [weak self] scope, target in
                self?.tap?.setScope(scope)
                self?.onTargetChanged?(target?.name)
            }
            tracker.onWaitingForAudio = { [weak self] name in
                self?.onWaitingForAudio?(name)
            }
            self.scopeTracker = tracker
            tracker.start()
        }

        tap.start()

        startLevelTimer()
        logger.notice("パイプラインを開始しました 対象モード=\(self.scopeMode.rawValue, privacy: .public)")
    }

    /// パイプラインを停止する
    func stop() async {
        stopLevelTimer()
        scopeTracker?.stop()
        scopeTracker = nil
        tap?.stop()
        tap = nil
        await recognizer.finish()
        segmentTask?.cancel()
        segmentTask = nil
        converter = nil
        logger.notice("パイプラインを停止しました")
    }

    // MARK: - 内部処理

    /// タップから届いた PCM を変換し、レベル集計と音声認識へ回す
    ///
    /// SystemAudioTap の IO キュー（直列）上で呼ばれる。AVAudioConverter は
    /// スレッド安全でないが、この経路 1 本からしか触らないため問題ない。
    private func handleCapturedBuffer(_ buffer: AVAudioPCMBuffer) {
        guard let converted = converter?.convert(buffer) else { return }

        // 最初の 1 回だけ、遅延計測の基準点を配る
        let isFirst = firstAudioAt.withLock { stored -> Bool in
            guard stored == nil else { return false }
            stored = CFAbsoluteTimeGetCurrent()
            return true
        }
        if isFirst, let origin = firstAudioAt.withLock({ $0 }) {
            logger.notice("最初の音声フレームが届きました")
            onFirstAudio?(origin)
        }

        let measurement = measureAudioLevel(converted)
        accumulator.withLock { state in
            state.sumOfSquares += measurement.sumOfSquares
            state.peak = max(state.peak, measurement.peak)
            state.frames += measurement.frames
        }

        recognizer.feed(converted)
    }

    /// 毎秒レベルを通知するタイマーを起動する
    private func startLevelTimer() {
        let timer = DispatchSource.makeTimerSource(queue: levelQueue)
        timer.schedule(deadline: .now() + 1.0, repeating: 1.0)
        timer.setEventHandler { [weak self] in self?.emitLevel() }
        timer.resume()
        levelTimer = timer
    }

    /// レベルタイマーを止める
    private func stopLevelTimer() {
        levelTimer?.cancel()
        levelTimer = nil
    }

    /// 集計値を RMS に均して通知し、カウンタをリセットする
    private func emitLevel() {
        let snapshot = accumulator.withLock { state -> LevelAccumulator in
            let current = state
            state = LevelAccumulator()
            return current
        }
        let rms = snapshot.frames > 0 ? Float((snapshot.sumOfSquares / Double(snapshot.frames)).squareRoot()) : 0
        onLevel?(AudioLevel(rms: rms, peak: snapshot.peak, frames: snapshot.frames))
    }
}
