//
//  UpdaterPolicyTests.swift
//  自動アップデートの「再起動を待つか」と「失敗として見せるか」の判定の単体テスト
//
//  (1) 会議の記録中（ライブ字幕・Meet ボット）に「更新して再起動」を押すと、記録が途中で切れていた。
//      録音中と同じく終わるまで待つ。
//  (2) 裏の確認・ダウンロードが失敗しても何も出なかった。新版なし・取り消しは失敗に数えず、
//      それ以外はメニューに出す。
//  Sparkle 本体は動かさない（判定は純関数にしてある）。
//

import XCTest

@testable import voicekey

@MainActor
final class UpdaterPolicyTests: XCTestCase {

    func testMeetingRecordingDefersRelaunch() {
        XCTAssertTrue(UpdaterController.shouldDeferRelaunch(
            dictationBusy: false, recordingMeeting: true, bypass: false))
    }

    func testDictationBusyDefersRelaunch() {
        XCTAssertTrue(UpdaterController.shouldDeferRelaunch(
            dictationBusy: true, recordingMeeting: false, bypass: false))
    }

    func testIdleDoesNotDefer() {
        XCTAssertFalse(UpdaterController.shouldDeferRelaunch(
            dictationBusy: false, recordingMeeting: false, bypass: false))
    }

    // アプリ自前の再起動を肩代わりしているときは、もう再起動すると決まっているので待たない
    func testBypassNeverDefers() {
        XCTAssertFalse(UpdaterController.shouldDeferRelaunch(
            dictationBusy: true, recordingMeeting: true, bypass: true))
    }

    // 成功・新版なし・取り消し・「あとで」は失敗として見せない
    func testNonFailuresAreSilent() {
        let domain = "SUSparkleErrorDomain"
        XCTAssertNil(UpdaterController.failureMessage(for: nil))
        XCTAssertNil(UpdaterController.failureMessage(for: NSError(domain: domain, code: 1001)))  // 新版なし
        XCTAssertNil(UpdaterController.failureMessage(for: NSError(domain: domain, code: 4007)))  // 取り消し
        XCTAssertNil(UpdaterController.failureMessage(for: NSError(domain: domain, code: 4008)))  // あとで
    }

    // ダウンロード失敗などは理由を返す（メニューに出す）
    func testRealFailureIsReported() {
        let error = NSError(
            domain: "SUSparkleErrorDomain", code: 2001,
            userInfo: [NSLocalizedDescriptionKey: "ダウンロードに失敗しました"])
        XCTAssertEqual(UpdaterController.failureMessage(for: error), "ダウンロードに失敗しました")
        let network = NSError(domain: NSURLErrorDomain, code: NSURLErrorNotConnectedToInternet)
        XCTAssertNotNil(UpdaterController.failureMessage(for: network))
    }
}
