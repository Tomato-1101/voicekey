/// Meet の字幕から「確定した発言」を取り出す
///
/// Meet の字幕は**書きながら伸びる**（「こんに」→「こんにちは」→「こんにちは今日は」）うえに、
/// 伸びる途中で**書き直される**（"hello everyone" → "Hello everyone, today"、「良いです」→「いいですね」）。
/// 見えたものをそのまま書くと同じ発言が何度も議事録に載るので、書き直しは「同じ発言の改訂」として
/// 追いかけ、「伸びが止まった」「ブロックが画面から消えた（別の発言に置き換わった）」ときだけ 1 行を確定させる。
///
/// 以前は「先頭一致しない変化＝別の発言」として即確定していたため、Meet が大文字化や言い直しで
/// 文頭を書き換えるたびに、途中版と完成版が二重に議事録へ載っていた。
///
/// DOM の読み取り（`MeetBotScripts.captionScript`）とは分けてある。
/// ここは時刻と文字列だけを見る純粋なロジックなので、ブラウザ無しで回帰テストできる。
import Foundation

/// 字幕の確定判定
struct CaptionSettleTracker {

    /// 確定した 1 行
    struct Line: Equatable {
        /// 話者名（取れなかったときは nil）
        let speaker: String?
        /// 本文
        let text: String
    }

    /// 画面から読んだ 1 件
    struct Entry {
        let speaker: String
        let text: String
    }

    /// この秒数だけ更新が止まったら確定とみなす
    ///
    /// 短すぎると 1 つの発言が細切れになり、長すぎると議事録への反映が遅れる。
    /// Meet の字幕はおよそ 1〜2 秒おきに伸びるので、2.5 秒あれば「言い終わった」と判断できる。
    private let settleSeconds: TimeInterval

    /// 字幕欄まるごと（region）の経路で、変化し続けていても途中で確定させる間隔
    ///
    /// region は表示欄の生テキストなので、話が途切れないまま古い行が画面から押し出されると
    /// 一度も確定しないまま失われる。15 秒ごとに「ここまで」を確定して取りこぼしを防ぐ
    /// （続きは前回確定分との差分だけを書くので、二重にはならない）。
    private static let regionForceSeconds: TimeInterval = 15

    /// 話者が取れなかったときの内部キー
    private static let unknownSpeaker = "\u{0000}unknown"

    /// 書き直しとみなす編集量の上限（元の文の長さに対する割合）
    ///
    /// Meet の書き直しは大文字化・句読点・数語の言い直しが中心で、文の 3 割を超えて変わることはまず無い。
    /// これより大きく違えば別の発言として扱う。
    private static let revisionRatio = 0.3

    /// これより短い文は、正規化した先頭一致のときだけ同じ発言とみなす
    ///
    /// 「はい」と「では」のような短い文は 1 文字違うだけで割合が 5 割になり、割合での判定が効かない。
    private static let minimumFuzzyLength = 4

    /// 確定待ちのブロック（画面に出た順）
    ///
    /// 同じ話者のブロックが同時に 2 つ見えることがある（言い終わった前のブロックを残したまま次を足す）ので、
    /// 話者ごとに 1 枠ではなく、ブロックごとに持つ。
    /// `emitted` は「同じブロックのうち、もう議事録に書いた部分」。確定後に同じブロックが
    /// 伸び続けたとき（同じ人が間を置いて話し続けたとき）に、伸びた分だけを書くために持つ。
    private struct Pending {
        let key: String
        var text: String
        var lastChanged: Date
        let emitted: String?
    }

    private var pending: [Pending] = []

    /// 話者ごとに「確定済みで、直前の読み取りでもまだ画面に見えていた本文」
    ///
    /// Meet は言い終わった字幕をしばらく画面に残し、同じ人が続けて話すと前のブロックを
    /// 残したまま次のブロックを足す。確定済みの文が**見え続けているだけ**なら拾い直さない。
    /// 画面から消えたら外すので、同じ言葉を後でもう一度言ったときはちゃんと書かれる。
    ///
    /// 以前は「最後に確定した 1 文」だけを覚えていたため、同じ話者のブロックが 2 つ見えると
    /// 交互に「別の発言に切り替わった」と判定し、読み取りのたびに 2 行ずつ重複して書いていた（B1）。
    private var settledOnScreen: [String: Set<String>] = [:]

    /// 字幕欄まるごとの経路の確定待ち（`ingestRegion` 専用）
    private var regionPending: (text: String, firstChanged: Date, lastChanged: Date)?
    /// 字幕欄まるごとの経路で、最後に確定させた欄の全文
    private var regionSettledText: String?

    /// - Parameter settleSeconds: 確定とみなすまでの無変化時間
    init(settleSeconds: TimeInterval = 2.5) {
        self.settleSeconds = settleSeconds
    }

    /// 画面の字幕を 1 回分取り込み、確定した行を返す
    ///
    /// 確定するのは次の 2 つだけ:
    /// - 一定時間（`settleSeconds`）変化が無い
    /// - 同じ話者の字幕は見えているのに、そのブロックがどれにも対応しない（画面から消えて別の発言に置き換わった）
    ///
    /// 話者のブロックが 1 つも見えないとき（読み取りの一時的な空振りもある）は時間経過だけで確定する。
    /// 話者が替わったときも、前の話者のブロックは伸びが止まるので時間経過で確定する。
    ///
    /// - Parameters:
    ///   - entries: いま画面に出ている字幕
    ///   - now: 取り込んだ時刻
    /// - Returns: 確定した行（無ければ空）
    mutating func ingest(_ entries: [Entry], now: Date) -> [Line] {
        var settled: [Line] = []
        var visible: [String: Set<String>] = [:]
        var matched = Set<Int>()
        var keysSeen = Set<String>()

        for entry in entries {
            let text = entry.text.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !text.isEmpty else { continue }
            let speaker = entry.speaker.trimmingCharacters(in: .whitespacesAndNewlines)
            let key = speaker.isEmpty ? Self.unknownSpeaker : speaker
            visible[key, default: []].insert(text)
            keysSeen.insert(key)

            // 確定済みの文がまだ見えているだけ（前のブロックが残っている）なら何もしない。
            // ここで確定待ちを確定させると、2 つのブロックを交互に書く重複になる。
            if settledOnScreen[key]?.contains(text) == true { continue }

            // 同じ話者の確定待ちのうち、まだ今回対応させていないものと照合する。
            // 完全一致を先に探すのは、同じ話者のブロックが 2 つ見えているときに取り違えないため。
            let candidates = pending.indices.filter { pending[$0].key == key && !matched.contains($0) }
            if let index = candidates.first(where: { pending[$0].text == text }) {
                matched.insert(index)  // 変化なし。settle を過ぎたら下で確定する
                continue
            }
            if let index = candidates.first(where: { Self.isRevision(old: pending[$0].text, new: text) }) {
                matched.insert(index)
                pending[index].text = text  // 同じ発言が伸びた・書き直された
                pending[index].lastChanged = now
                continue
            }
            pending.append(
                Pending(key: key, text: text, lastChanged: now, emitted: Self.emittedPart(of: text, among: settledOnScreen[key]))
            )
            matched.insert(pending.count - 1)
        }

        // 確定させるもの: 伸びが止まったもの、または同じ話者の字幕は見えているのにどれにも対応しなかったもの
        // （そのブロックが画面から消えて別の発言に置き換わった）
        var remaining: [Pending] = []
        for (index, block) in pending.enumerated() {
            let quiet = now.timeIntervalSince(block.lastChanged) >= settleSeconds
            let replaced = !matched.contains(index) && keysSeen.contains(block.key)
            if quiet || replaced {
                if let line = settle(key: block.key, text: block.text, emitted: block.emitted) {
                    settled.append(line)
                }
            } else {
                remaining.append(block)
            }
        }
        pending = remaining

        // 画面から消えた確定済みの文は忘れる（同じ言葉を後でもう一度言ったら書けるように）
        for key in Array(settledOnScreen.keys) {
            let remaining = settledOnScreen[key, default: []].intersection(visible[key] ?? [])
            settledOnScreen[key] = remaining.isEmpty ? nil : remaining
        }
        return settled
    }

    /// 字幕欄まるごとの生テキストを 1 回分取り込み、確定した部分を返す（代替経路）
    ///
    /// 話者ごとの字幕ブロックが取れないとき、`captionScript` は字幕欄の全文を 1 件で返す。
    /// 全文には言い終わった古い行も含まれ続けるので、確定のたびに**前回確定した全文から新しく増えた末尾だけ**を書く。
    /// 以前は全文を 1 件のブロックとして扱ったため、欄が伸びるたびに先頭からの全文が繰り返し議事録へ載っていた。
    ///
    /// - Parameters:
    ///   - text: 字幕欄の全文
    ///   - now: 取り込んだ時刻
    /// - Returns: 確定した行（話者は取れないので nil）
    mutating func ingestRegion(_ text: String, now: Date) -> [Line] {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        if !trimmed.isEmpty {
            if let current = regionPending {
                if current.text != trimmed { regionPending = (trimmed, current.firstChanged, now) }
            } else if trimmed != regionSettledText {
                regionPending = (trimmed, now, now)
            }
        }
        guard let current = regionPending else { return [] }
        let quiet = now.timeIntervalSince(current.lastChanged) >= settleSeconds
        let tooLong = now.timeIntervalSince(current.firstChanged) >= Self.regionForceSeconds
        guard quiet || tooLong else { return [] }
        regionPending = nil
        return settleRegion(current.text)
    }

    /// 確定待ちを全部吐き出す（退出時・終了時に取りこぼさないため）
    mutating func flush() -> [Line] {
        let waiting = pending
        pending.removeAll()
        var lines = waiting.compactMap { settle(key: $0.key, text: $0.text, emitted: $0.emitted) }
        if let region = regionPending {
            regionPending = nil
            lines += settleRegion(region.text)
        }
        return lines
    }

    /// 1 件を確定させる（既に書いた部分があれば、その続きだけを返す）
    private mutating func settle(key: String, text: String, emitted: String?) -> Line? {
        settledOnScreen[key, default: []].insert(text)
        var body = text
        if let emitted {
            body = Self.continuation(of: text, after: emitted) ?? text
        }
        guard !body.isEmpty else { return nil }
        return Line(speaker: Self.displaySpeaker(key), text: body)
    }

    /// 字幕欄の全文を確定させる（前回確定した全文から増えた末尾だけを返す）
    private mutating func settleRegion(_ text: String) -> [Line] {
        let previous = regionSettledText
        regionSettledText = text
        guard let body = Self.newTail(of: text, after: previous), !body.isEmpty else { return [] }
        return [Line(speaker: nil, text: body)]
    }

    /// 新しく見えた本文が、確定済みの文の続き（同じブロックが伸びた・書き直されたもの）なら、その確定済みの文を返す
    private static func emittedPart(of text: String, among settled: Set<String>?) -> String? {
        settled?.filter { continuation(of: text, after: $0) != nil }.max { $0.count < $1.count }
    }

    /// 内部キーを表示用の話者名に戻す
    private static func displaySpeaker(_ key: String) -> String? {
        key == unknownSpeaker ? nil : key
    }

    // MARK: - 書き直しの判定（純粋関数）

    /// `new` が `old` と同じ発言の伸び・書き直しか
    ///
    /// 大文字小文字・句読点・空白を無視して比べ、`old` 全体が `new` の文頭に
    /// 3 割以内の編集で重なれば同じ発言とみなす（伸びた・書き直された）。
    /// Meet が末尾を少し削って書き直す場合（`new` の方が短い）も、6 割以上残っていれば同じ発言とみなす。
    static func isRevision(old: String, new: String) -> Bool {
        let oldChars = normalized(old).map(\.char)
        let newChars = normalized(new).map(\.char)
        if oldChars.isEmpty || newChars.isEmpty { return false }
        if prefixAlignment(base: oldChars, in: newChars) != nil { return true }
        guard newChars.count * 10 >= oldChars.count * 6 else { return false }
        return prefixAlignment(base: newChars, in: oldChars) != nil
    }

    /// `text` が `prefix` の続き（書き直しを許す）なら、`prefix` に当たる部分より後ろを返す
    ///
    /// - Returns: 続きの部分（前後の句読点・空白は落とす。伸びていなければ空文字）。続きでなければ nil
    static func continuation(of text: String, after prefix: String) -> String? {
        let base = normalized(prefix).map(\.char)
        let target = normalized(text)
        guard !base.isEmpty, !target.isEmpty,
              let end = prefixAlignment(base: base, in: target.map(\.char)) else { return nil }
        return rest(of: text, target: target, end: end)
    }

    /// 字幕欄の全文 `text` のうち、前回確定した全文 `previous` より後ろに増えた部分を返す
    ///
    /// 字幕欄は古い行が上へ押し出されて消えるので、`previous` の**末尾**（最大 60 文字）が
    /// `text` のどこにあるかを近似一致で探し、その後ろを返す。見つからなければ全部が新しい。
    static func newTail(of text: String, after previous: String?) -> String? {
        guard let previous else { return text }
        let target = normalized(text)
        let previousChars = normalized(previous).map(\.char)
        guard !previousChars.isEmpty, !target.isEmpty else { return text }
        // 前回の頭が押し出されて消えていると長い目印は見つからないので、末尾を短くしながら探す
        // （8 文字より短い目印は偶然の一致が増えるので使わない）
        var length = min(previousChars.count, 60)
        while true {
            let anchor = Array(previousChars.suffix(length))
            if let end = substringAlignment(pattern: anchor, in: target.map(\.char)) {
                return rest(of: text, target: target, end: end)
            }
            guard length > 8 else { return text }
            length = max(8, length / 2)
        }
    }

    /// 正規化後の位置 `end` より後ろを、元の文字列から切り出す
    private static func rest(of text: String, target: [(char: Character, offset: Int)], end: Int) -> String {
        let original = Array(text)
        let cut = end == 0 ? 0 : target[end - 1].offset + 1
        let tail = String(original[cut...])
        return tail.trimmingCharacters(in: .whitespacesAndNewlines.union(.punctuationCharacters))
    }

    /// 比較用に、文字と数字だけを小文字で取り出す（元の文字列での位置も持つ）
    private static func normalized(_ text: String) -> [(char: Character, offset: Int)] {
        Array(text).enumerated().compactMap { offset, character in
            guard character.unicodeScalars.contains(where: { CharacterSet.alphanumerics.contains($0) }) else { return nil }
            return (Character(character.lowercased()), offset)
        }
    }

    /// `base` 全体を `text` の文頭に重ねたときの最小編集量で、許容範囲内なら `text` 側の終わり位置を返す
    ///
    /// `text` の末尾は自由（伸びた分は数えない）。同じ編集量なら終わり位置は長い方を選ぶ
    /// （既に書いた文字を「続き」として二重に書かないため）。
    private static func prefixAlignment(base: [Character], in text: [Character]) -> Int? {
        // 共通の文頭は編集量 0 なので先に飛ばす（長い字幕でも比較を軽く保つ）
        var common = 0
        while common < base.count, common < text.count, base[common] == text[common] { common += 1 }
        if common == base.count { return common }
        guard base.count >= minimumFuzzyLength else { return nil }
        let rowBase = Array(base[common...])
        let rowText = Array(text[common...])
        let limit = Int(Double(base.count) * revisionRatio)
        guard let (distance, end) = alignment(pattern: rowBase, in: rowText, freeStart: false),
              distance <= limit else { return nil }
        return common + end
    }

    /// `pattern` が `text` のどこかに許容範囲内の編集量で現れれば、その終わり位置を返す（最も後ろの一致）
    private static func substringAlignment(pattern: [Character], in text: [Character]) -> Int? {
        let limit = pattern.count < minimumFuzzyLength ? 0 : Int(Double(pattern.count) * revisionRatio)
        guard let (distance, end) = alignment(pattern: pattern, in: text, freeStart: true),
              distance <= limit else { return nil }
        return end
    }

    /// 半大域アラインメントの編集距離（`pattern` は全体、`text` は末尾が自由）
    ///
    /// - Parameter freeStart: `text` の先頭も自由にする（途中に現れるのを探す）
    /// - Returns: 最小の編集量と、そのときの `text` 側の終わり位置（同点なら後ろ）
    private static func alignment(pattern: [Character], in text: [Character], freeStart: Bool) -> (Int, Int)? {
        let n = pattern.count
        let m = text.count
        if n == 0 { return (0, 0) }
        // previous[j] = pattern の先頭 i 文字を text の先頭 j 文字（freeStart なら任意の位置から j 文字目まで）に合わせる最小編集量
        var previous = (0...m).map { freeStart ? 0 : $0 }
        var current = [Int](repeating: 0, count: m + 1)
        for i in 1...n {
            current[0] = i
            if m > 0 {
                for j in 1...m {
                    let substitution = previous[j - 1] + (pattern[i - 1] == text[j - 1] ? 0 : 1)
                    current[j] = min(substitution, previous[j] + 1, current[j - 1] + 1)
                }
            }
            swap(&previous, &current)
        }
        var best = (Int.max, 0)
        for j in 0...m where previous[j] <= best.0 { best = (previous[j], j) }
        return best
    }
}
