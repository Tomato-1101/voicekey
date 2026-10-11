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
    /// 英語 40 字 ≒ 日本語 20 字前後 ≒ 字幕 1 行に収まる量。80 字 → 48 字 → 40 字と詰めてきた。
    /// 40 字にしたのは 2026-10-12 ユーザー指示「精度が落ちてもいいから速さ重視」と、
    /// 字幕の幅を固定したこと（訳が 1 行に収まれば高さが揺れない）の両方のため。
    static let maxBufferedCharacters = 40

    /// 節区切りを探し始める長さ
    ///
    /// これ未満で切ると「単語だけの行」が並んで逆に読みにくい。
    /// 24 字（英語 5〜6 語）あれば 1 つの節として意味が通る。32 字から詰めた（速さ重視）。
    static let clauseBreakMinimumCharacters = 24

    /// 節の切れ目とみなす目印（見つけた目印の**直後**で切る）
    ///
    /// カンマと等位・従属接続詞。話し言葉は文末記号がなかなか来ないので、
    /// ここで刻まないと 1 文がいつまでも確定しない。
    private static let clauseBreakMarkers = [
        ", ", " and ", " but ", " so ", " because ", " then ", " or ", " while ", " when ",
    ]

    /// 新しい確定が来ないまま経過したら送る（言い切りで止まった時に字幕が出ないのを防ぐ）
    ///
    /// 0.7 秒 → 0.45 秒 → 0.25 秒と詰めた。認識側が 0.3 秒の切れ目で確定を前倒しするように
    /// なったので（PauseFinalizeTrigger）、確定が来た時点で話はもう途切れている。ここで
    /// さらに長く待つ理由が無い。息継ぎで細切れになるのは 24 字未満の端切れだけで、
    /// それも 1 文字も落とさずに出る。
    static let idleFlushInterval: TimeInterval = 0.25
    /// 翻訳待ちキューの上限
    ///
    /// 翻訳は実測 1 文 30ms なので、喋る速さ（数秒に 1 文）に対して実質詰まらない。
    /// クラウドが詰まったとき用の安全弁。**溢れた行も捨てない**（確定文は全部訳す恒久要件）。
    /// 溢れた分は `overflowTranslatorProvider`（Apple）で別に訳す。
    static let maxPendingCount = 10
    /// クラウドの訳を待って次の差し替えへ進むまでの上限（秒）
    ///
    /// これを過ぎたら待つのをやめて次の行の差し替えへ進む（クラウドの遅れで後続の行が詰まらないように）。
    /// ただし投げた呼び出しは止めず、遅れて届いた訳も、その行がまだ画面か文脈に残っていれば差し替える
    /// （課金済みの訳を捨てない）。締め切り前に投げられなかった行だけは、送らずに Apple の訳で確定する。
    static let refineTimeout: TimeInterval = 1.5
    /// 締め切りを過ぎても結果を待っている差し替えの同時数の上限
    ///
    /// 遅れている呼び出しを止めずに次へ進むので、クラウドが詰まると呼び出しが積み上がる。
    /// 2 本までにして、それ以上は先の呼び出しが返るまで新しく投げない（429 の連鎖と課金の膨張を防ぐ）。
    static let maxRefinesInFlight = 2
    /// 文脈として持たせる直近ペア数
    private static let contextPairCount = 4

    private let logger = makeCaptionLogger("TranslationCoordinator")
    /// 翻訳器は 1 文ごとに解決する（メニューでエンジンを切り替えたら次の文から反映するため）
    private let translatorProvider: @Sendable () -> Translator
    /// 表示順の採番（部分訳と共通の連番。古い結果を捨てる判定に使う）
    private let sequenceProvider: @Sendable () -> Int
    /// 溢れた行を訳す翻訳器（nil なら溢れても捨てずにキューに残す）
    private let overflowTranslatorProvider: (@Sendable () -> Translator)?
    /// 先に出した訳を差し替える翻訳器（クラウド）。nil なら差し替えない
    ///
    /// 確定行には `translatorProvider`（Apple）の訳をまず出し、こちらの訳が
    /// `refineTimeout` 以内に届いたら同じ行を差し替える。クラウドへ送るのは確定文だけ。
    private let refinerProvider: (@Sendable () -> Translator?)?
    private let queue = DispatchQueue(label: "com.voicekey.caption.translation")
    /// 差し替え待ちの行（クラウドは同時 1 本に絞る。古い順）
    private var refinePending: [RefineRequest] = []
    /// 締め切りまで結果を待っている差し替えがある（この間は次を投げない＝順番どおりに差し替える）
    private var isRefining = false
    /// 投げて結果がまだ返っていない差し替えの数（締め切りを過ぎたものも含む）
    private var refinesInFlight = 0
    /// 差し替えの枠の世代（締め切りと結果のどちらが先に枠を空けたかを見分ける）
    private var refineSlotID = 0

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
    /// 先に出した訳がクラウドの訳で差し替わったときに呼ばれる（原文, 訳文, 採番）
    ///
    /// 採番は `onTranslated` で渡したものと同じ。表示側はこれで同じ行を探して書き換える。
    var onRevised: (@Sendable (String, String, Int) -> Void)?

    /// 差し替え待ちの 1 行
    private struct RefineRequest {
        let text: String
        let sequence: Int
        let context: [TranslationExchange]
        /// これを過ぎたら差し替えを諦める時刻
        let deadline: CFAbsoluteTime
        let translator: Translator
    }

    /// - Parameters:
    ///   - translatorProvider: 実際に翻訳する実装を返すクロージャ（毎回呼ばれる）
    ///   - sequenceProvider: 表示順の採番（既定は単調増加のみで良い場面向け）
    ///   - overflowTranslatorProvider: キューが溢れたときに使う翻訳器（Apple）
    ///   - refinerProvider: 先に出した訳を差し替える翻訳器（クラウド）。1 文ごとに呼ぶ
    init(
        translatorProvider: @escaping @Sendable () -> Translator,
        sequenceProvider: @escaping @Sendable () -> Int,
        overflowTranslatorProvider: (@Sendable () -> Translator)? = nil,
        refinerProvider: (@Sendable () -> Translator?)? = nil
    ) {
        self.translatorProvider = translatorProvider
        self.sequenceProvider = sequenceProvider
        self.overflowTranslatorProvider = overflowTranslatorProvider
        self.refinerProvider = refinerProvider
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
            refinePending.removeAll()
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
        if pending.count > Self.maxPendingCount, let overflowTranslatorProvider {
            // 確定文は捨てない。いちばん古い行を、詰まっている翻訳器とは別に Apple で訳す
            let overflow = pending.removeFirst()
            logger.notice("翻訳キューが溢れたため Apple で訳します 長さ=\(overflow.text.count, privacy: .public)")
            translateOverflow(overflow, with: overflowTranslatorProvider())
        }
        dispatchNextLocked()
    }

    /// 溢れた 1 行を、キューとは別に訳して出す
    ///
    /// 順番はキューの行より先に出ることがあるが、捨てるよりは良い（全文翻訳保証が優先）。
    private func translateOverflow(_ line: (text: String, sequence: Int), with translator: Translator) {
        Task { [self] in
            do {
                let translated = try await translator.translate(line.text, context: [])
                onTranslated?(line.text, translated, line.sequence)
            } catch {
                let message = (error as? TranslationError)?.userMessage ?? String(describing: error)
                onFailed?(line.text, message)
            }
        }
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
                // 差し替え先はここで解決しておく（エンジンを切り替えたら次の文から効く）
                let refiner = refinerProvider?()
                queue.async { [self] in
                    appendHistoryLocked(TranslationExchange(source: text, translated: translated))
                    isTranslating = false
                    if let refiner {
                        refinePending.append(RefineRequest(
                            text: text, sequence: line.sequence, context: context,
                            deadline: CFAbsoluteTimeGetCurrent() + Self.refineTimeout, translator: refiner
                        ))
                        dispatchNextRefineLocked()
                    }
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

    /// 文脈の履歴に足す（長くなりすぎた古い分は落とす）
    private func appendHistoryLocked(_ exchange: TranslationExchange) {
        history.append(exchange)
        if history.count > Self.contextPairCount * 2 {
            history.removeFirst(history.count - Self.contextPairCount * 2)
        }
    }

    /// 差し替え待ちの先頭を 1 本だけクラウドで訳す
    ///
    /// 締め切り（Apple の訳を出してから `refineTimeout`）を過ぎた行は投げずに諦める。
    /// 待ち行列で古くなった行までクラウドへ送ると、課金だけ増えて画面は変わらないため。
    ///
    /// 投げた呼び出しは締め切りで打ち切らない。締め切りで「待つのをやめて次へ進む」だけにし、
    /// 遅れて届いた訳も `onRevised` で渡す（表示側は同じ採番の行がまだあれば書き換える）。
    /// 以前は締め切りで結果を捨てていたため、課金済みのクラウドの訳が画面にも文脈にも残らなかった。
    /// 429 などの失敗は従来どおり Apple の訳のまま確定する。
    private func dispatchNextRefineLocked() {
        guard !isRefining, refinesInFlight < Self.maxRefinesInFlight else { return }
        let now = CFAbsoluteTimeGetCurrent()
        while let first = refinePending.first, first.deadline <= now {
            refinePending.removeFirst()
            logger.notice("クラウドへ送る前に締め切りを過ぎたため Apple の訳で確定 長さ=\(first.text.count, privacy: .public)")
        }
        guard !refinePending.isEmpty else { return }
        let request = refinePending.removeFirst()
        isRefining = true
        refinesInFlight += 1
        refineSlotID += 1
        let slot = refineSlotID
        Task { [self] in
            let refined: String?
            do {
                refined = try await request.translator.translate(request.text, context: request.context)
            } catch {
                refined = nil
                logger.notice("クラウドの訳に失敗したため Apple の訳で確定 長さ=\(request.text.count, privacy: .public)")
            }
            if let refined {
                onRevised?(request.text, refined, request.sequence)
            }
            queue.async { [self] in
                if let refined {
                    // 文脈にはクラウドの訳を残す（次の文の訳の質を上げるため）。
                    // 遅れて届いたときも、まだ文脈に残っていれば差し替える
                    if let index = history.lastIndex(where: { $0.source == request.text }) {
                        history[index] = TranslationExchange(source: request.text, translated: refined)
                    }
                }
                refinesInFlight -= 1
                // 締め切り前に返ったなら、この呼び出しが持っていた枠を空ける
                if slot == refineSlotID { isRefining = false }
                dispatchNextRefineLocked()
            }
        }
        // 締め切りが来たら、結果を待たずに次の行の差し替えへ進む（呼び出し自体は続ける）
        queue.asyncAfter(deadline: .now() + max(0, request.deadline - now)) { [self] in
            guard isRefining, slot == refineSlotID else { return }
            isRefining = false
            logger.notice("クラウドの訳が締め切りまでに来ないため次へ進む（届けば差し替える） 長さ=\(request.text.count, privacy: .public)")
            dispatchNextRefineLocked()
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
