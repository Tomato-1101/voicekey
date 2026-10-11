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

import Sparkle
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

    // MARK: - 自動の裏確認のネット断

    /// Sparkle は appcast の取得失敗を SUDownloadError で 2 段包み、URL エラーを奥に入れて渡す
    private func wrappedOfflineError() -> NSError {
        let url = NSError(domain: NSURLErrorDomain, code: NSURLErrorNotConnectedToInternet)
        let inner = NSError(domain: "SUSparkleErrorDomain", code: 2001, userInfo: [NSUnderlyingErrorKey: url])
        return NSError(domain: "SUSparkleErrorDomain", code: 2001,
                       userInfo: [NSLocalizedDescriptionKey: "更新情報を取得できませんでした", NSUnderlyingErrorKey: inner])
    }

    // 自動の裏確認でのネット断は失敗として出さない
    func testBackgroundCheckOfflineIsSilent() {
        XCTAssertNil(UpdaterController.failureMessage(for: wrappedOfflineError(), isBackgroundCheck: true))
        let timeout = NSError(domain: NSURLErrorDomain, code: NSURLErrorTimedOut)
        XCTAssertNil(UpdaterController.failureMessage(for: timeout, isBackgroundCheck: true))
    }

    // 手動確認・ダウンロード中（裏確認ではない）のネット断は出す
    func testManualOrDownloadOfflineIsReported() {
        XCTAssertEqual(UpdaterController.failureMessage(for: wrappedOfflineError(), isBackgroundCheck: false),
                       "更新情報を取得できませんでした")
    }

    // 裏確認でも、ネット断以外の失敗（署名不一致など）は出す
    func testBackgroundCheckRealFailureIsReported() {
        let signature = NSError(domain: "SUSparkleErrorDomain", code: 3001,
                                userInfo: [NSLocalizedDescriptionKey: "署名が一致しません"])
        XCTAssertEqual(UpdaterController.failureMessage(for: signature, isBackgroundCheck: true), "署名が一致しません")
    }

    // 確認の開始で前回の失敗表示を消すデリゲートが、Sparkle の名前どおりに実装されている
    func testClearsFailureWhenCheckStarts() {
        let selector = #selector(SPUUpdaterDelegate.updater(_:mayPerform:))
        XCTAssertTrue(UpdaterController.instancesRespond(to: selector))
    }

    // MARK: - 更新を待たせる「会議の記録中」

    // 字幕を表示しているだけ（議事録を保存しない）なら更新を待たせない
    func testCaptionWithoutTranscriptDoesNotCountAsMeeting() {
        XCTAssertFalse(AppController.isMeetingBeingRecorded(captionActive: true, savesTranscript: false, botActive: false))
        XCTAssertTrue(AppController.isMeetingBeingRecorded(captionActive: true, savesTranscript: true, botActive: false))
        XCTAssertFalse(AppController.isMeetingBeingRecorded(captionActive: false, savesTranscript: true, botActive: false))
        XCTAssertTrue(AppController.isMeetingBeingRecorded(captionActive: false, savesTranscript: false, botActive: true))
    }
}
