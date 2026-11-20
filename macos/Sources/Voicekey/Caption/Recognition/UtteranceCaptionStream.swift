//
//  UtteranceCaptionStream.swift
//  発話ごとに文字起こしへ回すストリーム（録音後に送る方式と、発話ごとに繋ぐライブ方式）
//
//  Soniox のように繋ぎっぱなしで流すのではなく、`UtteranceSegmenter` が切った発話 1 つを
//  認識 1 回ぶんとして扱う。黙っている間は何も送らず、接続も持たない。
//

import AVFoundation
import Foundation

/// 発話 1 つの認識結果
enum UtteranceOutcome: Equatable {
    /// 文字起こしできた（空なら声が言葉として取れなかっただけ。失敗ではない）
    case text(String)
    /// 失敗した。`isFatal` は続けても直らない失敗（キーが無い・無効・レート制限）
    case failed(reason: String, isFatal: Bool)
}

/// 発話 1 つを文字起こしする係（発話のたびに作る）
///
/// `begin` / `feed` / `cancel` はストリームの直列キューから順に呼ぶ。`finish` は発話が終わった後に 1 回だけ呼ぶ。
protocol UtteranceRecognizing: AnyObject {
    /// 途中経過（いまの発話の全文）。出せない方式は呼ばない
    var onInterim: ((String) -> Void)? { get set }
    /// 始める。始められなければ false
    func begin() -> Bool
    /// 音声を足す（16kHz モノラル Float32）
    func feed(_ samples: [Float])
    /// 終わらせて結果を受け取る
    func finish() async -> UtteranceOutcome
    /// 結果を使わずに捨てる
    func cancel()
}

/// 録音後に送る方式（OpenAI / ElevenLabs / Microsoft MAI / Groq）
final class RestUtteranceRecognizer: UtteranceRecognizing, @unchecked Sendable {

    var onInterim: ((String) -> Void)?
    private let providerName: String
    private let transcribe: @Sendable ([Float]) async throws -> String
    /// 発話の音声（ストリームの直列キューからだけ触る。finish はキューを抜けた後に読む）
    private var samples: [Float] = []

    /// - Parameters:
    ///   - providerName: 失敗の理由に出す提供元の名前
    ///   - transcribe: 実際の送信（テストでは差し替える）
    init(providerName: String, transcribe: @escaping @Sendable ([Float]) async throws -> String) {
        self.providerName = providerName
        self.transcribe = transcribe
    }

    func begin() -> Bool { true }

    func feed(_ samples: [Float]) {
        self.samples.append(contentsOf: samples)
    }

    func finish() async -> UtteranceOutcome {
        let audio = samples
        samples = []
        do {
            return .text(try await transcribe(audio).trimmingCharacters(in: .whitespacesAndNewlines))
        } catch let error as TranscriptionError {
            // キーが無い・無効とレート制限は、次の発話を送っても同じなのですぐ Apple へ切り替える
            if error.needsApiKey {
                return .failed(reason: "\(providerName) の API キーが未設定か無効", isFatal: true)
            }
            if error.isRateLimited {
                return .failed(reason: "\(providerName) のレート制限", isFatal: true)
            }
            return .failed(reason: "\(providerName) の文字起こしに失敗", isFatal: false)
        } catch {
            return .failed(reason: "\(providerName) の文字起こしに失敗", isFatal: false)
        }
    }

    func cancel() {
        samples = []
    }
}

/// 発話ごとに繋ぐライブ方式（OpenAI ライブ）
///
/// 音声入力の `OpenAILiveTranscriber` をそのまま使う（キーを押している間＝発話 1 つ、とみなす）。
/// 送受信の形を二重に持たないため、字幕専用の WebSocket は書かない。
final class LiveUtteranceRecognizer: UtteranceRecognizing, @unchecked Sendable {

    var onInterim: ((String) -> Void)? {
        didSet { session.onInterim = onInterim }
    }
    private let session: OpenAILiveTranscriber

    init(session: OpenAILiveTranscriber) {
        self.session = session
    }

    func begin() -> Bool { session.start() }

    func feed(_ samples: [Float]) { session.send(samples) }

    func finish() async -> UtteranceOutcome {
        let text = await session.finish()
        // 空で、かつ確定まで行かずに終わった（エラー・切断・時間切れ）ときだけ失敗に数える。
        // 文字が取れていれば途中で切れていても使う（取れた分を捨てない）
        if text.isEmpty, let reason = session.finishReason, reason != "completed" {
            return .failed(reason: "OpenAI ライブに接続できません", isFatal: false)
        }
        return .text(text)
    }

    func cancel() { session.cancel() }
}

/// 発話ごとに文字起こしへ回すストリーム
@available(macOS 26.0, *)
final class UtteranceCaptionStream: CaptionSpeechStream, @unchecked Sendable {

    /// フレームが途絶えたことを見る間隔（秒）
    private static let tickInterval: TimeInterval = 0.2
    /// 停止時に、送信中の発話の結果を待つ上限（秒）
    static let finishWaitLimit: TimeInterval = 2.5

    private let logger = makeCaptionLogger("UtteranceCaption")
    private let providerName: String
    private let canStart: @Sendable () -> Bool
    private let makeRecognizer: @Sendable () -> UtteranceRecognizing
    private let queue = DispatchQueue(label: "com.voicekey.caption.utterance")

    // 以下はすべて queue 上で触る
    private var segmenter = UtteranceSegmenter()
    private var failures = UtteranceFailurePolicy()
    private var current: UtteranceRecognizing?
    /// 発話の結果待ちを順番につなぐ（確定文の順序を保つ。前の発話が返るまで次の結果を出さない）
    private var tail: Task<Void, Never>?
    private var tickTimer: DispatchSourceTimer?
    private var isStopped = false
    private var gaveUp = false

    var onLive: (@Sendable (String, String) -> Void)?
    var onConfirmed: (@Sendable (String, String) -> Void)?
    var onGiveUp: (@Sendable (String) -> Void)?
    var onLinkStateChange: (@Sendable (SonioxCaptionLinkState) -> Void)?

    /// - Parameters:
    ///   - providerName: 理由・ログに出す提供元の名前
    ///   - canStart: 開始できるか（キーがあるか）
    ///   - makeRecognizer: 発話 1 つぶんの認識係を作る
    init(
        providerName: String,
        canStart: @escaping @Sendable () -> Bool,
        makeRecognizer: @escaping @Sendable () -> UtteranceRecognizing
    ) {
        self.providerName = providerName
        self.canStart = canStart
        self.makeRecognizer = makeRecognizer
    }

    func start() -> Bool {
        guard canStart() else { return false }
        queue.async { [self] in
            let timer = DispatchSource.makeTimerSource(queue: queue)
            timer.schedule(deadline: .now() + Self.tickInterval, repeating: Self.tickInterval)
            timer.setEventHandler { [weak self] in
                guard let self, let event = self.segmenter.tick(now: CFAbsoluteTimeGetCurrent()) else { return }
                self.handleLocked([event])
            }
            timer.resume()
            tickTimer = timer
        }
        return true
    }

    func send(_ buffer: AVAudioPCMBuffer) {
        guard let channel = buffer.floatChannelData?[0], buffer.frameLength > 0 else { return }
        let samples = Array(UnsafeBufferPointer(start: channel, count: Int(buffer.frameLength)))
        queue.async { [self] in
            guard !isStopped, !gaveUp else { return }
            handleLocked(segmenter.append(samples, now: CFAbsoluteTimeGetCurrent()))
        }
    }

    func stop() async {
        let pending: Task<Void, Never>? = queue.sync { [self] in
            guard !isStopped else { return nil }
            tickTimer?.cancel()
            tickTimer = nil
            // 停止の瞬間に喋っていた最後の発話も文字起こしへ回す（議事録から落とさない）
            if let event = segmenter.flush() { handleLocked([event]) }
            return tail
        }
        if let pending {
            _ = await StopWaiter.wait(limit: Self.finishWaitLimit) { await pending.value }
        }
        queue.sync { [self] in
            isStopped = true
            current?.cancel()
            current = nil
        }
    }

    /// 区切りの出来事を処理する（queue 上）
    private func handleLocked(_ events: [UtteranceSegmenter.Event]) {
        for event in events {
            guard !gaveUp else { return }
            switch event {
            case .begin:
                let recognizer = makeRecognizer()
                recognizer.onInterim = { [weak self] text in
                    guard !text.isEmpty else { return }
                    self?.onLive?(text, "")
                }
                guard recognizer.begin() else {
                    giveUpLocked("\(providerName) を開始できません")
                    return
                }
                current = recognizer
            case let .audio(samples):
                current?.feed(samples)
            case let .end(isSpeech):
                guard let recognizer = current else { continue }
                current = nil
                guard isSpeech else {
                    recognizer.cancel()
                    continue
                }
                // 送信はすぐ始め（前の発話の返事を待たせない）、結果を出す順番だけ前の発話の後ろに並べる
                let result = Task { await recognizer.finish() }
                let previous = tail
                tail = Task { [weak self] in
                    await previous?.value
                    let outcome = await result.value
                    await withCheckedContinuation { (continuation: CheckedContinuation<Void, Never>) in
                        guard let self else {
                            continuation.resume()
                            return
                        }
                        self.queue.async {
                            self.applyLocked(outcome)
                            continuation.resume()
                        }
                    }
                }
            }
        }
    }

    /// 発話 1 つの結果を反映する（queue 上）
    private func applyLocked(_ outcome: UtteranceOutcome) {
        guard !isStopped, !gaveUp else { return }
        switch outcome {
        case let .text(text):
            failures.noteSuccess()
            if !text.isEmpty { onConfirmed?(text, "") }
        case let .failed(reason, isFatal):
            logger.error("発話の文字起こしに失敗: \(reason, privacy: .public)")
            if failures.noteFailure(isFatal: isFatal) { giveUpLocked(reason) }
        }
    }

    /// 諦めて呼び出し側へ知らせる（1 回だけ・queue 上）
    private func giveUpLocked(_ reason: String) {
        guard !gaveUp else { return }
        gaveUp = true
        current?.cancel()
        current = nil
        onGiveUp?(reason)
    }
}
