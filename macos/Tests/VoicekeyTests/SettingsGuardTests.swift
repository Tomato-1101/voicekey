//
//  SettingsGuardTests.swift
//  設定画面の重複キー警告とエラー通知の表示時間の単体テスト
//
//  (1) 2 スロットに同じ録音キーを入れても何も言われず、片方が鳴らない理由が分からなかった。
//      重なり判定（設定画面の警告の根拠）を固定する。
//  (2) エラー通知が 2 秒で消えて読めなかった。エラーだけ 5 秒以上・情報通知は 2 秒のまま。
//

import XCTest

@testable import voicekey

@MainActor
final class SettingsGuardTests: XCTestCase {

    func testSameHotkeyConflicts() {
        XCTAssertTrue(KeyToken.conflicts(["cmd_r"], ["cmd_r"]))
    }

    // 順序が違っても同じ組み合わせ
    func testOrderDoesNotMatter() {
        XCTAssertTrue(KeyToken.conflicts(["cmd", "shift"], ["shift", "cmd"]))
    }

    // 汎用の ⌘ は右⌘ と重なる
    func testGenericModifierOverlapsSided() {
        XCTAssertTrue(KeyToken.conflicts(["cmd"], ["cmd_r"]))
    }

    // 左右が違えば別キー・組み合わせが違えば別・未割り当ては重ならない
    func testDifferentKeysDoNotConflict() {
        XCTAssertFalse(KeyToken.conflicts(["cmd_l"], ["cmd_r"]))
        XCTAssertFalse(KeyToken.conflicts(["cmd_r"], ["cmd_r", "shift"]))
        XCTAssertFalse(KeyToken.conflicts([], []))
    }

    func testErrorNoticeStaysLonger() {
        XCTAssertGreaterThanOrEqual(HudController.noticeDuration(isError: true), 5)
        XCTAssertEqual(HudController.noticeDuration(isError: false), 2)
    }
}
