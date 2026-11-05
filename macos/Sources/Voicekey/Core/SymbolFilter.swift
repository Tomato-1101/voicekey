//
//  SymbolFilter.swift
//  入力文から「残す記号」以外の記号を消す純関数（副作用なし・決定的）
//
//  音声認識や翻訳の結果に混じる ！？「」（）… などを貼付直前に落とす。
//  Windows 版 symbol_filter.py と規則・グループ定義を完全一致させること。
//
//  規則:
//   - 削除候補は下の `groups` に載っている文字だけ。それ以外（- / @ # ~ 〜 ー など）は一切触らない。
//   - 「残す記号」に選ばれたグループの文字は消さない。
//   - 壊さないための例外: ASCII 英数字に挟まれた . , :（3.5・1,000・10:30・example.com）と、
//     ASCII 英字に挟まれた ' ’（don't・it’s）は消さない。
//   - 削除で半角スペースが 2 つ並んだら 1 つに詰め、行頭・行末に残った半角スペースは落とす。
//     削除と無関係なスペース・改行・その他の整形はしない。
//

import Foundation

enum SymbolFilter {

    /// 記号グループ（設定画面の表示順）。id は設定に保存される。
    struct Group {
        let id: String
        let name: String
        let characters: String
    }

    static let groups: [Group] = [
        Group(id: "touten", name: "読点", characters: "、"),
        Group(id: "kuten", name: "句点", characters: "。"),
        Group(id: "comma", name: "カンマ", characters: ",，"),
        Group(id: "period", name: "ドット", characters: ".．"),
        Group(id: "exclamation", name: "感嘆符", characters: "!！"),
        Group(id: "question", name: "疑問符", characters: "?？"),
        Group(id: "nakaguro", name: "中黒", characters: "・"),
        Group(id: "ellipsis", name: "三点リーダー", characters: "…‥"),
        Group(id: "colon", name: "コロン", characters: ":："),
        Group(id: "semicolon", name: "セミコロン", characters: ";；"),
        Group(id: "kagikakko", name: "かぎかっこ", characters: "「」『』"),
        Group(id: "parentheses", name: "丸かっこ", characters: "()（）"),
        Group(id: "brackets", name: "角かっこ", characters: "[]［］【】〔〕"),
        Group(id: "quotes", name: "引用符", characters: "\"“”'‘’"),
    ]

    /// 既定で残すグループ（読点・句点・カンマ・ドット）
    static let defaultKeepGroups = ["comma", "period", "touten", "kuten"]

    /// 英数字に挟まれていたら残す ASCII 記号
    private static let innerKept: Set<Unicode.Scalar> = [".", ",", ":"]
    /// 英字に挟まれていたら残すアポストロフィ
    private static let apostrophes: Set<Unicode.Scalar> = ["'", "\u{2019}"]

    private static func isAsciiLetter(_ s: Unicode.Scalar?) -> Bool {
        guard let v = s?.value else { return false }
        return (0x41...0x5A).contains(v) || (0x61...0x7A).contains(v)
    }

    private static func isAsciiAlnum(_ s: Unicode.Scalar?) -> Bool {
        guard let v = s?.value else { return false }
        return isAsciiLetter(s) || (0x30...0x39).contains(v)
    }

    /// 記号を消す。`enabled` が false なら完全に素通し。
    static func filter(_ text: String, enabled: Bool = true, keepGroups: [String] = defaultKeepGroups) -> String {
        guard enabled, !text.isEmpty else { return text }
        let keep = Set(keepGroups)
        var remove = Set<Unicode.Scalar>()
        for g in groups where !keep.contains(g.id) {
            remove.formUnion(g.characters.unicodeScalars)
        }
        if remove.isEmpty { return text }

        let src = Array(text.unicodeScalars)
        var out = String.UnicodeScalarView()
        // 直前の出力以降に削除があったか（スペースの詰め・落としの判断に使う）
        var deletedSinceOutput = false

        func atLineStart() -> Bool {
            guard let last = out.last else { return true }
            return last == "\n" || last == "\r"
        }
        func trimTrailingSpaces() {
            while out.last == " " { out.removeLast() }
        }

        for (i, ch) in src.enumerated() {
            if remove.contains(ch) {
                let prev: Unicode.Scalar? = i > 0 ? src[i - 1] : nil
                let next: Unicode.Scalar? = i + 1 < src.count ? src[i + 1] : nil
                let keepInner = innerKept.contains(ch) && isAsciiAlnum(prev) && isAsciiAlnum(next)
                let keepApostrophe = apostrophes.contains(ch) && isAsciiLetter(prev) && isAsciiLetter(next)
                if !(keepInner || keepApostrophe) {
                    deletedSinceOutput = true
                    continue
                }
            }
            if ch == " " {
                if deletedSinceOutput && (atLineStart() || out.last == " ") { continue }
            } else if ch == "\n" || ch == "\r" {
                if deletedSinceOutput { trimTrailingSpaces() }
            }
            out.append(ch)
            deletedSinceOutput = false
        }
        if deletedSinceOutput { trimTrailingSpaces() }
        return String(out)
    }
}
