/// `--pipeline-test <秒数>` モード（E2E ハーネス用）
///
/// GUI を出さずにキャプチャ＋認識だけを指定秒数走らせ、機械判定できる形式で
/// 標準出力（および `--log-file` 指定時はファイル）へ結果を流して終了する。
/// `open` 経由で起動すると標準出力が捨てられるため、ファイル出力を併設している。
import AVFoundation
import Foundation
import os

/// 行単位の出力先（標準出力＋任意のファイル）
///
/// 複数のキューからコールバック経由で書かれるためロックで直列化する。
@available(macOS 26.0, *)
final class CaptionTestLogWriter: @unchecked Sendable {
    private let lock = OSAllocatedUnfairLock()
    private let fileHandle: FileHandle?

    /// - Parameter path: 追記するログファイルのパス（nil なら標準出力のみ）
    init(path: String?) {
        guard let path else {
            self.fileHandle = nil
            return
        }
        FileManager.default.createFile(atPath: path, contents: nil)
        self.fileHandle = FileHandle(forWritingAtPath: path)
    }

    /// 1 行出力する（改行は自動付与）
    func write(_ line: String) {
        lock.lock()
        defer { lock.unlock() }
        print(line)
        // open 経由起動では標準出力が消えるため、ファイル側を即時フラッシュして
        // テストスクリプトが tail で追えるようにする
        fflush(stdout)
        if let data = (line + "\n").data(using: .utf8) {
            try? fileHandle?.write(contentsOf: data)
        }
    }

    /// ファイルを閉じる
    func close() {
        lock.lock()
        defer { lock.unlock() }
        try? fileHandle?.close()
    }
}

/// パイプラインテストの実行本体
@available(macOS 26.0, *)
enum CaptionPipelineTestRunner {

    /// 指定秒数だけキャプチャ＋認識を走らせる
    ///
    /// - Parameters:
    ///   - seconds: 実行時間（秒）
    ///   - logFilePath: ログの追加出力先（nil なら標準出力のみ）
    /// - Returns: プロセスの終了コード（0=正常終了 / 1=エラー）
    static func run(seconds: Double, logFilePath: String?) async -> Int32 {
        let writer = CaptionTestLogWriter(path: logFilePath)
        defer { writer.close() }

        writer.write("[INFO] voicekey caption-pipeline-test 開始 duration=\(seconds)s pid=\(getpid())")
        writer.write("[INFO] bundleID=\(Bundle.main.bundleIdentifier ?? "(なし)")")

        // 収集する統計（複数キューから触るためロックで保護する）
        let stats = OSAllocatedUnfairLock(initialState: Statistics())
        let pipeline = CapturePipeline()

        pipeline.onLevel = { level in
            stats.withLock { state in
                state.maxRMS = max(state.maxRMS, level.rms)
                state.totalFrames += level.frames
                state.levelSamples.append(level.rms)
            }
            writer.write(
                String(
                    format: "[RMS] rms=%.6f peak=%.6f frames=%d",
                    level.rms, level.peak, level.frames
                )
            )
        }

        // 認識だけでなく翻訳まで通しで確かめられるよう、本番と同じ調停役を挟む。
        // 翻訳器は 1 個だけ作って使い回す（Apple 側はセッションを保持するため）
        let translator = makeTranslator()
        // Apple 翻訳は導入済みのときだけ準備する。ここでダウンロード承認 UI を出すと
        // ユーザー操作待ちで計測が止まるため、未導入なら翻訳せずに進む。
        if let apple = translator as? AppleTranslator {
            let ready = await apple.prepareIfInstalled()
            writer.write("[INFO] Apple 翻訳の準備: \(ready ? "済" : "未導入のためスキップ")")
        }

        let latency = CaptionLatencyLog()
        // 表示順の採番。ハーネスでは単調増加していれば足りる。
        let sequence = OSAllocatedUnfairLock(initialState: 0)
        let coordinator = TranslationCoordinator(
            translatorProvider: { translator },
            sequenceProvider: { sequence.withLock { value in value += 1; return value } }
        )
        coordinator.onTranslated = { source, japanese, _ in
            latency.noteTranslationShown()
            stats.withLock {
                $0.translatedTexts.append(japanese)
                $0.translatedSources.append(source)
            }
            writer.write("[TRANSLATED] \(japanese)")
            writer.write("[TRANSLATED-SOURCE] \(source)")
        }
        coordinator.onFailed = { _, message in
            stats.withLock { $0.translationFailures += 1 }
            writer.write("[TRANSLATE-FAIL] \(message)")
        }
        // ストリーミング対応エンジン（Groq）で、訳が届く途中の描画をログから確かめられるようにする。
        // 差分は 1 文で数十回来るため、ログは 5 回に 1 回だけ書く（全部書くと出力が詰まる）。
        coordinator.onStreamingDelta = { _, japanese, _ in
            let count = stats.withLock { state -> Int in
                state.streamingDeltaCount += 1
                return state.streamingDeltaCount
            }
            guard count % 5 == 1 else { return }
            writer.write("[STREAM] \(japanese)")
        }

        // 本番と同じ条件で部分訳も回す（Gemini では無効になる）
        let partialDriver = PartialTranslationDriver(
            translatorProvider: { partialTranslator(translator) },
            sequenceProvider: { sequence.withLock { value in value += 1; return value } }
        )
        partialDriver.onPartial = { _, japanese, _ in
            latency.notePartialShown()
            stats.withLock { $0.partialCount += 1 }
            writer.write("[PARTIAL] \(japanese)")
        }

        pipeline.onSegment = { segment in
            if segment.isFinal {
                latency.noteFinal(audioEndSeconds: segment.range.end.seconds)
                stats.withLock { $0.finalTexts.append(segment.text) }
                writer.write("[FINAL] \(segment.text)")
                coordinator.submit(segment.text)
            } else {
                latency.noteVolatile(audioEndSeconds: segment.range.end.seconds)
                partialDriver.submit(segment.text)
                writer.write("[VOLATILE] \(segment.text)")
            }
        }

        // 遅延の基準点は「最初のフレームが届いた時刻」。開始から音が鳴り出すまでの待ち時間を
        // 遅延に混ぜないため（混ぜると実測 35 秒という実態とかけ離れた値になる）。
        // start() より前に繋ぐ（開始直後にフレームが来ても取りこぼさない）。
        pipeline.onFirstAudio = { origin in latency.setAudioOrigin(origin) }

        do {
            try await pipeline.start()
        } catch {
            writer.write("[ERROR] パイプラインの開始に失敗: \(String(describing: error))")
            writer.write("[DONE] status=error")
            return 1
        }

        // ここまで来ればタップ生成と音声認識の準備が通っている。
        // テストスクリプトはこの行を見てから英語音声を再生する。
        writer.write("[READY] キャプチャと音声認識を開始しました")

        try? await Task.sleep(for: .seconds(seconds))

        await pipeline.stop()
        // 最後の断片も訳させ、非同期の翻訳が届くのを少しだけ待つ
        coordinator.flush()
        try? await Task.sleep(for: .seconds(3))

        let snapshot = stats.withLock { $0 }
        // 冒頭 2 秒は無音のはずなので、それを基準線として「音が乗ったか」を判定できるようにする
        let baseline = snapshot.levelSamples.prefix(2).max() ?? 0
        writer.write(
            String(
                format: "[DONE] status=ok maxRMS=%.6f baselineRMS=%.6f frames=%d finals=%d translated=%d translateFails=%d engine=%@",
                snapshot.maxRMS, baseline, snapshot.totalFrames, snapshot.finalTexts.count,
                snapshot.translatedTexts.count, snapshot.translationFailures, currentEngineName()
            )
        )
        writer.write("[STREAM-COUNT] deltas=\(snapshot.streamingDeltaCount)")
        writer.write("[TEXT] \(snapshot.finalTexts.joined(separator: " "))")
        writer.write("[JA] \(snapshot.translatedTexts.joined(separator: " "))")
        // 遅延の要約（改修の前後を同じ手法で比べるための行）
        writer.write("[LATENCY] \(latency.summaryLine())")
        // 確定した文がひとつ残らず訳されたか（全文保証の機械判定）
        writeCoverage(snapshot: snapshot, writer: writer)
        return 0
    }

    /// 「確定した文 == 訳して表示イベントに乗った文」を判定して出力する
    ///
    /// 確定断片は調停役がまとめてから訳すので件数は 1:1 にならない。そこで
    /// **確定テキストを繋いだもの**と**実際に訳した原文を繋いだもの**を突き合わせ、
    /// 1 文字でも欠けたら incomplete として出す（鮮度優先で捨てていないことの担保）。
    private static func writeCoverage(snapshot: Statistics, writer: CaptionTestLogWriter) {
        let expected = normalize(snapshot.finalTexts.joined(separator: " "))
        let covered = normalize(snapshot.translatedSources.joined(separator: " "))
        let isComplete = expected == covered
        writer.write(
            "[COVERAGE] status=\(isComplete ? "ok" : "incomplete") "
                + "finalChars=\(expected.count) coveredChars=\(covered.count) "
                + "finalSentences=\(sentenceCount(expected)) coveredSentences=\(sentenceCount(covered)) "
                + "finalEvents=\(snapshot.finalTexts.count) translateEvents=\(snapshot.translatedTexts.count) "
                + "translateFails=\(snapshot.translationFailures)"
        )
        guard !isComplete else { return }
        // どこから食い違ったのかが分かるよう、一致した先頭の後ろを出す
        let commonPrefix = expected.commonPrefix(with: covered)
        writer.write("[COVERAGE-MISSING] \(expected.dropFirst(commonPrefix.count))")
    }

    /// 比較用に空白を潰して前後を削る
    private static func normalize(_ text: String) -> String {
        text.split(whereSeparator: { $0.isWhitespace }).joined(separator: " ")
    }

    /// 文末記号の数（参考情報）
    private static func sentenceCount(_ text: String) -> Int {
        text.filter { ".!?。！？".contains($0) }.count
    }

    /// ハーネスで使う翻訳器
    ///
    /// 本番と同じ選択規則（モック指定 → 設定エンジン）。
    private static func makeTranslator() -> Translator {
        if CaptionService.usesMockTranslator { return MockTranslator() }
        switch CaptionSettings.translationEngine {
        case .apple: return AppleTranslator()
        case .gemini: return FallbackTranslator(primary: GeminiTranslator(), fallback: AppleTranslator())
        case .groq: return FallbackTranslator(primary: GroqTranslator(), fallback: AppleTranslator())
        }
    }

    /// 部分訳に使う翻訳器（本番と同じ規則。クラウドは高頻度呼び出しを避けるため対象外）
    private static func partialTranslator(_ translator: Translator) -> Translator? {
        if CaptionService.usesMockTranslator { return translator }
        return CaptionSettings.translationEngine.isCloud ? nil : translator
    }

    /// ログに出すエンジン名
    private static func currentEngineName() -> String {
        CaptionService.usesMockTranslator ? "mock" : CaptionSettings.translationEngine.rawValue
    }

    /// テスト中に集める統計
    private struct Statistics {
        var maxRMS: Float = 0
        var totalFrames: Int = 0
        var levelSamples: [Float] = []
        var finalTexts: [String] = []
        var translatedTexts: [String] = []
        /// 実際に翻訳して表示イベントに乗った原文（全文保証の判定に使う）
        var translatedSources: [String] = []
        var translationFailures: Int = 0
        var partialCount: Int = 0
        /// ストリーミングで届いた差分の回数（Groq のときだけ増える）
        var streamingDeltaCount: Int = 0
    }
}
