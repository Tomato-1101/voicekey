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
    private let levelQueue = DispatchQueue(label: "com.voicekey.caption.pipeline.level")

    private var tap: SystemAudioTap?
    private var levelTimer: DispatchSourceTimer?
    /// 最前面アプリ追従（「最前面のアプリだけ」モードのときだけ作る）
    private var scopeTracker: CaptureScopeTracker?

    /// 認識対象の言語（リサイクルで認識器を作り直すときにも使う）
    private let locale: Locale
    /// キャプチャ対象のモード
    private let scopeMode: CaptureScopeMode

    /// 認識セッション 1 つ分（認識器・その入力形式の変換器・結果の受け口）
    ///
    /// SpeechAnalyzer は解析中に内部の計測配列を無制限に伸ばし続け（macOS 26.6 実測で
    /// 1 日あたり約 100MB）、アプリ側からはセッションを作り直す以外に解放手段が無い。
    /// そこで「まるごと捨てて作り直す単位」を 1 つの値にまとめておく。
    ///
    /// 変換器はスレッド安全でないが、この値は必ず `session` ロックの中でしか触らないので
    /// `@unchecked Sendable` としている。
    private struct Session: @unchecked Sendable {
        /// 端末内の認識器（Soniox のように外部へ音声を渡す構成では nil）
        let recognizer: SpeechRecognizer?
        let converter: AudioFormatConverter
        let segmentTask: Task<Void, Never>?
        /// このセッションを用意した時刻（リサイクル判定の基準）
        let startedAt: CFAbsoluteTime
    }

    /// 現在の認識セッション（停止中は nil）
    ///
    /// タップの IO キューとリサイクル処理の両方から触るためロックで守る。
    private let session = OSAllocatedUnfairLock<Session?>(initialState: nil)

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
    /// 変換済みの音声（16kHz モノラル Float32）の通知
    ///
    /// 端末内の認識器を使わない構成（`usesOnDeviceRecognizer: false`）でだけ流れる。
    /// Soniox のように音声を外部の認識へ渡す経路のための口。
    var onAudio: (@Sendable (AVAudioPCMBuffer) -> Void)?

    /// 端末内の認識（SpeechAnalyzer）を使うか
    private let usesOnDeviceRecognizer: Bool
    /// 認識の優先度（nil なら OS の既定）
    private let recognitionPriority: TaskPriority?
    /// 発話の切れ目で確定を前倒しするか（字幕だけが true。Meet ボット等は従来どおり）
    private let finalizesOnPause: Bool
    /// 切れ目の判定（IO キューとタイマーの両方から触るのでロックで守る）
    private let pauseTrigger = OSAllocatedUnfairLock(initialState: PauseFinalizeTrigger())
    /// フレーム途絶を見るためのタイマー（切れ目の前倒しを使うときだけ）
    private var pauseTimer: DispatchSourceTimer?
    /// 途絶を見る間隔（秒）。切れ目 0.3 秒の判定を 0.1 秒刻みで拾う
    private static let pauseTickInterval: Double = 0.1
    /// 外部の認識へ渡すときの音声形式（Soniox の pcm_s16le 16kHz モノラルへそのまま詰められる）
    private static let externalFormat = AVAudioFormat(
        commonFormat: .pcmFormatFloat32, sampleRate: 16000, channels: 1, interleaved: false
    )!

    /// 直近 1 秒の集計。IO キューが加算し、レベルタイマーが読み出してリセットする。
    private struct LevelAccumulator {
        var sumOfSquares: Double = 0
        var peak: Float = 0
        var frames: Int = 0
    }
    private let accumulator = OSAllocatedUnfairLock(initialState: LevelAccumulator())

    // MARK: - 認識セッションのリサイクル

    /// 無音とみなす RMS のしきい値（真の無音は 1e-6 未満、音が乗ると 0.002 以上になる）
    private static let recycleSilenceRMS: Float = 0.0005
    /// リサイクル前に無音が続いている必要のある秒数（字幕が出ていない瞬間を狙うため）
    private static let recycleSilenceSeconds = 8
    /// 無音時にリサイクルしてよくなるセッション経過（30 分 ≒ 計測配列 3.7MB）
    private static let recycleIdleAfter: Double = 30 * 60
    /// 音が続いていても強制的に作り直す上限（4 時間 ≒ 計測配列 30MB）
    private static let recycleForceAfter: Double = 4 * 60 * 60

    /// 連続して無音だった秒数（レベルタイマーのキューだけが触る）
    private var silentSeconds = 0
    /// リサイクル実行中か（作り直しは数百 ms かかるので多重に走らせない）
    private let isRecycling = OSAllocatedUnfairLock(initialState: false)

    /// - Parameters:
    ///   - locale: 認識対象の言語（既定 en-US）
    ///   - scopeMode: キャプチャ対象のモード（既定は設定・環境変数から解決）
    ///   - fixedPID: 対象を固定したいプロセス（Meet ボットが起動した Chrome など）。
    ///     指定すると最前面の追従をやめ、そのアプリ（と責任 PID が一致する子プロセス）の音だけを拾う。
    ///   - processTreeOnly: 固定 PID の子孫プロセスだけを拾う（同じバンドル ID の別プロセスを拾わない）。
    ///     Meet ボットが普段使いの Chrome の音を拾わないために使う。
    ///   - recognitionPriority: 認識の優先度（字幕は `.userInitiated`。nil なら既定）
    ///   - finalizesOnPause: 発話の切れ目で確定を前倒しするか
    ///   - usesOnDeviceRecognizer: false なら端末内の認識をせず、変換した音声を `onAudio` へ流す
    init(
        locale: Locale = Locale(identifier: "en-US"),
        scopeMode: CaptureScopeMode = CaptionSettings.effectiveCaptureScopeMode,
        fixedPID: pid_t? = nil,
        processTreeOnly: Bool = false,
        recognitionPriority: TaskPriority? = nil,
        finalizesOnPause: Bool = false,
        usesOnDeviceRecognizer: Bool = true
    ) {
        self.locale = locale
        self.recognitionPriority = recognitionPriority
        self.finalizesOnPause = finalizesOnPause && usesOnDeviceRecognizer
        self.usesOnDeviceRecognizer = usesOnDeviceRecognizer
        // PID を固定するときは、対象を追いかける必要があるので frontmost 側の経路に載せる
        // （CaptureScopeTracker が固定 PID を見て、そのプロセスの音声だけを拾う）。
        self.scopeMode = fixedPID == nil ? scopeMode : .frontmost
        self.fixedPID = fixedPID
        self.processTreeOnly = processTreeOnly
    }

    /// 対象を固定するプロセス（nil なら環境変数 / 最前面の追従に任せる）
    private let fixedPID: pid_t?
    /// 固定 PID の子孫だけを拾うか
    private let processTreeOnly: Bool

    /// パイプラインを開始する
    ///
    /// 音声認識の準備（アセット確認・モデルロード）を先に済ませてからタップを張る。
    /// 逆順にすると、モデル準備中の音声を捨てることになるため。
    ///
    /// - Throws: 音声認識の準備に失敗した場合
    func start() async throws {
        let initial = try await makeSession()
        analysisStartedAt = initial.startedAt
        session.withLock { $0 = initial }

        // 「最前面のアプリだけ」モードでは、対象が決まるまで何も拾わないタップから始める
        // （裏で鳴っている音楽を一瞬でも拾わないため）。対象は追従側が即座に埋める。
        let initialScope: CaptureScope = scopeMode == .frontmost ? .processes([]) : .allExcludingSelf
        let tap = SystemAudioTap(scope: initialScope) { [weak self] buffer in
            self?.handleCapturedBuffer(buffer)
        }
        self.tap = tap

        if scopeMode == .frontmost {
            let tracker = CaptureScopeTracker(
                fixedPID: fixedPID ?? CaptionSettings.fixedCaptureTargetPID,
                processTreeOnly: fixedPID != nil && processTreeOnly
            )
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
        if finalizesOnPause { startPauseTimer() }
        logger.notice("パイプラインを開始しました 対象モード=\(self.scopeMode.rawValue, privacy: .public)")
    }

    /// パイプラインを停止する
    func stop() async {
        stopLevelTimer()
        stopPauseTimer()
        scopeTracker?.stop()
        scopeTracker = nil
        tap?.stop()
        tap = nil
        let previous = session.withLock { current -> Session? in
            let previous = current
            current = nil
            return previous
        }
        await previous?.recognizer?.finish()
        // 打ち切らずに最後まで読み切る（finish() が結果ストリームを閉じるので必ず終わる）。
        // 以前は cancel していたため、停止の瞬間に確定した最後の 1 文が後段へ届かずに落ちていた。
        await previous?.segmentTask?.value
        logger.notice("パイプラインを停止しました")
    }

    // MARK: - 内部処理

    /// 新しい認識セッションを 1 つ用意する（解析開始まで済ませる）
    ///
    /// - Returns: 差し替えにそのまま使えるセッション
    /// - Throws: 音声認識の準備に失敗した場合
    private func makeSession() async throws -> Session {
        guard usesOnDeviceRecognizer else {
            // 外部の認識へ渡す構成では SpeechAnalyzer を起動しない（モデルのロードも CPU も使わない）
            return Session(
                recognizer: nil,
                converter: AudioFormatConverter(outputFormat: Self.externalFormat),
                segmentTask: nil,
                startedAt: CFAbsoluteTimeGetCurrent()
            )
        }
        let recognizer = SpeechRecognizer(locale: locale, analyzerPriority: recognitionPriority)
        let analyzerFormat = try await recognizer.start()
        // 結果ストリームは認識器ごとに別物なので、受け口もセッションと寿命を揃える
        let segmentTask = Task(priority: recognitionPriority) { [weak self] in
            for await segment in recognizer.segments {
                self?.onSegment?(segment)
            }
        }
        return Session(
            recognizer: recognizer,
            converter: AudioFormatConverter(outputFormat: analyzerFormat),
            segmentTask: segmentTask,
            startedAt: CFAbsoluteTimeGetCurrent()
        )
    }

    /// タップから届いた PCM を変換し、レベル集計と音声認識へ回す
    ///
    /// SystemAudioTap の IO キュー（直列）上で呼ばれる。AVAudioConverter はスレッド安全でなく、
    /// リサイクルで認識器ごと差し替わるため、「変換 → 集計 → 投入」をロックの中で完結させる。
    private func handleCapturedBuffer(_ buffer: AVAudioPCMBuffer) {
        let didFeed = session.withLock { current -> Bool in
            guard let current, let converted = current.converter.convert(buffer) else { return false }
            let measurement = measureAudioLevel(converted)
            accumulator.withLock { state in
                state.sumOfSquares += measurement.sumOfSquares
                state.peak = max(state.peak, measurement.peak)
                state.frames += measurement.frames
            }
            guard let recognizer = current.recognizer else {
                onAudio?(converted)
                return true
            }
            recognizer.feed(converted)
            // 計算済みの RMS で発話の切れ目を見て、確定を前倒しする（HAL には触れない）
            if finalizesOnPause, measurement.frames > 0 {
                let rms = Float((measurement.sumOfSquares / Double(measurement.frames)).squareRoot())
                let duration = Double(converted.frameLength) / converted.format.sampleRate
                let now = CFAbsoluteTimeGetCurrent()
                if pauseTrigger.withLock({ $0.observeAudio(rms: rms, duration: duration, now: now) }) {
                    recognizer.finalizeNow()
                }
            }
            return true
        }
        guard didFeed else { return }

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

    /// フレーム途絶を見るタイマーを起動する
    ///
    /// タップは対象が黙るとフレームを配らないので、IO 側の判定だけでは切れ目を拾えない。
    /// 読むのはロック内の時刻だけで、HAL には触れない（軽い・上限を気にする再試行も無い）。
    private func startPauseTimer() {
        let timer = DispatchSource.makeTimerSource(queue: levelQueue)
        timer.schedule(deadline: .now() + Self.pauseTickInterval, repeating: Self.pauseTickInterval)
        timer.setEventHandler { [weak self] in self?.checkPause() }
        timer.resume()
        pauseTimer = timer
    }

    /// 途絶タイマーを止める
    private func stopPauseTimer() {
        pauseTimer?.cancel()
        pauseTimer = nil
    }

    /// 最後のフレームから切れ目ぶん経っていれば確定を頼む
    private func checkPause() {
        let now = CFAbsoluteTimeGetCurrent()
        guard pauseTrigger.withLock({ $0.observeTick(now: now) }) else { return }
        session.withLock { $0?.recognizer }?.finalizeNow()
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
        checkRecycle(rms: rms)
    }

    /// 認識セッションを作り直す頃合いかを判定する（レベルタイマーから毎秒呼ばれる）
    ///
    /// 無音が続いているときを狙うのは、字幕が出ていない瞬間に差し替えれば
    /// 体感の途切れが出ないため。音が鳴りっぱなしの環境でも上限で必ず回収する。
    ///
    /// - Parameter rms: 直近 1 秒の RMS
    private func checkRecycle(rms: Float) {
        silentSeconds = rms < Self.recycleSilenceRMS ? silentSeconds + 1 : 0

        // 端末内の認識器を持たない構成（Soniox）は太る物が無いので作り直さない
        guard usesOnDeviceRecognizer,
              let startedAt = session.withLock({ $0?.startedAt }), startedAt > 0 else { return }
        let elapsed = CFAbsoluteTimeGetCurrent() - startedAt
        let thresholds = Self.recycleThresholds

        let reason: String
        if elapsed >= thresholds.forceAfter {
            reason = "上限"
        } else if elapsed >= thresholds.idleAfter, silentSeconds >= Self.recycleSilenceSeconds {
            reason = "無音"
        } else {
            return
        }

        let started = isRecycling.withLock { flag -> Bool in
            guard !flag else { return false }
            flag = true
            return true
        }
        guard started else { return }
        Task { [weak self] in await self?.recycle(reason: reason, elapsed: elapsed) }
    }

    /// リサイクル判定に使う秒数（ハーネスは環境変数で短縮できる）
    private static var recycleThresholds: (idleAfter: Double, forceAfter: Double) {
        guard let seconds = CaptionSettings.recycleTestSeconds else {
            return (recycleIdleAfter, recycleForceAfter)
        }
        // 本番と同じ比率（無音 : 強制 = 1 : 8）のまま縮める
        return (seconds, seconds * 8)
    }

    /// 認識セッションを作り直してメモリを回収する
    ///
    /// タップ（SystemAudioTap）は張り替えない。TCC の許可と対象アプリのロックを保ったまま、
    /// 際限なく太る SpeechAnalyzer 側だけを捨てて入れ替える。
    ///
    /// - Parameters:
    ///   - reason: ログに残す発動理由
    ///   - elapsed: 旧セッションの経過秒数
    private func recycle(reason: String, elapsed: Double) async {
        defer { isRecycling.withLock { $0 = false } }
        logger.notice(
            "認識セッションを再作成（メモリ回収） 理由=\(reason, privacy: .public) 経過=\(Int(elapsed), privacy: .public)秒"
        )

        let fresh: Session
        do {
            fresh = try await makeSession()
        } catch {
            logger.error(
                "認識セッションの再作成に失敗（現行セッションを継続）: \(String(describing: error), privacy: .public)"
            )
            return
        }

        // 新しい解析器では音声タイムラインが 0 から振り直されるため、遅延計測の基準も入れ替える
        firstAudioAt.withLock { $0 = nil }
        analysisStartedAt = CFAbsoluteTimeGetCurrent()

        let previous = session.withLock { current -> Session? in
            // stop() と競合して既に畳まれていたら、作ったばかりのセッションは使わない
            guard let previous = current else { return nil }
            current = fresh
            return previous
        }
        guard let previous else {
            await fresh.recognizer?.finish()
            fresh.segmentTask?.cancel()
            return
        }

        // 旧セッションには最後の確定を吐き切らせてから畳む（作り直しで 1 文落とさないため）。
        // 結果の受け口も打ち切らずに読み切らせる（cancel すると吐き切った最後の 1 文が落ちる）。
        await previous.recognizer?.finish()
        await previous.segmentTask?.value
        logger.notice("認識セッションの再作成が完了しました")
    }
}
