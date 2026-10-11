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
