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

    // MARK: - 区切りポリシー（2026-08-10 ユーザー指摘「一文が長すぎて翻訳されるまで時間がかかりすぎる。
    // もっと文を区切れ区切れにして。ある程度長くなったら一旦すぐ翻訳して」を受けた調整値）
    //
    // 考え方: 字幕は「読める粒度」で早く出るほうが体感が良い。1 行の最低表示時間は
    // `max(3秒, 文字数×0.15秒)` なので、**日本語 20〜30 字＝英語 40〜50 字**あたりが
    // 「3〜4 秒で読み切れる 1 行」に収まる。ここを超えて溜めると、読む速さではなく
    // 待ち時間のほうが長くなる。

    /// 文末とみなす記号
    private static let sentenceEndings: Set<Character> = [".", "!", "?", "。", "！", "？"]

    /// 文末が来なくてもこの文字数を超えたら送る（ハード上限）
    ///
    /// 英語 48 字 ≒ 日本語 25 字前後 ≒ 1 行で読み切れる量。以前は 80 字だったが、
    /// 接続詞で延々と続く話し方（実測の配信音声）では 1 行が出るまで数秒待たされていた。
    private static let maxBufferedCharacters = 48

    /// 節区切りを探し始める長さ
    ///
    /// これ未満で切ると「単語だけの行」が並んで逆に読みにくい。
    /// 32 字あれば 1 つの節として意味が通る。
    private static let clauseBreakMinimumCharacters = 32

    /// 節の切れ目とみなす目印（見つけた目印の**直後**で切る）
    ///
    /// カンマと等位・従属接続詞。話し言葉は文末記号がなかなか来ないので、
    /// ここで刻まないと 1 文がいつまでも確定しない。
    private static let clauseBreakMarkers = [
        ", ", " and ", " but ", " so ", " because ", " then ", " or ", " while ", " when ",
    ]

    /// 新しい確定が来ないまま経過したら送る（言い切りで止まった時に字幕が出ないのを防ぐ）
    ///
    /// 0.7 秒だと「言い切って黙った」ときの空白が体感ではっきり分かるため詰めた。
    /// これ以上短くすると、単に息継ぎしただけの間で切れて行が細切れになる。
    private static let idleFlushInterval: TimeInterval = 0.45
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
            drainBufferLocked(force: false)
        }
    }

    /// バッファに残っている分を強制的に流す（停止時など）
    func flush() {
        queue.async { [self] in drainBufferLocked(force: true) }
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

    /// 送れる分だけバッファから切り出して流す
    ///
    /// 1 回の呼び出しで複数の節が出ることがある（長い確定が一度に来たとき）。
    /// 残った端切れは次の確定か idle タイマーで出るので、**取りこぼしはしない**
    /// （全文翻訳保証＝ `[COVERAGE]` を壊さない）。
    ///
    /// - Parameter force: 短くても残り全部を出す（停止時・言い切りで黙ったとき）
    private func drainBufferLocked(force: Bool) {
        cancelIdleFlush()
        while let segment = takeFlushableSegmentLocked() {
            emitLocked(segment)
        }
        let rest = buffer.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !rest.isEmpty else {
            buffer = ""
            return
        }
        if force {
            buffer = ""
            emitLocked(rest)
        } else {
            scheduleIdleFlush()
        }
    }

    /// バッファの先頭から「いま送出してよい部分」を切り出す
    ///
    /// **長さの判定を文末判定より先に行う**のが要点。音声認識は 40 語を超える 1 文を
    /// まるごと 1 件の確定として渡してくることがあり（実測 199 字）、
    /// 「文末で終わっていたらそのまま出す」を先に見ると、その巨大な 1 文が
    /// 1 行として出るまで丸ごと待たされてしまう。
    ///
    /// - Returns: 送出する文字列。まだ溜めておくなら nil
    private func takeFlushableSegmentLocked() -> String? {
        let text = buffer.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return nil }

        // 1. 上限より長いものは、文末で終わっていても刻む
        if text.count > Self.maxBufferedCharacters {
            let cut = clauseBreakIndex(in: text, limit: Self.maxBufferedCharacters)
                ?? wordBreakIndex(in: text, limit: Self.maxBufferedCharacters)
            if let cut { return splitLocked(text, at: cut) }
            // 切れ目がまったく無い（1 単語が異常に長い等）ときだけ、そのまま出す
            buffer = ""
            return text
        }
        // 2. 上限内で文末まで来ていれば 1 文としてそのまま出す
        if let last = text.last, Self.sentenceEndings.contains(last) {
            buffer = ""
            return text
        }
        // 3. ある程度の長さになったら、節の切れ目で先に出す（文末を待たない）
        guard text.count >= Self.clauseBreakMinimumCharacters,
              let cut = clauseBreakIndex(in: text, limit: Self.maxBufferedCharacters) else { return nil }
        return splitLocked(text, at: cut)
    }

    /// 指定位置で切り、前半を返して後半をバッファに残す
    private func splitLocked(_ text: String, at cut: String.Index) -> String? {
        let head = String(text[text.startIndex..<cut]).trimmingCharacters(in: .whitespacesAndNewlines)
        guard !head.isEmpty else { return nil }
        buffer = String(text[cut...]).trimmingCharacters(in: .whitespacesAndNewlines)
        return head
    }

    /// 節の切れ目（目印の直後の位置）を探す
    ///
    /// 候補のうち**上限に収まる範囲でいちばん後ろ**を採る。前寄りで切ると
    /// 「単語 2〜3 個だけの行」になり、かえって読みにくくなるため。
    ///
    /// - Parameters:
    ///   - text: 探索対象（トリム済み）
    ///   - limit: 切り出す先頭の最大文字数
    /// - Returns: 切る位置。適切な切れ目が無ければ nil
    private func clauseBreakIndex(in text: String, limit: Int) -> String.Index? {
        var best: String.Index?
        var bestOffset = 0
        for marker in Self.clauseBreakMarkers {
            var searchStart = text.startIndex
            while let found = text.range(of: marker, range: searchStart..<text.endIndex) {
                let offset = text.distance(from: text.startIndex, to: found.upperBound)
                // 短すぎる先頭は採らない／上限を超える位置も採らない
                if offset >= Self.clauseBreakMinimumCharacters, offset <= limit,
                   found.upperBound < text.endIndex, offset > bestOffset {
                    best = found.upperBound
                    bestOffset = offset
                }
                searchStart = found.upperBound
            }
        }
        return best
    }

    /// 節の切れ目が無いときの保険：上限内で最後の単語境界を探す
    ///
    /// 単語の途中で切ると訳が壊れるため、必ず空白で切る。
    ///
    /// - Parameters:
    ///   - text: 探索対象（トリム済み）
    ///   - limit: 切り出す先頭の最大文字数
    /// - Returns: 切る位置。見つからなければ nil
    private func wordBreakIndex(in text: String, limit: Int) -> String.Index? {
        var best: String.Index?
        var offset = 0
        var index = text.startIndex
        while index < text.endIndex, offset <= limit {
            if text[index].isWhitespace, offset >= Self.clauseBreakMinimumCharacters {
                best = text.index(after: index)
            }
            index = text.index(after: index)
            offset += 1
        }
        return best
    }

    /// 1 区切りぶんをキューへ移して翻訳を起動する
    private func emitLocked(_ text: String) {
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
        // 言い切って黙ったときは短くても出す（無言で待たせない）
        timer.setEventHandler { [weak self] in self?.drainBufferLocked(force: true) }
        timer.resume()
        idleTimer = timer
    }

    /// 保険タイマーを止める
    private func cancelIdleFlush() {
        idleTimer?.cancel()
        idleTimer = nil
    }
}
