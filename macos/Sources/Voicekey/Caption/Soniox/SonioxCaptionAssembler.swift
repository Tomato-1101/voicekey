/// Soniox リアルタイム応答 → 字幕の行 への組み立て（純ロジック）
///
/// Soniox は 1 つの WebSocket で「認識（原文）」と「翻訳（訳）」のトークンを混ぜて返す。
/// - `is_final=false` のトークンは毎回の応答で丸ごと置き換わる（＝ライブ行）
/// - `is_final=true` のトークンは 1 度だけ届き、以後は変わらない（＝確定分として積む）
/// - 訳トークン（`translation_status="translation"`）は元のトークンより後に、塊ごとに届く。
///   原文と 1 対 1 には対応しない。
/// - 発話の切れ目は `{"text":"<end>","is_final":true}` で届く（エンドポイント検出）。
///
/// ここでは「確定分」と「未確定分」を原文・訳それぞれで持ち、切れ目が来たら 1 行に確定する。
/// 通信と切り離して単体テストできるように、JSON の解析もここに置く。
import Foundation

/// Soniox の応答 1 件を字幕の行にまとめる
struct SonioxCaptionAssembler {

    /// 応答のトークン 1 つ（字幕に要る項目だけ）
    struct Token: Equatable {
        var text: String
        var isFinal: Bool
        /// 訳トークンか（`translation_status="translation"`）
        var isTranslation: Bool
        /// 後で訳が届く原文トークンか（`translation_status="original"`）
        ///
        /// 翻訳先と同じ言語（日本語）で話された部分は `"none"` になり、訳は届かない。
        /// それを待ち続けると行が確定しないので区別する。
        var awaitsTranslation: Bool = false
    }

    /// 組み立ての結果
    enum Event: Equatable {
        /// ライブ行（まだ確定していない分を含む原文と訳）
        case live(source: String, translation: String)
        /// 確定した 1 行（原文と訳。文字起こしモードでは訳は空）
        case confirmed(source: String, translation: String)
    }

    /// 解析した応答
    struct Response: Equatable {
        var tokens: [Token]
        /// サーバーがストリームを閉じた
        var finished: Bool
        /// エラー（あれば）
        var errorCode: Int?
        var errorMessage: String?
    }

    /// 文末とみなす記号（原文・訳の両方）
    private static let sentenceEndings: Set<Character> = [".", "!", "?", "。", "！", "？"]

    /// 発話の切れ目を表す特別なトークン
    static let endToken = "<end>"
    /// 停止時に送る finalize の完了印（本文ではないので字幕・議事録に出さない）
    static let finToken = "<fin>"

    /// 文末が来ないまま喋り続けたときに、確定分だけで行を切る長さ（原文の文字数）
    ///
    /// 翻訳経路（TranslationCoordinator）の上限 40 字と同じ考え方。字幕 1 行で読み切れる量。
    /// 訳トークンは文の切れ目でまとめて届くので、ここで切るのは訳が無い文字起こしモードだけ。
    static let maxConfirmedSourceCharacters = 60

    /// 訳も組み立てるか（翻訳モード）
    let translates: Bool

    /// 確定済みでまだ行にしていない原文・訳
    private var finalSource = ""
    private var finalTranslation = ""
    /// 未確定（毎回置き換わる）原文・訳
    private var pendingSource = ""
    private var pendingTranslation = ""
    /// 切れ目（`<end>`）が来て、訳が揃いしだい確定したい状態
    private var endReached = false
    /// 確定分の原文に、まだ訳が届いていないもの（`"original"`）が含まれるか
    private var finalSourceAwaitsTranslation = false

    /// 切れ目は来たが訳を待っている状態か（呼び出し側が時間切れで `flush` するための目印）
    var isWaitingForTranslation: Bool {
        translates && endReached && !Self.clean(finalSource).isEmpty
    }

    init(translates: Bool) {
        self.translates = translates
    }

    /// 応答 1 件を取り込む
    ///
    /// - Returns: 画面へ出すイベント（確定行 → ライブ行 の順）
    mutating func apply(_ response: Response) -> [Event] {
        pendingSource = ""
        pendingTranslation = ""
        for token in response.tokens {
            // <fin> は「送った音声はすべて確定した」印なので、切れ目と同じく訳が揃いしだい確定させる
            if token.text == Self.endToken || token.text == Self.finToken {
                if token.isFinal { endReached = true }
                continue
            }
            switch (token.isTranslation, token.isFinal) {
            case (true, true): finalTranslation += token.text
            case (true, false): pendingTranslation += token.text
            case (false, true):
                finalSource += token.text
                if token.awaitsTranslation { finalSourceAwaitsTranslation = true }
            case (false, false): pendingSource += token.text
            }
        }

        // 訳がまとめて届いたときは 1 回で複数行が確定する（文ごとに原文と組にする）
        var events = takeConfirmedLines()
        events.append(.live(source: Self.clean(finalSource + pendingSource),
                            translation: Self.clean(finalTranslation + pendingTranslation)))
        return events
    }

    /// 確定済みの分だけを 1 行に確定する（訳待ちの時間切れ用）
    ///
    /// 未確定（`is_final=false`）の分は残す。接続は続いているので、未確定分は後で確定トークンとして
    /// 改めて届く。ここで行にすると、その確定トークンが届いたときに同じ文がもう一度記録される（二重記録）。
    mutating func flushConfirmed() -> Event? {
        let source = Self.clean(finalSource)
        let translation = Self.clean(finalTranslation)
        finalSource = ""
        finalTranslation = ""
        endReached = false
        finalSourceAwaitsTranslation = false
        guard !source.isEmpty || !translation.isEmpty else { return nil }
        return .confirmed(source: source, translation: translation)
    }

    /// 残っている分を全部 1 行に確定する（停止・切断時）
    ///
    /// 未確定分も含める。停止の瞬間に喋っていた最後の文を議事録・画面から落とさないため。
    mutating func flush() -> Event? {
        let source = Self.clean(finalSource + pendingSource)
        let translation = Self.clean(finalTranslation + pendingTranslation)
        finalSource = ""; pendingSource = ""
        finalTranslation = ""; pendingTranslation = ""
        endReached = false
        finalSourceAwaitsTranslation = false
        guard !source.isEmpty || !translation.isEmpty else { return nil }
        return .confirmed(source: source, translation: translation)
    }

    // MARK: - 内部処理

    /// 確定してよい行があれば取り出す
    private mutating func takeConfirmedLines() -> [Event] {
        let source = Self.clean(finalSource)
        guard !source.isEmpty else {
            // 原文が無いのに切れ目だけ来た（無音の区切り）ときは何もしない
            if Self.clean(finalTranslation).isEmpty { endReached = false }
            return []
        }
        if translates {
            return takeTranslatedLines()
        }
        // 文字起こし: 切れ目・文末・長さのどれかで確定する
        let endsSentence = source.last.map { Self.sentenceEndings.contains($0) } ?? false
        guard endReached || endsSentence || source.count >= Self.maxConfirmedSourceCharacters else { return [] }
        finalSource = ""
        endReached = false
        return [.confirmed(source: source, translation: "")]
    }

    /// 翻訳モード: 訳が文末まで確定した分だけ、原文と**文ごとに**組にして確定する
    ///
    /// 訳は原文より後に届くので、原文が文末まで確定していても訳を待つ。
    /// 訳の n 文目は原文の n 文目と組にする。以前は「原文の最後の文末まで」を 1 行にしていたため、
    /// 原文 2 文が確定した後に訳が 1 文ずつ届くと、1 文目の訳に原文 2 文が付き、
    /// 2 文目の訳が次の原文と組になって 1 文ずつずれていった（2026-10-12 レビュー指摘）。
    /// 切れ目（`<end>`）まで訳が揃ったら、文の数が合わなくても残りを全部最後の行に寄せる（落とさない）。
    private mutating func takeTranslatedLines() -> [Event] {
        let translation = Self.clean(finalTranslation)
        // 訳されない言語（日本語など）だけの発話は、訳を待たずに原文だけで確定する
        if !finalSourceAwaitsTranslation, translation.isEmpty, pendingTranslation.isEmpty {
            let source = Self.clean(finalSource)
            let endsSentence = source.last.map { Self.sentenceEndings.contains($0) } ?? false
            guard endReached || endsSentence else { return [] }
            finalSource = ""
            endReached = false
            return [.confirmed(source: source, translation: "")]
        }

        let (sources, sourceTail) = Self.splitSentences(finalSource)
        let (translations, translationTail) = Self.splitSentences(finalTranslation)
        var lines = zip(sources, translations).map { (source: $0, translation: $1) }
        let restSources = Array(sources.dropFirst(lines.count))
        var restTranslations = Array(translations.dropFirst(lines.count))

        let restSource = (restSources + [sourceTail]).joined()
        let restTranslation = (restTranslations + [translationTail]).joined()
        // 切れ目まで訳が揃った（残りの原文が無いか、残りの訳がある）: 組にならなかった残りを
        // 全部 1 行にまとめる。残りの原文に訳がまだ 1 文字も無いなら、その訳を待つ（下の else）
        if endReached, pendingTranslation.isEmpty,
           Self.clean(restSource).isEmpty || !Self.clean(restTranslation).isEmpty {
            if Self.clean(restSource).isEmpty, !lines.isEmpty {
                lines[lines.count - 1].translation += restTranslation
            } else if !Self.clean(restSource).isEmpty || !Self.clean(restTranslation).isEmpty {
                lines.append((source: restSource, translation: restTranslation))
            }
            finalSource = ""
            finalTranslation = ""
            endReached = false
            finalSourceAwaitsTranslation = false
        } else {
            // 原文の文が尽きたのに訳の文が余った（1 文が 2 文に訳された）。訳は原文より先に
            // 来ないので、余りは直前の原文の訳として扱う（次の文へずれ込ませない）
            if restSources.isEmpty, Self.clean(sourceTail).isEmpty, !restTranslations.isEmpty, !lines.isEmpty {
                lines[lines.count - 1].translation += restTranslations.joined()
                restTranslations = []
            }
            guard !lines.isEmpty else { return [] }
            finalSource = restSource
            finalTranslation = (restTranslations + [translationTail]).joined()
            // 持ち越した文は、まだ訳が届いていない前提で扱う
            finalSourceAwaitsTranslation = !Self.clean(finalSource).isEmpty
        }
        return lines.map { .confirmed(source: Self.clean($0.source), translation: Self.clean($0.translation)) }
    }

    /// 文末で区切る（区切った文の並びと、文末まで来ていない残り）
    ///
    /// 英語の "." は直後が空白か末尾のときだけ文末とみなす（"3.5" や "U.S" の途中で切らない）。
    /// 全角の「。！？」は直後に空白が来ないので常に文末とみなす。
    static func splitSentences(_ text: String) -> (sentences: [String], tail: String) {
        var sentences: [String] = []
        var current = ""
        var index = text.startIndex
        while index < text.endIndex {
            let character = text[index]
            current.append(character)
            let next = text.index(after: index)
            if sentenceEndings.contains(character),
               next == text.endIndex || text[next].isWhitespace || !character.isASCII {
                sentences.append(current)
                current = ""
            }
            index = next
        }
        return (sentences, current)
    }

    /// 前後の空白を落とす（Soniox のトークンは先頭に空白を含む）
    private static func clean(_ text: String) -> String {
        text.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    // MARK: - 解析

    /// 応答の JSON を読む（読めなければ nil）
    static func parse(_ data: Data) -> Response? {
        guard let root = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return nil }
        var tokens: [Token] = []
        for raw in root["tokens"] as? [[String: Any]] ?? [] {
            guard let text = raw["text"] as? String else { continue }
            tokens.append(Token(
                text: text,
                isFinal: raw["is_final"] as? Bool ?? false,
                isTranslation: (raw["translation_status"] as? String) == "translation",
                awaitsTranslation: (raw["translation_status"] as? String) == "original"
            ))
        }
        return Response(
            tokens: tokens,
            finished: root["finished"] as? Bool ?? false,
            errorCode: (root["error_code"] as? NSNumber)?.intValue,
            errorMessage: root["error_message"] as? String
        )
    }
}
