/// Meet の字幕から「確定した発言」を取り出す
///
/// Meet の字幕は**書きながら伸びる**（「こんに」→「こんにちは」→「こんにちは今日は」）。
/// 見えたものをそのまま書くと同じ発言が何度も議事録に載るので、
/// 「伸びが止まった」「別の発言に切り替わった」の 2 つを合図に 1 行を確定させる。
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

    /// 話者が取れなかったときの内部キー
    private static let unknownSpeaker = "\u{0000}unknown"

    /// 話者ごとの確定待ち
    ///
    /// `emitted` は「同じブロックのうち、もう議事録に書いた文頭」。確定後に同じブロックが
    /// 伸び続けたとき（同じ人が間を置いて話し続けたとき）に、伸びた分だけを書くために持つ。
    private var pending: [String: (text: String, lastChanged: Date, emitted: String?)] = [:]

    /// 話者ごとに「確定済みで、直前の読み取りでもまだ画面に見えていた本文」
    ///
    /// Meet は言い終わった字幕をしばらく画面に残し、同じ人が続けて話すと前のブロックを
    /// 残したまま次のブロックを足す。確定済みの文が**見え続けているだけ**なら拾い直さない。
    /// 画面から消えたら外すので、同じ言葉を後でもう一度言ったときはちゃんと書かれる。
    ///
    /// 以前は「最後に確定した 1 文」だけを覚えていたため、同じ話者のブロックが 2 つ見えると
    /// 交互に「別の発言に切り替わった」と判定し、読み取りのたびに 2 行ずつ重複して書いていた（B1）。
    private var settledOnScreen: [String: Set<String>] = [:]

    /// - Parameter settleSeconds: 確定とみなすまでの無変化時間
    init(settleSeconds: TimeInterval = 2.5) {
        self.settleSeconds = settleSeconds
    }

    /// 画面の字幕を 1 回分取り込み、確定した行を返す
    ///
    /// - Parameters:
    ///   - entries: いま画面に出ている字幕
    ///   - now: 取り込んだ時刻
    /// - Returns: 確定した行（無ければ空）
    mutating func ingest(_ entries: [Entry], now: Date) -> [Line] {
        var settled: [Line] = []
        var visible: [String: Set<String>] = [:]

        for entry in entries {
            let text = entry.text.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !text.isEmpty else { continue }
            let speaker = entry.speaker.trimmingCharacters(in: .whitespacesAndNewlines)
            let key = speaker.isEmpty ? Self.unknownSpeaker : speaker
            visible[key, default: []].insert(text)

            // 確定済みの文がまだ見えているだけ（前のブロックが残っている）なら何もしない。
            // ここで確定待ちを確定させると、2 つのブロックを交互に書く重複になる。
            if settledOnScreen[key]?.contains(text) == true { continue }

            guard let current = pending[key] else {
                pending[key] = (text, now, Self.emittedPrefix(of: text, among: settledOnScreen[key]))
                continue
            }
            if text == current.text {
                continue  // 変化なし。settle を過ぎたら下のループで確定する
            }
            if text.hasPrefix(current.text) {
                pending[key] = (text, now, current.emitted)  // 同じ発言が伸びている
            } else {
                // 別の発言に切り替わった。前の発言をここで確定する。
                if let line = settle(key: key, text: current.text, emitted: current.emitted) {
                    settled.append(line)
                }
                pending[key] = (text, now, Self.emittedPrefix(of: text, among: settledOnScreen[key]))
            }
        }

        // 伸びが止まったものを確定する
        for (key, line) in pending where now.timeIntervalSince(line.lastChanged) >= settleSeconds {
            pending.removeValue(forKey: key)
            if let settledLine = settle(key: key, text: line.text, emitted: line.emitted) {
                settled.append(settledLine)
            }
        }

        // 画面から消えた確定済みの文は忘れる（同じ言葉を後でもう一度言ったら書けるように）
        for key in Array(settledOnScreen.keys) {
            let remaining = settledOnScreen[key, default: []].intersection(visible[key] ?? [])
            settledOnScreen[key] = remaining.isEmpty ? nil : remaining
        }
        return settled
    }

    /// 確定待ちを全部吐き出す（退出時・終了時に取りこぼさないため）
    mutating func flush() -> [Line] {
        let waiting = pending
        pending.removeAll()
        return waiting.compactMap { settle(key: $0.key, text: $0.value.text, emitted: $0.value.emitted) }
    }

    /// 1 件を確定させる（既に書いた文頭があれば、その続きだけを返す）
    private mutating func settle(key: String, text: String, emitted: String?) -> Line? {
        settledOnScreen[key, default: []].insert(text)
        var body = text
        if let emitted, text.hasPrefix(emitted) {
            body = String(text.dropFirst(emitted.count)).trimmingCharacters(in: .whitespacesAndNewlines)
        }
        guard !body.isEmpty else { return nil }
        return Line(speaker: Self.displaySpeaker(key), text: body)
    }

    /// 新しく見えた本文が、確定済みの文の続き（同じブロックが伸びたもの）なら、その確定済み部分を返す
    private static func emittedPrefix(of text: String, among settled: Set<String>?) -> String? {
        settled?.filter { text.count > $0.count && text.hasPrefix($0) }.max { $0.count < $1.count }
    }

    /// 内部キーを表示用の話者名に戻す
    private static func displaySpeaker(_ key: String) -> String? {
        key == unknownSpeaker ? nil : key
    }
}
