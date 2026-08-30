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
    private var pending: [String: (text: String, lastChanged: Date)] = [:]

    /// 話者ごとに最後に確定した本文
    ///
    /// Meet は言い終わった字幕をしばらく画面に残す。確定して `pending` から外したあと、
    /// **同じ文がまだ見えているだけ**で新しい発言として拾い直すと、議事録に同じ行が
    /// 何度も並ぶ（実測で気づいた穴）。直前に確定した文と同じなら無視する。
    private var lastSettled: [String: String] = [:]

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

        for entry in entries {
            let text = entry.text.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !text.isEmpty else { continue }
            let speaker = entry.speaker.trimmingCharacters(in: .whitespacesAndNewlines)
            let key = speaker.isEmpty ? Self.unknownSpeaker : speaker

            guard let current = pending[key] else {
                // 直前に確定した文がまだ画面に残っているだけなら拾い直さない
                if lastSettled[key] == text { continue }
                pending[key] = (text, now)
                continue
            }
            if text == current.text {
                continue  // 変化なし。settle を過ぎたら下のループで確定する
            }
            if text.hasPrefix(current.text) {
                pending[key] = (text, now)  // 同じ発言が伸びている
            } else {
                // 別の発言に切り替わった。前の発言をここで確定する。
                settled.append(Line(speaker: Self.displaySpeaker(key), text: current.text))
                lastSettled[key] = current.text
                pending[key] = (text, now)
            }
        }

        // 伸びが止まったものを確定する
        for (key, line) in pending where now.timeIntervalSince(line.lastChanged) >= settleSeconds {
            settled.append(Line(speaker: Self.displaySpeaker(key), text: line.text))
            lastSettled[key] = line.text
            pending.removeValue(forKey: key)
        }
        return settled
    }

    /// 確定待ちを全部吐き出す（退出時・終了時に取りこぼさないため）
    mutating func flush() -> [Line] {
        let lines = pending.map { Line(speaker: Self.displaySpeaker($0.key), text: $0.value.text) }
        for line in pending { lastSettled[line.key] = line.value.text }
        pending.removeAll()
        return lines
    }

    /// 内部キーを表示用の話者名に戻す
    private static func displaySpeaker(_ key: String) -> String? {
        key == unknownSpeaker ? nil : key
    }
}
