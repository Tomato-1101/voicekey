//
//  TextNormalize.swift
//  文字起こし結果の共通正規化（CJK 隣接スペース除去）
//
//  streaming / REST、Mac / Windows で同じ結果にするための単一実装（#21）。
//  Windows 版 text_utils.strip_cjk_spaces / _is_cjk と規則・範囲を一致させること。
//

import Foundation

enum TextNormalize {

    /// 「前後どちらかが CJK 文字」のスペースだけを除去する。
    ///
    /// Deepgram / ElevenLabs などは日本語出力に単語間スペースを挿入するため、そのまま貼り付けると
    /// 「今日 は 晴れ」のように不自然になる。英単語間のスペース（"GPT 4" 等）は残すので英語表記は壊さない。
    static func stripCJKSpaces(_ s: String) -> String {
        guard s.contains(" ") else { return s }
        let chars = Array(s)
        var out = String()
        out.reserveCapacity(chars.count)
        for (i, c) in chars.enumerated() {
            if c == " " {
                let prev = i > 0 ? chars[i - 1] : nil
                let next = i + 1 < chars.count ? chars[i + 1] : nil
                if (prev.map(isCJK) ?? false) || (next.map(isCJK) ?? false) {
                    continue  // CJK 隣接のスペースは除去
                }
            }
            out.append(c)
        }
        return out
    }

    /// ひらがな・カタカナ・漢字（および全角・半角形）判定。Windows 版 _is_cjk と同範囲。
    static func isCJK(_ c: Character) -> Bool {
        for scalar in c.unicodeScalars {
            let v = scalar.value
            if (0x3040...0x30FF).contains(v)       // ひらがな・カタカナ
                || (0x3400...0x4DBF).contains(v)   // CJK 拡張A
                || (0x4E00...0x9FFF).contains(v)   // CJK 統合漢字
                || (0xF900...0xFAFF).contains(v)   // CJK 互換漢字
                || (0xFF00...0xFFEF).contains(v) { // 全角・半角形
                return true
            }
        }
        return false
    }
}
