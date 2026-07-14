//
//  NumeralNormalizer.swift
//  文字起こし確定テキストの数字表記を半角アラビア数字へ寄せる純関数（副作用なし）
//
//  Whisper prompt（Groq/OpenAI）や Deepgram の smart_format で拾いきれない
//  「漢数字・全角」を貼付直前に補正する最終防衛。安全側に振り、普通の日本語語彙
//  （一人・二階・十時・一番・五月雨 等）を壊さないよう最小限の 2 変換のみ行う。
//  Windows 版 numeral_normalizer.py と規則・範囲を完全一致させること。
//
//  変換規則:
//   1. 全角数字（０-９）・全角英字（Ａ-Ｚ / ａ-ｚ）→ 半角。全角記号・句読点は変換しない。
//   2. 位取りを含まない漢数字（〇一二三四五六七八九）が 2 文字以上連続する並びだけ
//      アラビア数字へ変換（例: 三五八〇九一 → 358091）。単独の漢数字や位取り
//      （十・百・千・万）は変換しない（「一人」「二階」「十時」等の普通の語を守るため）。
//

import Foundation

enum NumeralNormalizer {

    /// 漢数字 → アラビア数字（位取りの十・百・千・万は含めない＝連続並びの読み上げ数字だけを対象）。
    private static let kanjiDigits: [Character: Character] = [
        "〇": "0", "一": "1", "二": "2", "三": "3", "四": "4",
        "五": "5", "六": "6", "七": "7", "八": "8", "九": "9",
    ]

    /// 確定テキストの数字表記を半角へ正規化する（冪等）。
    static func normalize(_ s: String) -> String {
        guard !s.isEmpty else { return s }
        var out = String()
        out.reserveCapacity(s.count)
        var run: [Character] = []  // 連続する漢数字を一時的に溜めるバッファ

        // 直前まで溜めた漢数字を確定する。2 文字以上ならアラビア数字化、1 文字ならそのまま。
        func flushRun() {
            if run.count >= 2 {
                for c in run { out.append(kanjiDigits[c]!) }
            } else {
                out.append(contentsOf: run)
            }
            run.removeAll(keepingCapacity: true)
        }

        for c in s {
            if kanjiDigits[c] != nil {
                run.append(c)  // 連続漢数字を溜める（確定は非漢数字が来た時）
                continue
            }
            flushRun()
            out.append(toHalfwidth(c))
        }
        flushRun()
        return out
    }

    /// 全角数字・全角英字を半角へ変換する（対象外の文字はそのまま返す）。
    private static func toHalfwidth(_ c: Character) -> Character {
        guard c.unicodeScalars.count == 1, let scalar = c.unicodeScalars.first else { return c }
        let v = scalar.value
        let isFullwidth =
            (0xFF10...0xFF19).contains(v)   // 全角数字 ０-９
            || (0xFF21...0xFF3A).contains(v)  // 全角大文字 Ａ-Ｚ
            || (0xFF41...0xFF5A).contains(v)  // 全角小文字 ａ-ｚ
        if isFullwidth, let half = UnicodeScalar(v - 0xFEE0) {
            return Character(half)
        }
        return c
    }
}
