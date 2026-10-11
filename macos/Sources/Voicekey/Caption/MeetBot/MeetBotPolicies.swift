/// Meet ボットの「いつ諦めるか・いつ知らせるか」の判断（純粋なロジック）
///
/// ボット本体（`MeetBotService`）は Chrome と画面の状態に振り回されるので、回数・時間の数え方と
/// 通知の文言はここに切り出し、ブラウザ無しで回帰テストできるようにしてある。
/// Meet の画面の文言への依存は `MeetBotScripts.swift`（`MeetBotRules`）だけに置く。
import Foundation
import os

/// 「Chrome が応答しない」が続いた回数を数える
///
/// 1 回の時間切れで止めると、重い画面の切り替わりで誤って終了してしまう。上限回数続いたら諦める。
struct TimeoutStreak {
    /// 何回続いたら諦めるか
    let limit: Int
    /// いま続いている回数
    private(set) var count = 0

    init(limit: Int = 3) {
        self.limit = limit
    }

    /// 1 回分の結果を記録する
    ///
    /// - Parameter timedOut: 今回の命令が時間切れだったか（応答が来たら数え直し）
    /// - Returns: 上限に達した（諦めるべき）か
    mutating func record(timedOut: Bool) -> Bool {
        count = timedOut ? count + 1 : 0
        return count >= limit
    }
}

/// 記録中の会議画面の見張り（外された・終わったの確定）
///
/// 1 回の読み取りで決めると、画面の切り替わり途中の文字を拾って誤判定する。
/// 「外された」は 2 回、それ以外の「会議画面でない」は 3 回続いたら確定する。
struct MeetingPresenceWatch {

    /// 確定した終わり方
    enum Verdict: Equatable {
        /// 会議から外された
        case removed
        /// 会議が終わった（会議画面でなくなった）
        case ended
    }

    /// 「外された」が何回続いたら確定するか
    static let removedLimit = 2
    /// 「会議画面でない」が何回続いたら確定するか（3 秒おきなので約 9 秒）
    static let notInMeetingLimit = 3

    private var removedCount = 0
    private var notInMeetingCount = 0

    /// 1 回分の画面の状態を取り込む
    ///
    /// - Returns: 終わりが確定したら、その種類（まだなら nil）
    mutating func observe(_ status: MeetPageStatus) -> Verdict? {
        switch status {
        case .inMeeting:
            removedCount = 0
            notInMeetingCount = 0
            return nil
        case .removed:
            removedCount += 1
            notInMeetingCount += 1
            return removedCount >= Self.removedLimit ? .removed : nil
        case .denied, .ended, .waitingApproval, .notFound, .other:
            removedCount = 0
            notInMeetingCount += 1
            return notInMeetingCount >= Self.notInMeetingLimit ? .ended : nil
        }
    }
}

/// 「Meet の字幕が読めていない」状態の見張り
///
/// 字幕がオフのまま・Meet の画面が変わった、などで 1 行も取れていないのに、画面は「記録中 0 行」のままだと
/// 本人は会議が終わるまで気づけない。参加してから一定時間ずっと読めなければ 1 回だけ知らせる。
struct CaptionPresenceWatch {

    /// 何秒続いたら知らせるか
    static let warnAfter: TimeInterval = 60

    /// 知らせ・取り消しの合図
    enum Event: Equatable {
        /// 字幕が取れていないと知らせる
        case warn
        /// また取れるようになった（表示を消す）
        case recovered
    }

    /// 字幕が読めなくなった時刻（読めている間は nil）
    private var missingSince: Date?
    /// いま知らせている最中か
    private(set) var warning = false

    /// - Parameter start: 見張りを始めた時刻（参加した時刻）。最初から読めないときもここから数える
    init(start: Date) {
        missingSince = start
    }

    /// 1 回分の読み取り結果を取り込む
    ///
    /// - Parameters:
    ///   - found: 字幕欄が見つかったか
    ///   - now: 読み取った時刻
    /// - Returns: 知らせる・取り消す合図（何もしないなら nil）
    mutating func observe(found: Bool, now: Date) -> Event? {
        if found {
            missingSince = nil
            guard warning else { return nil }
            warning = false
            return .recovered
        }
        let since = missingSince ?? now
        missingSince = since
        guard !warning, now.timeIntervalSince(since) >= Self.warnAfter else { return nil }
        warning = true
        return .warn
    }
}

/// 参加ボタンを押し直す段取り
///
/// Meet のロビーは組み上がるのが遅く、4 秒後に 1 回押すだけでは「ボタンがまだ無い」で終わることがあった。
/// 入室待ちで「その他」の画面が続く間は、数秒おきに上限回数まで押し直す。
struct JoinRetry {
    /// 押す回数の上限（最初の 1 回を含む）
    static let maxAttempts = 5
    /// 押し直す間隔
    static let interval: TimeInterval = 4

    /// 押した回数
    private(set) var attempts = 0
    /// 最後に押した時刻
    private var lastAttempt: Date?
    /// 一度でも参加ボタンが見つかったか
    private(set) var buttonFound = false

    /// いま押すべきか
    ///
    /// - Parameters:
    ///   - status: いまの画面の状態（「その他」＝まだロビーにいる間だけ押す）
    ///   - now: 現在時刻
    func shouldPress(status: MeetPageStatus, now: Date) -> Bool {
        guard status == .other, attempts < Self.maxAttempts else { return false }
        guard let lastAttempt else { return true }
        return now.timeIntervalSince(lastAttempt) >= Self.interval
    }

    /// 押した結果を記録する
    ///
    /// - Parameters:
    ///   - found: 参加ボタン（または名前の入力欄）が見つかって押せたか
    ///   - now: 押した時刻
    mutating func recordPress(found: Bool, now: Date) {
        attempts += 1
        lastAttempt = now
        if found { buttonFound = true }
    }
}

extension MeetBotError {

    /// 入室を待ちきれなかったときの失敗を、実際の状況で出し分ける
    ///
    /// 以前は承認待ちを見たかどうかだけで 2 通りに分けていて、「参加ボタンが無かった」のか
    /// 「承認されなかった」のかが本人に分からなかった。
    ///
    /// - Parameters:
    ///   - waitingSeen: 承認待ちの画面を見たか
    ///   - joinButtonFound: 参加ボタンを一度でも押せたか
    static func joinTimeout(waitingSeen: Bool, joinButtonFound: Bool) -> MeetBotError {
        if waitingSeen { return .approvalTimeout }
        return joinButtonFound ? .couldNotEnter : .joinButtonNotFound
    }
}

/// Meet ボットの通知・状態行の文言
enum MeetBotText {

    /// 記録を閉じたときの通知の文言
    ///
    /// 「保存できなかった」と「保存はできたが 0 行だった」を分ける。以前はどちらも
    /// 「議事録を保存しました（0 行）」で、本人は字幕が取れていなかったのか保存に失敗したのかが分からなかった。
    ///
    /// - Parameters:
    ///   - reason: 閉じた理由（「会議から退出しました」など）
    ///   - savesTranscript: 議事録の保存がオンか
    ///   - fileStarted: この会議の議事録ファイルを作ろうとしたか
    ///   - lines: 書き込みがすべて終わった時点の行数
    ///   - saveError: 最後の書き込みが失敗していれば、その理由
    static func closingMessage(
        reason: String, savesTranscript: Bool, fileStarted: Bool, lines: Int, saveError: String?
    ) -> String {
        if !savesTranscript || !fileStarted { return "\(reason)。議事録の保存はオフでした。" }
        if let saveError { return "\(reason)。議事録を保存できませんでした: \(saveError)" }
        if lines == 0 { return "\(reason)。議事録の記録は 0 行でした（Meet の字幕が出ていたか確かめてください）。" }
        return "\(reason)。議事録を保存しました（\(lines) 行）。"
    }

    /// 字幕が取れていないときの状態行・通知
    static let captionMissing = "字幕が取れていません（Meet の字幕が ON か確認）"
}

/// 音声パイプラインの停止を、上限時間つきで待つ
///
/// 最後の 1 文は停止処理（認識器の締め）の中で確定する。待たずに議事録を閉じると、その 1 文が
/// 閉じた後に届いて捨てられていた。ただし停止が固まったときにボットごと止まらないよう、上限で打ち切る。
enum StopWaiter {

    /// 停止を待つ
    ///
    /// - Parameters:
    ///   - limit: 待つ上限（秒）
    ///   - stop: 停止処理
    /// - Returns: 上限内に停止し終えたか
    static func wait(limit: TimeInterval, _ stop: @escaping @Sendable () async -> Void) async -> Bool {
        await withCheckedContinuation { (continuation: CheckedContinuation<Bool, Never>) in
            // 停止の完了と時間切れのどちらか先に来た方で 1 回だけ返す
            let resumed = OSAllocatedUnfairLock(initialState: false)
            let finish: @Sendable (Bool) -> Void = { value in
                let first = resumed.withLock { done -> Bool in
                    if done { return false }
                    done = true
                    return true
                }
                if first { continuation.resume(returning: value) }
            }
            Task.detached {
                await stop()
                finish(true)
            }
            DispatchQueue.global().asyncAfter(deadline: .now() + limit) { finish(false) }
        }
    }
}
