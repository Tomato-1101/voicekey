//
//  HotkeyResyncTests.swift
//  イベントタップ再有効化時の押下状態補正（HotkeyMonitor.tokensToRelease）の単体テスト
//
//  純関数の検証のみ。実キーボード・イベントタップには触れない。
//

import XCTest
@testable import voicekey

final class HotkeyResyncTests: XCTestCase {

    /// 無効化中に右⌥を離した → 離鍵扱いにする（hold 録音が止まらない不具合の本体）
    func testReleasedModifierIsReported() {
        let released = HotkeyMonitor.tokensToRelease(
            pressed: ["alt_r"], currentModifiers: [], isKeyDown: { _ in false })
        XCTAssertEqual(released, ["alt_r"])
    }

    /// まだ握っている修飾キーは離鍵扱いにしない（録音を勝手に切らない）
    func testHeldModifierIsKept() {
        let released = HotkeyMonitor.tokensToRelease(
            pressed: ["alt_r", "cmd_r"], currentModifiers: ["cmd_r"], isKeyDown: { _ in false })
        XCTAssertEqual(released, ["alt_r"])
    }

    /// 左右は別トークン。左が押されていても、右を離していれば右は離鍵扱い
    func testLeftRightAreDistinct() {
        let released = HotkeyMonitor.tokensToRelease(
            pressed: ["shift_r"], currentModifiers: ["shift_l"], isKeyDown: { _ in false })
        XCTAssertEqual(released, ["shift_r"])
    }

    /// 無効化中に新しく押されたキーは対象にしない（戻り値は「離鍵扱い」だけ＝押下通知は出さない）
    func testNewlyPressedKeysAreNotReported() {
        let released = HotkeyMonitor.tokensToRelease(
            pressed: [], currentModifiers: ["alt_r", "fn"], isKeyDown: { _ in true })
        XCTAssertTrue(released.isEmpty)
    }

    /// 修飾キー以外は isKeyDown で判定する。確かめられない（false）キーは安全側＝離鍵扱い
    func testNonModifierUsesKeyState() {
        let released = HotkeyMonitor.tokensToRelease(
            pressed: ["f2", "space", "fn"], currentModifiers: ["fn"],
            isKeyDown: { $0 == "space" })
        XCTAssertEqual(released, ["f2"])
    }

    /// fn は修飾キーとして currentModifiers で判定する（isKeyDown には問い合わせない）
    func testFnIsTreatedAsModifier() {
        var asked: [String] = []
        let released = HotkeyMonitor.tokensToRelease(
            pressed: ["fn"], currentModifiers: [],
            isKeyDown: { asked.append($0); return true })
        XCTAssertEqual(released, ["fn"])
        XCTAssertTrue(asked.isEmpty)
    }
}
