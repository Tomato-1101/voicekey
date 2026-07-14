//
//  SlotConfigTests.swift
//  ホットキースロットの「未割り当て」状態（空トークン配列）の判定・表示・後方互換の単体テスト
//
//  未割り当ての内部表現は空トークン配列 `[]`。旧設定（hotkey キー欠落）も decode で `[]` に
//  なり、そのまま「未割り当て」として扱える（後方互換）。判定 isAssigned と表示 hotkeyLabel は
//  値型 SlotConfig 上の純関数なので、実 Keychain / 実キーイベントに触れず検証できる。
//

import XCTest
@testable import voicekey

@MainActor
final class SlotConfigTests: XCTestCase {

    // MARK: - 判定・表示（純関数）

    // 未割り当て（空）は isAssigned=false・表示は「未割り当て」
    func testUnassignedLabelAndFlag() {
        let slot = SlotConfig(hotkey: [], mode: .hold, backend: .groq, model: "x", prompt: "")
        XCTAssertFalse(slot.isAssigned)
        XCTAssertEqual(slot.hotkeyLabel, "未割り当て")
    }

    // 修飾キー単独の割り当ては isAssigned=true・記号で表示
    func testAssignedSingleModifierLabel() {
        let slot = SlotConfig(hotkey: ["cmd_r"], mode: .hold, backend: .groq, model: "x", prompt: "")
        XCTAssertTrue(slot.isAssigned)
        XCTAssertEqual(slot.hotkeyLabel, "右⌘")
    }

    // 複数キーの組み合わせは "+" 連結で表示
    func testAssignedComboLabel() {
        let slot = SlotConfig(hotkey: ["ctrl_l", "space"], mode: .hold, backend: .groq, model: "x", prompt: "")
        XCTAssertTrue(slot.isAssigned)
        XCTAssertEqual(slot.hotkeyLabel, "左⌃+Space")
    }

    // MARK: - 後方互換（decode / round-trip）

    // 旧設定に hotkey キーが無くても decode は失敗せず [] になり「未割り当て」として扱える
    func testDecodeWithoutHotkeyIsUnassigned() throws {
        let json = Data(#"{"mode":"hold","backend":"groq","model":"whisper-large-v3-turbo","prompt":""}"#.utf8)
        let slot = try JSONDecoder().decode(SlotConfig.self, from: json)
        XCTAssertEqual(slot.hotkey, [])
        XCTAssertFalse(slot.isAssigned)
        XCTAssertEqual(slot.hotkeyLabel, "未割り当て")
    }

    // hotkey がある旧設定はそのまま保持される（未割り当て導入で既存割り当てを壊さない）
    func testDecodeWithHotkeyPreserved() throws {
        let json = Data(#"{"hotkey":["alt_r"],"mode":"toggle","backend":"groq","model":"whisper-large-v3-turbo","prompt":""}"#.utf8)
        let slot = try JSONDecoder().decode(SlotConfig.self, from: json)
        XCTAssertEqual(slot.hotkey, ["alt_r"])
        XCTAssertTrue(slot.isAssigned)
        XCTAssertEqual(slot.hotkeyLabel, "右⌥")
    }

    // 空スロットの encode→decode で値が保たれる（保存・読み込みで未割り当てが消えない）。
    // release は decode でモデル整合（当該バックエンドの既知モデルへ揃える）を行うため、
    // round-trip が壊れないよう groq の既定モデルを使う。
    func testEmptyHotkeyEncodeDecodeRoundTrip() throws {
        let original = SlotConfig(
            hotkey: [], mode: .hold, backend: .groq,
            model: Backend.groq.defaultModel, prompt: ""
        )
        let data = try JSONEncoder().encode(original)
        let decoded = try JSONDecoder().decode(SlotConfig.self, from: data)
        XCTAssertEqual(decoded, original)
        XCTAssertFalse(decoded.isAssigned)
    }

    // MARK: - 永続化（ConfigStore 経由・実 UserDefaults に触れない suite 注入）

    // 未割り当てにしたスロットは保存され、作り直したストアでも空のまま（既定に戻らない）。
    // もう片方のスロットは影響を受けない。
    func testUnassignedSlotPersistsAndDoesNotRevertToDefault() {
        let suite = "voicekey.test.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suite)!
        defer { defaults.removePersistentDomain(forName: suite) }

        do {
            let store = ConfigStore(defaults: defaults)
            store.slot2.hotkey = []  // スロット 2 を未割り当てにする
        }

        let reloaded = ConfigStore(defaults: defaults)
        XCTAssertEqual(reloaded.slot2.hotkey, [])            // 空のまま（既定 alt_r に戻らない）
        XCTAssertFalse(reloaded.slot2.isAssigned)
        XCTAssertEqual(reloaded.slot2.hotkeyLabel, "未割り当て")
        // 片方を未割り当てにしても、もう片方の既定割り当ては保持される
        XCTAssertEqual(reloaded.slot1.hotkey, ["cmd_r"])
        XCTAssertTrue(reloaded.slot1.isAssigned)
    }
}
