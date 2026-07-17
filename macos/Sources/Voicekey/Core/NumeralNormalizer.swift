//
//  NumeralNormalizer.swift
//  文字起こし確定テキストの数字表記を半角アラビア数字へ寄せる純関数（副作用なし・冪等）
//
//  Whisper prompt（Groq/OpenAI）や Deepgram の smart_format で拾いきれない
//  「漢数字・全角」を貼付直前に補正する最終防衛。LLM を使わず決定的・マイクロ秒で動く。
//  Windows 版 numeral_normalizer.py と規則・範囲を完全一致させること。
//
//  変換規則（v2）:
//   0. 全角英数字（０-９Ａ-Ｚａ-ｚ）→ 半角。全角記号・句読点は変換しない。
//   1. 数字漢字集合 N = { 〇 零 一 二 三 四 五 六 七 八 九 十 百 千 万 億 兆 }。
//   2. 保護リスト W（数字漢字で始まる語）を、数字ランの先頭にアンカーして照合する。
//      ラン先頭 i で text[i..<i+len(W)] == W なら、そのランは変換せずそのまま出す
//      （部分一致でなく先頭アンカー＝「十二人」の中の「二人」で「十二」を壊さないため）。
//   3. N の極大連続ラン R を走査:
//      - 長さ≥2 かつ全て裸数字（〇零一〜九）→ 各桁を置換（二〇二六→2026）。
//      - 長さ≥2 で位取りを含む → 日本語数詞としてパースして整数化（千二百三十四→1234）。
//        パース不能ならそのまま出力（安全側）。
//      - 長さ1（単独漢数字）→ convertCounter=true かつ直後 1 文字が助数詞なら変換
//        （三時→3時・十時→10時）。該当しなければそのまま（千葉・六本木 を守る）。
//   4. カタカナ「ゼロ」は数字（ASCII/全角/漢数字）に隣接するときだけ 0/〇 に寄せる
//      （1234567ゼロ→12345670・ゼロ九〇→090）。単独の「ゼロから」等は語として温存。
//

import Foundation

enum NumeralNormalizer {

    /// 裸の数字漢字 → 桁値（〇零＝0。位取り・グループ乗数は含めない）。
    private static let bareDigits: [Character: Int] = [
        "〇": 0, "零": 0, "一": 1, "二": 2, "三": 3, "四": 4,
        "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
    ]

    /// 位取り（万未満グループ内の副乗数）: 十=10・百=100・千=1000。
    private static let smallUnits: [Character: Int] = ["十": 10, "百": 100, "千": 1000]

    /// グループ乗数: 万=10^4・億=10^8・兆=10^12。
    private static let bigUnits: [Character: Int] = [
        "万": 10_000, "億": 100_000_000, "兆": 1_000_000_000_000,
    ]

    /// 単独漢数字（長さ1・助数詞つき）の値。裸数字＋位取り＋グループ乗数を単体の値として扱う。
    private static let singleValues: [Character: Int] = [
        "〇": 0, "零": 0, "一": 1, "二": 2, "三": 3, "四": 4,
        "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
        "十": 10, "百": 100, "千": 1000,
        "万": 10_000, "億": 100_000_000, "兆": 1_000_000_000_000,
    ]

    /// 数字漢字集合 N（裸数字 ∪ 位取り ∪ グループ乗数）。
    private static let numeralChars: Set<Character> = Set(singleValues.keys)

    /// 助数詞集合 COUNTER（安全・高頻度。地名で誤爆しやすい 本/反/条/丁/目/州/国 は除外）。
    private static let counters: Set<Character> = [
        "時", "分", "秒", "人", "名", "個", "円", "年", "月", "日", "回", "歳", "才",
        "度", "台", "冊", "枚", "杯", "匹", "頭", "件", "品", "番", "位", "階", "週", "泊", "章", "話",
    ]

    /// 確定テキストの数字表記を半角へ正規化する（冪等・純関数）。
    /// - Parameters:
    ///   - s: 対象テキスト
    ///   - enabled: マスタースイッチ（false なら完全パススルー）
    ///   - convertCounter: 単独漢数字＋助数詞ルールのゲート（位取り≥2 はマスターのみで常時変換）
    ///   - protectWords: 変換しない語（数字漢字で始まる語だけをラン先頭にアンカー照合）
    /// - Returns: 正規化後テキスト
    static func normalize(
        _ s: String,
        enabled: Bool = true,
        convertCounter: Bool = true,
        protectWords: Set<String> = []
    ) -> String {
        // マスター OFF は一切触らず完全パススルー（全角半角化もしない）
        guard enabled, !s.isEmpty else { return s }

        // 数字漢字で始まる保護語だけを [Character] 化して前処理（照合時に毎回配列化しない）
        let protectPatterns: [[Character]] = protectWords.compactMap { word in
            let chars = Array(word)
            guard let first = chars.first, numeralChars.contains(first) else { return nil }
            return chars
        }

        // カタカナ「ゼロ」を数字文脈でのみ 0/〇 へ寄せる（電話番号末尾の「…ゼロ」など。
        // 単独の語「ゼロから」等は数字に隣接しないので温存される）。
        let chars = foldKatakanaZero(Array(s))
        let n = chars.count
        var out = String()
        out.reserveCapacity(n)
        var i = 0

        while i < n {
            let c = chars[i]
            // 数字漢字でなければ全角→半角だけ掛けて通す
            guard numeralChars.contains(c) else {
                out.append(toHalfwidth(c))
                i += 1
                continue
            }

            // 数字漢字の極大連続ラン [i, j)
            var j = i
            while j < n, numeralChars.contains(chars[j]) { j += 1 }
            let run = Array(chars[i..<j])

            // 保護: ラン先頭にアンカーして保護語と照合（一致したらラン全体をそのまま出す）
            if isProtected(chars, at: i, patterns: protectPatterns) {
                out.append(contentsOf: run)
                i = j
                continue
            }

            if run.count >= 2 {
                if run.allSatisfy({ bareDigits[$0] != nil }) {
                    // 全て裸数字 → 各桁を置換（読み上げ数字列）
                    for ch in run { out.append(Character(String(bareDigits[ch]!))) }
                } else if let value = parseJapaneseNumeral(run) {
                    // 位取りを含む → 日本語数詞としてパースして整数化
                    out.append(contentsOf: String(value))
                } else {
                    out.append(contentsOf: run)  // パース不能はそのまま（安全側）
                }
            } else {
                // 長さ1（単独漢数字）: convertCounter かつ直後が助数詞のときだけ変換
                let single = run[0]
                if convertCounter, j < n, counters.contains(chars[j]), let v = singleValues[single] {
                    out.append(contentsOf: String(v))
                } else {
                    out.append(single)
                }
            }
            i = j
        }
        return out
    }

    /// ラン先頭 i にアンカーして、いずれかの保護語と前方一致するか判定する。
    private static func isProtected(_ chars: [Character], at i: Int, patterns: [[Character]]) -> Bool {
        for w in patterns {
            let end = i + w.count
            guard end <= chars.count else { continue }
            if Array(chars[i..<end]) == w { return true }
        }
        return false
    }

    /// ASCII/全角数字か、または漢数字 N か（「ゼロ」が数字文脈にあるかの判定に使う）。
    private static func isDigitContext(_ c: Character) -> Bool {
        if numeralChars.contains(c) { return true }
        guard c.unicodeScalars.count == 1, let sc = c.unicodeScalars.first else { return false }
        let v = sc.value
        return (0x30...0x39).contains(v) || (0xFF10...0xFF19).contains(v)  // 0-9 / ０-９
    }

    /// カタカナ「ゼロ」を数字文脈でのみアラビア数字へ寄せる前処理。
    /// STT は数字の読み上げを算用数字にしても末尾等の「ゼロ」だけカタカナで残すことがある。
    /// 数字（ASCII/全角/漢数字）に隣接する「ゼロ」だけを対象にし、隣が漢数字なら「〇」
    /// （後段の漢数字ラン処理に載せて 090 等へ）、それ以外は「0」に置換する。連鎖
    /// （ゼロゼロ九 → 〇〇九 → 009）は反復で解消する。単独の「ゼロ」（ゼロから 等）は温存。
    private static func foldKatakanaZero(_ input: [Character]) -> [Character] {
        var arr = input
        var changed = true
        while changed {
            changed = false
            var i = 0
            while i + 1 < arr.count {
                if arr[i] == "ゼ", arr[i + 1] == "ロ" {
                    let prevKanji = i > 0 && numeralChars.contains(arr[i - 1])
                    let nextKanji = i + 2 < arr.count && numeralChars.contains(arr[i + 2])
                    let prevDigit = i > 0 && isDigitContext(arr[i - 1])
                    let nextDigit = i + 2 < arr.count && isDigitContext(arr[i + 2])
                    if prevDigit || nextDigit {
                        // 隣が漢数字なら「〇」（漢数字ランに載せる）、ASCII/全角数字なら「0」
                        let repl: Character = (prevKanji || nextKanji) ? "〇" : "0"
                        arr.replaceSubrange(i...(i + 1), with: [repl])
                        changed = true
                        continue  // 置換後の同位置から再評価して連鎖を解消
                    }
                }
                i += 1
            }
        }
        return arr
    }

    /// 位取りを含む日本語数詞を整数へパースする（万/億/兆でグループ分けし副乗数を加算）。
    /// 例: 千二百三十四=1234・二十三=23・三万五千=35000。パース不能・曖昧は nil。
    private static func parseJapaneseNumeral(_ run: [Character]) -> Int? {
        var total = 0    // 万/億/兆で区切った上位グループの確定和
        var section = 0  // 現在のグループ（万未満）の値
        var number = 0   // 直前に読んだ裸数字（十百千 の係数）

        for c in run {
            if let d = bareDigits[c] {
                number = number * 10 + d
            } else if let unit = smallUnits[c] {
                // 十/百/千: 直前の裸数字が無ければ 1（十＝10）
                section += (number == 0 ? 1 : number) * unit
                number = 0
            } else if let unit = bigUnits[c] {
                // 万/億/兆: グループを確定して乗じる（単独なら万＝10000）
                section += number
                total += (section == 0 ? 1 : section) * unit
                section = 0
                number = 0
            } else {
                return nil  // N 以外は来ない想定だが安全側
            }
        }
        let result = total + section + number
        return result == 0 ? nil : result
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
