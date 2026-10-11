/// 字幕のライブ行（最下段）をどう更新するかの判定（純ロジック）
///
/// HUD（AppKit）から切り離して単体テストできるように、判定だけをここへ出している。
/// 2026-10-12 のユーザー指摘「ライブ字幕は邪魔」「遅い」への対処で、
/// ライブ行が古い文へ巻き戻る不具合と、行数が 3 段を超えて映像を隠す問題をここで塞ぐ。
import Foundation

/// ライブ行に届く更新の種類
enum CaptionLiveUpdate: Equatable {
    /// 認識途中の原文（ボラタイル）
    case listening(String)
    /// 確定して翻訳待ちになった原文
    case pendingSource(String)
    /// 認識途中の暫定訳
    case partial(source: String, japanese: String)
    /// 文字起こしモードの認識途中（訳さないので本文そのもの）
    case transcript(String)
}

/// ライブ行・確定行の中身（判定に要る分だけ）
struct CaptionLiveLineText: Equatable {
    var japanese: String
    var source: String
}

/// ライブ行の更新判定
enum CaptionLiveLinePolicy {

    /// 画面に出す行数の上限（確定行＋ライブ行）
    ///
    /// 「確定 2 行＋ライブ 1 行」。確定行が最低表示時間を満たすまで 3 行目として残っている間は、
    /// ライブ行を出さずに 3 段に収める（4 段目を出すと映像を隠しすぎる）。
    static let maxVisibleLines = 3

    /// 確定行がこの数のとき、ライブ行を出してよいか
    static func showsLiveLine(confirmedCount: Int) -> Bool {
        confirmedCount < maxVisibleLines
    }

    /// この更新をライブ行に反映してよいか
    ///
    /// - Parameters:
    ///   - update: 届いた更新
    ///   - current: いまのライブ行（無ければ nil）
    ///   - lastConfirmed: 直前の確定行（無ければ nil）
    /// - Returns: 反映するなら true
    static func accepts(
        _ update: CaptionLiveUpdate,
        current: CaptionLiveLineText?,
        lastConfirmed: CaptionLiveLineText?
    ) -> Bool {
        // 確定した直後に同じ発話の途中結果が届くと、同じ文が確定行とライブ行に 2 行並ぶ
        if let last = lastConfirmed {
            switch update {
            case let .partial(_, japanese) where japanese == last.japanese: return false
            case let .listening(text) where text == last.source: return false
            case let .pendingSource(text) where text == last.source: return false
            case let .transcript(text) where text == last.japanese: return false
            default: break
            }
        }
        switch update {
        case .pendingSource:
            // 翻訳待ちの原文は「確定が溜まっていた古い文」なので、ライブ行に新しい話が
            // 出ているときに上書きすると字幕が巻き戻る。空のときの穴埋めにだけ使う。
            guard let current else { return true }
            return current.japanese.isEmpty && current.source.isEmpty
        case .listening:
            // 暫定訳が出ている間は生の原文で上書きしない（訳→原文→訳 とちらつくため）。
            // 次の暫定訳は 0.18 秒間隔で届くので、ここで待っても遅れは小さい。
            return current?.japanese.isEmpty ?? true
        case .partial, .transcript:
            return true
        }
    }
}

/// 短い確定行を直前の行へまとめるかの判定（純ロジック）
///
/// 確定行が上限（2 行）まで出ていて、いちばん古い行がまだ読み終わる時間（`max(3秒, 文字数×0.15秒)`）を
/// 満たしていないときに次の行が来ると、行数の上限で古い行を早く押し出すことになる。Soniox は 1 回の応答で
/// 短い文を何行も確定させるので、相槌のような短い行が続くとこれが起きやすい。
/// 新しい行を待たせると字幕が遅れるので、待たせずに直前の行へ足して行数を増やさない。
/// 最低表示時間の式は変えない（まとめた行は、足した分の読む時間だけ表示を延ばす）。
enum CaptionLineMergePolicy {

    /// これより短い行はまとめる候補（日本語なら一息で読める長さ）
    static let shortLineCharacters = 24
    /// まとめた後の上限（字幕の幅に 1〜2 行で収まる長さ。超えるなら別の行にする）
    static let maxMergedCharacters = 48

    /// 新しい確定行を直前の行へまとめるか
    ///
    /// - Parameters:
    ///   - newText: 新しい行の本文
    ///   - lastText: 直前の確定行の本文（まとめられる行が無ければ nil）
    ///   - activeCount: いま出ている確定行の数（消えかけを除く）
    ///   - oldestHasMetMinimum: いちばん古い行が最低表示時間を満たしたか
    ///   - maxLines: 確定行の上限
    static func shouldMerge(
        newText: String,
        lastText: String?,
        activeCount: Int,
        oldestHasMetMinimum: Bool,
        maxLines: Int
    ) -> Bool {
        guard let lastText, !lastText.isEmpty, !newText.isEmpty else { return false }
        // 足しても上限に収まる、または古い行を読み終わっているなら普通に積めばよい
        guard activeCount >= maxLines, !oldestHasMetMinimum else { return false }
        guard newText.count < shortLineCharacters || lastText.count < shortLineCharacters else { return false }
        return join(lastText, newText).count <= maxMergedCharacters
    }

    /// 2 つの本文をつなぐ（境目が英数字・記号なら空白を挟む。日本語どうしはそのままつなぐ）
    static func join(_ first: String, _ second: String) -> String {
        guard !first.isEmpty else { return second }
        guard !second.isEmpty else { return first }
        guard let tail = first.last, let head = second.first else { return first + second }
        let needsSpace = (tail.isASCII && !tail.isWhitespace) || (head.isASCII && !head.isWhitespace)
        return needsSpace ? first + " " + second : first + second
    }
}
