/// Meet ボットの判断ロジックの回帰テスト（ブラウザ・Chrome・ネットワークなし）
///
/// 実会議でしか起きない分岐（承認待ち・拒否・終了・字幕の状態）と、取り残し Chrome の片付け対象、
/// 会議音声を拾うプロセスの範囲を、純粋な関数として確かめる。
import XCTest

@testable import voicekey

final class MeetBotRulesTests: XCTestCase {

    // MARK: - 参加の世代

    /// 退出や再参加のあとは、古い試行の続きが「無効」と判定される
    func testJoinGenerationInvalidatesOldAttempt() {
        var generation = JoinGeneration()
        let first = generation.begin()
        XCTAssertTrue(generation.isCurrent(first))

        generation.invalidate()  // 退出
        XCTAssertFalse(generation.isCurrent(first))

        let second = generation.begin()  // 再参加
        XCTAssertTrue(generation.isCurrent(second))
        XCTAssertFalse(generation.isCurrent(first))
    }

    /// 続けて参加し直したときも、前の試行は無効になる
    func testJoinGenerationBeginSupersedesPrevious() {
        var generation = JoinGeneration()
        let first = generation.begin()
        let second = generation.begin()
        XCTAssertFalse(generation.isCurrent(first))
        XCTAssertTrue(generation.isCurrent(second))
    }

    // MARK: - 会議画面の状態

    func testPageStatusInMeetingWins() {
        // 会議中は画面の文字（チャットなど）に何が出ていても会議中
        XCTAssertEqual(MeetBotRules.pageStatus(inMeeting: true, text: "通話は終了しました"), .inMeeting)
    }

    func testPageStatusWaitingApproval() {
        XCTAssertEqual(MeetBotRules.pageStatus(inMeeting: false, text: "参加をリクエストしています…"), .waitingApproval)
        XCTAssertEqual(MeetBotRules.pageStatus(inMeeting: false, text: "Asking to be let in..."), .waitingApproval)
    }

    func testPageStatusDenied() {
        XCTAssertEqual(
            MeetBotRules.pageStatus(inMeeting: false, text: "参加リクエストが拒否されました"), .denied
        )
        XCTAssertEqual(
            MeetBotRules.pageStatus(inMeeting: false, text: "Someone in the call denied your request to join"), .denied
        )
    }

    func testPageStatusRemovedAndEnded() {
        XCTAssertEqual(MeetBotRules.pageStatus(inMeeting: false, text: "You've been removed from the meeting"), .removed)
        XCTAssertEqual(MeetBotRules.pageStatus(inMeeting: false, text: "通話から退出しました"), .ended)
        XCTAssertEqual(MeetBotRules.pageStatus(inMeeting: false, text: "The call has ended"), .ended)
    }

    func testPageStatusOther() {
        XCTAssertEqual(MeetBotRules.pageStatus(inMeeting: false, text: "今すぐ参加"), .other)
        XCTAssertEqual(MeetBotRules.pageStatus(inMeeting: false, text: ""), .other)
    }

    // MARK: - 字幕のオンオフ

    /// 字幕が見えていれば押さない（押すとオフになる）
    func testCaptionAlreadyOnWhenRegionVisible() {
        let buttons = [MeetCaptionButton(label: "字幕をオンにする", pressed: nil)]
        XCTAssertEqual(MeetBotRules.captionToggle(buttons: buttons, regionVisible: true, shortcutTried: false), .alreadyOn)
    }

    func testCaptionAlreadyOnWhenPressedOrOffLabel() {
        XCTAssertEqual(
            MeetBotRules.captionToggle(
                buttons: [MeetCaptionButton(label: "Captions", pressed: true)], regionVisible: false, shortcutTried: false
            ),
            .alreadyOn
        )
        XCTAssertEqual(
            MeetBotRules.captionToggle(
                buttons: [MeetCaptionButton(label: "字幕をオフにする", pressed: nil)], regionVisible: false, shortcutTried: false
            ),
            .alreadyOn
        )
    }

    func testCaptionClickWhenOff() {
        XCTAssertEqual(
            MeetBotRules.captionToggle(
                buttons: [MeetCaptionButton(label: "字幕をオンにする", pressed: nil)], regionVisible: false, shortcutTried: false
            ),
            .click("字幕をオンにする")
        )
        XCTAssertEqual(
            MeetBotRules.captionToggle(
                buttons: [MeetCaptionButton(label: "Captions", pressed: false)], regionVisible: false, shortcutTried: false
            ),
            .click("Captions")
        )
    }

    /// 関係ないボタンは押さない。見つからなければショートカットを 1 回だけ試す
    func testCaptionFallsBackToShortcutOnce() {
        let unrelated = [MeetCaptionButton(label: "マイクをオンにする", pressed: false)]
        XCTAssertEqual(MeetBotRules.captionToggle(buttons: unrelated, regionVisible: false, shortcutTried: false), .shortcut)
        XCTAssertEqual(MeetBotRules.captionToggle(buttons: unrelated, regionVisible: false, shortcutTried: true), .unknown)
    }

    // MARK: - ログイン判定

    func testLoggedInWithSessionCookie() {
        let cookies: [[String: Any]] = [["name": "SID", "domain": ".google.com", "expires": -1]]
        XCTAssertTrue(MeetBotRules.isLoggedIn(cookies: cookies))
    }

    func testNotLoggedInWhenExpiredOrOtherDomain() {
        let now = Date(timeIntervalSince1970: 2_000_000_000)
        let expired: [[String: Any]] = [["name": "__Secure-1PSID", "domain": ".google.com", "expires": 1_000_000_000.0]]
        XCTAssertFalse(MeetBotRules.isLoggedIn(cookies: expired, now: now))
        let otherDomain: [[String: Any]] = [["name": "SID", "domain": ".example.com", "expires": -1]]
        XCTAssertFalse(MeetBotRules.isLoggedIn(cookies: otherDomain, now: now))
        let unrelated: [[String: Any]] = [["name": "NID", "domain": ".google.com", "expires": -1]]
        XCTAssertFalse(MeetBotRules.isLoggedIn(cookies: unrelated, now: now))
        XCTAssertFalse(MeetBotRules.isLoggedIn(cookies: [], now: now))
    }

    func testLoggedInWithFutureExpiry() {
        let now = Date(timeIntervalSince1970: 2_000_000_000)
        let cookies: [[String: Any]] = [["name": "SAPISID", "domain": "google.com", "expires": 2_100_000_000.0]]
        XCTAssertTrue(MeetBotRules.isLoggedIn(cookies: cookies, now: now))
    }

    // MARK: - 取り残し Chrome の片付け対象

    /// ボット用プロファイルの Chrome だけを選び、普段使いの Chrome や似た名前のプロファイルは選ばない
    func testStaleChromeOnlyMatchesOurProfile() {
        let profile = "/Users/me/Library/Application Support/voicekey/meetbot-profile"
        let ps = """
          101 /Applications/Google Chrome.app/Contents/MacOS/Google Chrome --remote-debugging-port=9333 --user-data-dir=\(profile) --headless=new
          102 /Applications/Google Chrome.app/Contents/Frameworks/Google Chrome Framework.framework/Helpers/Google Chrome Helper (Renderer).app/Contents/MacOS/Google Chrome Helper (Renderer) --type=renderer --user-data-dir=\(profile)
          200 /Applications/Google Chrome.app/Contents/MacOS/Google Chrome
          201 /Applications/Google Chrome.app/Contents/MacOS/Google Chrome --user-data-dir=\(profile)2 --headless=new
          202 /usr/bin/some-tool --user-data-dir=\(profile)
          203 /Applications/Google Chrome.app/Contents/MacOS/Google Chrome --user-data-dir=/Users/me/Library/Application Support/Google/Chrome
        """
        XCTAssertEqual(ChromeDevTools.staleChromePIDs(psOutput: ps, profilePath: profile), [101, 102])
    }

    /// 引数の最後にプロファイルが来ても拾う
    func testStaleChromeMatchesProfileAtEndOfLine() {
        let profile = "/tmp/meetbot-profile"
        let ps = "  300 /Applications/Google Chrome.app/Contents/MacOS/Google Chrome --user-data-dir=\(profile)\n"
        XCTAssertEqual(ChromeDevTools.staleChromePIDs(psOutput: ps, profilePath: profile), [300])
    }

    // MARK: - 会議音声を拾うプロセスの範囲

    func testProcessTreeDescendant() {
        // 500(Chrome) → 510(Helper) → 520(Audio サービス)
        let parents: [pid_t: pid_t] = [520: 510, 510: 500, 500: 1, 600: 1]
        let parentOf: (pid_t) -> pid_t? = { parents[$0] }
        XCTAssertTrue(ProcessTree.isDescendant(520, of: 500, parentOf: parentOf))
        XCTAssertTrue(ProcessTree.isDescendant(510, of: 500, parentOf: parentOf))
        // 普段使いの Chrome（別の木）は拾わない
        XCTAssertFalse(ProcessTree.isDescendant(600, of: 500, parentOf: parentOf))
        // 親が分からないものも拾わない
        XCTAssertFalse(ProcessTree.isDescendant(700, of: 500, parentOf: parentOf))
    }

    /// 親子関係が循環していても止まる
    func testProcessTreeStopsOnLoop() {
        let parents: [pid_t: pid_t] = [10: 11, 11: 10]
        XCTAssertFalse(ProcessTree.isDescendant(10, of: 500, parentOf: { parents[$0] }))
        XCTAssertFalse(ProcessTree.isDescendant(20, of: 500, parentOf: { _ in 20 }))
    }
}
