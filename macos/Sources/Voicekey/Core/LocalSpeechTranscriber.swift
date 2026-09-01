//
//  LocalSpeechTranscriber.swift
//  Apple オンデバイス音声認識（SpeechAnalyzer / SpeechTranscriber, macOS 26）による音声入力
//
//  録音中のチャンクをそのまま解析器へ流し込み、ホットキーを離した時点で確定させる
//  ＝ Deepgram ストリーミングと同じ「離した瞬間に入力」型。ネットワーク往復も API キーも
//  無いので、原理的にいちばん速い経路になる（速度が採用の動機）。
//
//  認識器そのものは字幕と同じ `SpeechRecognizer`（Caption/Speech）を使い回す。
//  SpeechAnalyzer は 1 プロセスに複数インスタンスを持てるため、字幕を出したまま
//  音声入力しても互いに干渉しない（回帰ハーネス --local-stt-test で実測する）。
//

import AVFoundation
import Foundation
import os.log

private let log = Logger(subsystem: "com.voicekey.app", category: "localstt")

/// Apple オンデバイス音声認識による逐次文字起こしセッション（1 録音 = 1 インスタンス）。
/// LiveTranscribing 適合＝AppController から Deepgram / OpenAI ライブと同じ扱いで差し替えられる。
@available(macOS 26.0, *)
final class LocalSpeechTranscriber: LiveTranscribing, @unchecked Sendable {

    /// 現在の全文（確定 + 途中経過）の更新通知。HUD のライブ字幕用
    var onInterim: ((String) -> Void)?

    /// 言語モデルのダウンロード進捗の通知（初回だけ走る。HUD に出して無言の待ちを作らない）
    var onAssetProgress: ((String) -> Void)?

    /// 録音側のフォーマット（AudioRecorder と同じ 16kHz モノラル Float32）
    private static let inputFormat = AVAudioFormat(
        commonFormat: .pcmFormatFloat32, sampleRate: AudioRecorder.sampleRate,
        channels: 1, interleaved: false
    )!

    private let locale: Locale
    private let lock = NSLock()

    private var recognizer: SpeechRecognizer?
    /// 16kHz Float32 → 解析器が要求するフォーマットへの変換器（スレッド安全でないので lock 下でのみ触る）
    private var converter: AudioFormatConverter?
    /// 準備完了前に届いたチャンクの退避（解析器の準備は非同期なので必ず発生する）
    private var pending: [[Float]] = []
    /// 確定済みセグメント（生テキスト。結合してから前後を整える）
    private var finals: [String] = []
    /// 直近の途中経過（次の確定で置き換わる）
    private var interim = ""
    private var cancelled = false

    /// 解析器の準備タスク（finish で完了を待ち、退避チャンクを取りこぼさない）
    private var prepareTask: Task<Void, Never>?
    /// 認識結果の受信タスク（recognizer.finish() でストリームが閉じると自然に終わる）
    private var consumeTask: Task<Void, Never>?

    /// 生成時刻 ≒ 録音開始。確定までの実測を 1 行残すために持つ
    private let createdAt = Date()

    /// - Parameter language: 設定の言語コード（"ja" など。空ならシステムの言語）
    init(language: String) {
        self.locale = Self.recognitionLocale(for: language)
    }

    /// 設定の言語コードを認識ロケールへ写す（純関数・テスト対象）。
    /// 空文字は「自動判定」の意味だが SpeechTranscriber に自動判定は無いので、
    /// システムの言語で認識する（日本語環境なら日本語）。
    static func recognitionLocale(for language: String) -> Locale {
        let code = language.trimmingCharacters(in: .whitespacesAndNewlines)
        return code.isEmpty ? Locale.current : Locale(identifier: code)
    }

    // MARK: - LiveTranscribing

    /// 解析器の準備を始める（準備完了を待たずに返る）。
    ///
    /// 準備（アセット確認・モデルロード）は数百 ms かかるので、待たずに返して
    /// 録音を先に始める。その間のチャンクは `pending` に退避し、準備完了時にまとめて流す
    /// ＝ 1 サンプルも捨てない。
    ///
    /// - Returns: 常に true（開始できたかは finish() の結果で分かる）
    func start() -> Bool {
        ActionLog.shared.write("localstt", "ローカル音声認識 開始 (locale=\(locale.identifier))")
        prepareTask = Task { [weak self] in
            guard let self else { return }
            let recognizer = SpeechRecognizer(locale: self.locale)
            // 初回だけ走る言語モデルのダウンロードを無言の待ちにしない
            recognizer.onAssetProgress = { [weak self] message in self?.onAssetProgress?(message) }
            do {
                let format = try await recognizer.start()
                self.attach(recognizer: recognizer, format: format)
            } catch {
                log.error("ローカル音声認識を開始できません: \(String(describing: error), privacy: .public)")
                ActionLog.shared.write("localstt", "ローカル音声認識 開始失敗: \(String(describing: error))")
            }
        }
        return true
    }

    /// 16kHz モノラル Float32 チャンクを送る（audio スレッドから呼ばれる）
    func send(_ samples: [Float]) {
        guard !samples.isEmpty else { return }
        feed(samples)
    }

    /// 送信を打ち切り、確定テキストを返す（ホットキーを離したときに呼ぶ）。
    ///
    /// 準備タスクの完了を待つのは、退避チャンクを流し切ってから確定させるため
    /// （初回のモデルダウンロード中はここで待つが、進捗を HUD に出しているので無言にはならない）。
    func finish() async -> String {
        await prepareTask?.value
        let session = takeRecognizer()
        // 残りを確定させる（ストリームが閉じるので consumeTask も自然に終わる）
        await session?.finish()
        await consumeTask?.value

        let text = TextNormalize.stripCJKSpaces(currentText())
        let elapsedMs = Int(Date().timeIntervalSince(self.createdAt) * 1000)
        log.notice(
            "ローカル音声認識 確定まで \(elapsedMs, privacy: .public)ms / \(text.count, privacy: .public) 文字"
        )
        ActionLog.shared.write("localstt", "ローカル音声認識 確定 \(text.count) 文字 \(elapsedMs)ms")
        return text
    }

    /// 結果を使わずに破棄する（録音破棄時など）
    func cancel() {
        ActionLog.shared.write("localstt", "ローカル音声認識 終了（結果を破棄）")
        let session = takeRecognizer(markCancelled: true)
        consumeTask?.cancel()
        Task { await session?.finish() }
    }

    // MARK: - 内部処理

    /// 準備できた解析器を受け取り、退避していたチャンクを流す
    private func attach(recognizer: SpeechRecognizer, format: AVAudioFormat) {
        lock.lock()
        if cancelled {
            lock.unlock()
            Task { await recognizer.finish() }
            return
        }
        self.recognizer = recognizer
        self.converter = AudioFormatConverter(outputFormat: format)
        let buffered = pending
        pending = []
        lock.unlock()

        consumeTask = Task { [weak self] in
            for await segment in recognizer.segments { self?.handle(segment) }
        }
        for chunk in buffered { feed(chunk) }
    }

    /// チャンクを解析器のフォーマットへ変換して投入する。
    ///
    /// AVAudioConverter はスレッド安全でなく、audio スレッドと準備タスクの両方から
    /// 呼ばれうるため、「変換 → 投入」をロックの中で完結させる。
    private func feed(_ samples: [Float]) {
        lock.lock()
        defer { lock.unlock() }
        guard !cancelled else { return }
        guard let recognizer, let converter else {
            // まだ準備前 → 退避（attach がフラッシュする）
            pending.append(samples)
            return
        }
        guard let input = Self.makeBuffer(samples),
              let converted = converter.convert(input) else { return }
        recognizer.feed(converted)
    }

    /// [Float]（16kHz モノラル）を AVAudioPCMBuffer へ包む
    private static func makeBuffer(_ samples: [Float]) -> AVAudioPCMBuffer? {
        guard let buffer = AVAudioPCMBuffer(
            pcmFormat: inputFormat, frameCapacity: AVAudioFrameCount(samples.count)
        ), let channel = buffer.floatChannelData?[0] else { return nil }
        buffer.frameLength = AVAudioFrameCount(samples.count)
        samples.withUnsafeBufferPointer { source in
            guard let base = source.baseAddress else { return }
            channel.update(from: base, count: samples.count)
        }
        return buffer
    }

    /// 認識結果を溜め、途中経過を HUD へ流す
    private func handle(_ segment: SpeechRecognizer.Segment) {
        lock.lock()
        if segment.isFinal {
            // 生テキストのまま溜める（英語は語間の空白が意味を持つため trim しない）
            finals.append(segment.text)
            interim = ""
        } else {
            interim = segment.text
        }
        let snapshot = (finals + [interim]).joined()
        lock.unlock()
        onInterim?(TextNormalize.stripCJKSpaces(snapshot.trimmingCharacters(in: .whitespacesAndNewlines)))
    }

    /// 現在の全文（確定 + 途中経過）
    private func currentText() -> String {
        lock.lock(); defer { lock.unlock() }
        return (finals + [interim]).joined().trimmingCharacters(in: .whitespacesAndNewlines)
    }

    /// 解析器の所有権を取り出す（二重 finish を防ぐ）
    private func takeRecognizer(markCancelled: Bool = false) -> SpeechRecognizer? {
        lock.lock(); defer { lock.unlock() }
        if markCancelled {
            cancelled = true
            pending = []
        }
        let session = recognizer
        recognizer = nil
        converter = nil
        return session
    }
}
