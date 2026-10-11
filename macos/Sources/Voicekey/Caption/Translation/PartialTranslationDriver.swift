/// 認識途中テキストの部分訳（追従感を作るための先出し）
///
/// なぜ要るか（実測）:
/// SpeechAnalyzer が確定（isFinal）を出すのは **発話が終わってから中央値 4.3 秒後**。
/// 一方で翻訳自体は 30ms 程度しかかからない。つまり「翻訳が遅い」の正体は確定待ちで、
/// 確定を待って訳すかぎり字幕は必ず 4 秒遅れる。
/// そこで途中経過（ボラタイル）を先に訳して出し、確定訳で差し替える。
///
/// 方針:
/// - 約 180ms のスロットル。認識途中は毎秒何度も更新されるので、毎回は訳さない。
///   以前は 500ms だったが、2026-10-12 ユーザー指示「精度が落ちてもいいから速さ重視」で詰めた
///   （Apple のオンデバイス翻訳は 1 文 30ms なので 180ms 間隔でも詰まらない）。
/// - latest-wins。実行中 1 件＋待機は最新 1 件だけ。古い待機は捨てる（鮮度優先）。
/// - クラウドエンジン（Gemini）では使わない。高頻度呼び出しは恒久方針で禁止しているため、
///   呼び出し側が翻訳器を渡さないことで無効化する。
import Foundation
import OSLog

/// ボラタイルテキストの部分訳を回す
@available(macOS 26.0, *)
final class PartialTranslationDriver: @unchecked Sendable {

    /// 部分訳を投げる最短間隔
    ///
    /// 0.18 秒＝ほぼ 1 語ごと。これより短くしても認識の更新間隔（実測 0.1〜0.3 秒）を超えて
    /// 速くはならず、訳が細かく揺れて読みにくくなるだけ。
    static let throttleInterval: TimeInterval = 0.18
    /// これより短い断片は訳さない
    ///
    /// 3 字（"I'm" 程度）から訳す。6 字だと最初の 1〜2 語が訳されず、話し始めの反応が鈍く見えた。
    static let minimumCharacters = 3

    private let logger = makeCaptionLogger("PartialTranslation")
    private let queue = DispatchQueue(label: "com.voicekey.caption.partial")
    /// 部分訳に使う翻訳器（nil を返すと部分訳は行わない）
    private let translatorProvider: @Sendable () -> Translator?
    /// 表示順の採番（呼び出し側と共有する。古い結果を捨てる判定に使う）
    private let sequenceProvider: @Sendable () -> Int

    private var lastDispatchAt: CFAbsoluteTime = 0
    private var isTranslating = false
    private var pendingText: String?
    private var lastRequestedText = ""
    private var scheduledTimer: DispatchSourceTimer?
    /// 区切りの世代。確定が来たら進め、それより前に投げた部分訳の結果は捨てる
    ///
    /// 確定した文の部分訳が、確定訳を出した後から届いてライブ行に再表示されるのを防ぐ
    /// （確定訳の後に古い途中経過が出ると、同じ文が 2 回流れて見える）。
    private var epoch = 0

    /// 部分訳ができたときに呼ばれる（原文, 訳文, 採番）
    var onPartial: (@Sendable (String, String, Int) -> Void)?

    /// - Parameters:
    ///   - translatorProvider: 部分訳に使う翻訳器（対応しないエンジンでは nil を返す）
    ///   - sequenceProvider: 表示順の採番
    init(
        translatorProvider: @escaping @Sendable () -> Translator?,
        sequenceProvider: @escaping @Sendable () -> Int
    ) {
        self.translatorProvider = translatorProvider
        self.sequenceProvider = sequenceProvider
    }

    /// 認識途中のテキストを渡す
    func submit(_ text: String) {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard trimmed.count >= Self.minimumCharacters else { return }
        queue.async { [self] in
            pendingText = trimmed
            scheduleIfPossible()
        }
    }

    /// 状態を捨てる（字幕の停止・再開で前の発話を引きずらないように）
    ///
    /// 認識が確定したときにも呼ぶ。待機中の途中経過も、実行中の部分訳の結果も捨てる。
    func reset() {
        queue.async { [self] in
            epoch += 1
            scheduledTimer?.cancel()
            scheduledTimer = nil
            pendingText = nil
            lastRequestedText = ""
        }
    }

    // MARK: - 内部処理（すべて queue 上）

    /// スロットル間隔を見て、投げられるなら投げる・まだなら後で投げ直す
    private func scheduleIfPossible() {
        guard !isTranslating, let text = pendingText, text != lastRequestedText else { return }
        let elapsed = CFAbsoluteTimeGetCurrent() - lastDispatchAt
        if elapsed >= Self.throttleInterval {
            dispatch(text)
        } else if scheduledTimer == nil {
            let timer = DispatchSource.makeTimerSource(queue: queue)
            timer.schedule(deadline: .now() + (Self.throttleInterval - elapsed))
            timer.setEventHandler { [weak self] in
                guard let self else { return }
                self.scheduledTimer = nil
                self.scheduleIfPossible()
            }
            timer.resume()
            scheduledTimer = timer
        }
    }

    /// 実際に 1 件だけ訳す
    private func dispatch(_ text: String) {
        guard let translator = translatorProvider() else { return }
        isTranslating = true
        lastDispatchAt = CFAbsoluteTimeGetCurrent()
        lastRequestedText = text
        pendingText = nil
        // 採番は「投げた瞬間」に取る。後から来た確定訳の方が新しいと判定できるようにするため。
        let sequence = sequenceProvider()
        let requestedEpoch = epoch

        Task { [self] in
            do {
                let translated = try await translator.translate(text, context: [])
                // 訳している間に確定が来ていたら、この途中経過はもう古いので出さない
                let isCurrent = queue.sync { requestedEpoch == epoch }
                if isCurrent { onPartial?(text, translated, sequence) }
            } catch {
                // 部分訳は best effort。失敗しても確定訳で出るので黙って捨てる。
                logger.notice("部分訳に失敗（無視）: \(String(describing: error), privacy: .public)")
            }
            queue.async { [self] in
                isTranslating = false
                scheduleIfPossible()
            }
        }
    }
}
