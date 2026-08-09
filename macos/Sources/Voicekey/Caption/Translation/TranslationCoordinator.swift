/// 認識結果 → 翻訳 の流量制御
///
/// 音声認識の確定断片はしばしば短すぎる（"So," / "and then"）。そのまま毎回 API を
/// 叩くとコストも遅延も無駄なので、文末か一定文字数までバッファしてから 1 回投げる。
/// 翻訳中に次が来たらキューに積み、同時実行は 1 本に絞る（順序が入れ替わると字幕が壊れるため）。
///
/// **確定した文は必ず全部訳して順番どおり出す**（2026-08-09 ユーザー指示）。
/// 以前は「訳している間に次が来たら古い訳を捨てる」ようにしていたが、
/// 実測で翻訳は 1 文 30ms しかかからず捨てる必要がそもそも無かった。
/// 捨てると「読む前に消える・訳されないまま次へ行く」という実害だけが残ったため撤廃した。
import Foundation
import OSLog
import os

/// 認識テキストをまとめて翻訳へ流す調停役
@available(macOS 26.0, *)
final class TranslationCoordinator: @unchecked Sendable {

    /// 文末とみなす記号
    private static let sentenceEndings: Set<Character> = [".", "!", "?", "。", "！", "？"]
    /// 文末が来なくてもこの文字数を超えたら送る
    ///
    /// 長く溜めるほど 1 行が遅れて出る。鮮度優先で短めにしている。
    private static let maxBufferedCharacters = 80
    /// 新しい確定が来ないまま経過したら送る（言い切りで止まった時に字幕が出ないのを防ぐ）
    private static let idleFlushInterval: TimeInterval = 0.7
    /// 翻訳待ちキューの上限
    ///
    /// 翻訳は実測 1 文 30ms なので、喋る速さ（数秒に 1 文）に対して実質詰まらない。
    /// ここは「異常時に無限に溜めない」ためだけの安全弁で、通常は 1 件も溢れない。
    /// 溢れたときだけ notice を残す。
    private static let maxPendingCount = 10
    /// 文脈として持たせる直近ペア数
    private static let contextPairCount = 4

    private let logger = makeCaptionLogger("TranslationCoordinator")
    /// 翻訳器は 1 文ごとに解決する（メニューでエンジンを切り替えたら次の文から反映するため）
    private let translatorProvider: @Sendable () -> Translator
    /// 表示順の採番（部分訳と共通の連番。古い結果を捨てる判定に使う）
    private let sequenceProvider: @Sendable () -> Int
    private let queue = DispatchQueue(label: "com.voicekey.caption.translation")

    private var buffer = ""
    /// 翻訳待ちの行（本文と採番）
    private var pending: [(text: String, sequence: Int)] = []
    private var history: [TranslationExchange] = []
    private var isTranslating = false
    private var idleTimer: DispatchSourceTimer?

    /// 原文がまとまった直後（翻訳前）に呼ばれる。空白で待たせないため先に原文を出す。
    var onSourceReady: (@Sendable (String) -> Void)?
    /// 翻訳が成功したときに呼ばれる（原文, 訳文, 採番）
    var onTranslated: (@Sendable (String, String, Int) -> Void)?
    /// ストリーミング対応の翻訳器で、訳が届く途中に呼ばれる（原文, ここまでの訳, 採番）
    ///
    /// 訳し終わるのを待たずに読み始められるようにするための通知。
    /// 確定行は最後の `onTranslated` が必ず 1 行積むので、こちらは途中経過専用。
    var onStreamingDelta: (@Sendable (String, String, Int) -> Void)?
    /// 翻訳が失敗したときに呼ばれる（原文, ユーザー向け説明）
    var onFailed: (@Sendable (String, String) -> Void)?

    /// - Parameters:
    ///   - translatorProvider: 実際に翻訳する実装を返すクロージャ（毎回呼ばれる）
    ///   - sequenceProvider: 表示順の採番（既定は単調増加のみで良い場面向け）
    init(
        translatorProvider: @escaping @Sendable () -> Translator,
        sequenceProvider: @escaping @Sendable () -> Int
    ) {
        self.translatorProvider = translatorProvider
        self.sequenceProvider = sequenceProvider
    }

    /// 確定した認識テキストを渡す
    ///
    /// - Parameter finalText: SpeechTranscriber が確定させたテキスト
    func submit(_ finalText: String) {
        let text = finalText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        queue.async { [self] in
            buffer = buffer.isEmpty ? text : buffer + " " + text
            if shouldFlushBuffer() {
                flushBufferLocked()
            } else {
                scheduleIdleFlush()
            }
        }
    }

    /// バッファに残っている分を強制的に流す（停止時など）
    func flush() {
        queue.async { [self] in flushBufferLocked() }
    }

    /// 状態を初期化する（字幕の停止→再開で前の文脈を引きずらないように）
    func reset() {
        queue.async { [self] in
            cancelIdleFlush()
            buffer = ""
            pending.removeAll()
            history.removeAll()
        }
    }

    // MARK: - 内部処理（すべて queue 上で実行）

    /// バッファを送るべきか
    private func shouldFlushBuffer() -> Bool {
        guard let last = buffer.last else { return false }
        if Self.sentenceEndings.contains(last) { return true }
        return buffer.count >= Self.maxBufferedCharacters
    }

    /// バッファをキューへ移して翻訳を起動する
    private func flushBufferLocked() {
        cancelIdleFlush()
        let text = buffer.trimmingCharacters(in: .whitespacesAndNewlines)
        buffer = ""
        guard !text.isEmpty else { return }

        onSourceReady?(text)

        pending.append((text, sequenceProvider()))
        if pending.count > Self.maxPendingCount {
            let dropped = pending.removeFirst()
            logger.notice("翻訳キューが溢れたため古い行を破棄しました 長さ=\(dropped.text.count, privacy: .public)")
        }
        dispatchNextLocked()
    }

    /// キューの先頭を 1 本だけ翻訳する
    private func dispatchNextLocked() {
        guard !isTranslating, !pending.isEmpty else { return }
        let line = pending.removeFirst()
        let text = line.text
        let context = Array(history.suffix(Self.contextPairCount))
        isTranslating = true

        let translator = translatorProvider()
        Task { [self] in
            do {
                let translated = try await translate(text, context: context, with: translator, sequence: line.sequence)
                queue.async { [self] in
                    history.append(TranslationExchange(source: text, translated: translated))
                    if history.count > Self.contextPairCount * 2 {
                        history.removeFirst(history.count - Self.contextPairCount * 2)
                    }
                    isTranslating = false
                    dispatchNextLocked()
                }
                // 確定した文の訳は必ず出す。次の行が来ていても捨てない
                // （捨てると「訳される前に次へ行く」＝意味が分からない、になる）。
                onTranslated?(text, translated, line.sequence)
            } catch {
                let message = (error as? TranslationError)?.userMessage ?? String(describing: error)
                logger.error("翻訳に失敗: \(String(describing: error), privacy: .public)")
                queue.async { [self] in
                    isTranslating = false
                    dispatchNextLocked()
                }
                // 失敗しても止めない。原文だけ出し続ける。
                onFailed?(text, message)
            }
        }
    }

    /// 1 文を訳す（ストリーミングに対応した翻訳器なら途中経過も配る）
    ///
    /// - Parameters:
    ///   - text: 原文
    ///   - context: 直近の確定ペア
    ///   - translator: 使う翻訳器
    ///   - sequence: この行の採番
    /// - Returns: 訳文の全文
    private func translate(
        _ text: String, context: [TranslationExchange], with translator: Translator, sequence: Int
    ) async throws -> String {
        guard let streaming = translator as? StreamingTranslator, let onStreamingDelta else {
            return try await translator.translate(text, context: context)
        }
        // 差分は任意スレッドから届くので、積み上げはロックの中で行う
        let accumulated = OSAllocatedUnfairLock(initialState: "")
        return try await streaming.translateStreaming(text, context: context) { piece in
            let soFar = accumulated.withLock { value -> String in
                value += piece
                return value
            }
            onStreamingDelta(text, soFar, sequence)
        }
    }

    /// 無音で途切れたときの保険タイマーを張り直す
    private func scheduleIdleFlush() {
        cancelIdleFlush()
        let timer = DispatchSource.makeTimerSource(queue: queue)
        timer.schedule(deadline: .now() + Self.idleFlushInterval)
        timer.setEventHandler { [weak self] in self?.flushBufferLocked() }
        timer.resume()
        idleTimer = timer
    }

    /// 保険タイマーを止める
    private func cancelIdleFlush() {
        idleTimer?.cancel()
        idleTimer = nil
    }
}
