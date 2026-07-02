//
//  SlotConfigMigrationTests.swift
//  SlotConfig の decode マイグレーション（旧保存値 → 新 2 択）の単体テスト
//
//  文字起こしモードを 2 択（リアルタイム=deepgram / スタンダード=groq）に絞ったため、
//  選択肢から外した保存値（旧「高精度」= elevenlabs・openai）は groq へ移行する。
//  deepgram/groq は維持する。enum の case は decode 互換と EL の内部利用のため残す。
//  純関数（SlotConfig.init(from:)）だけを検証するので UserDefaults 不要。
//  Windows 版 tests/test_config_manager.py の release 制約テストと同じ観点。
//

import XCTest
@testable import voicekey

final class SlotConfigMigrationTests: XCTestCase {

    /// JSON 文字列を SlotConfig へ decode する（保存済みデータの再現）
    private func decode(_ json: String) throws -> SlotConfig {
        try JSONDecoder().decode(SlotConfig.self, from: Data(json.utf8))
    }

    // 旧「高精度」= elevenlabs を保存していたスロットはスタンダード(groq)へ移行する
    func testElevenLabsMigratesToGroq() throws {
        let slot = try decode(
            #"{"hotkey":["alt_r"],"mode":"toggle","backend":"elevenlabs","model":"scribe_v1","prompt":""}"#
        )
        XCTAssertEqual(slot.backend, .groq)
        // モデルは移行先(groq)の推奨へ揃う（scribe_v1 は groq の既知モデルではない）
        XCTAssertEqual(slot.model, Backend.groq.defaultModel)
        // モード・ホットキーは保持される
        XCTAssertEqual(slot.mode, .toggle)
        XCTAssertEqual(slot.hotkey, ["alt_r"])
    }

    // 選択肢から外した openai もスタンダード(groq)へ移行する
    func testOpenAIMigratesToGroq() throws {
        let slot = try decode(
            #"{"hotkey":["f5"],"mode":"hold","backend":"openai","model":"gpt-4o-mini-transcribe","prompt":""}"#
        )
        XCTAssertEqual(slot.backend, .groq)
        XCTAssertEqual(slot.model, Backend.groq.defaultModel)
    }

    // 選択肢に残る deepgram(リアルタイム)は維持し、当該バックエンドの保存モデルも保持する
    func testDeepgramPreserved() throws {
        let slot = try decode(
            #"{"hotkey":["cmd_r"],"mode":"hold","backend":"deepgram","model":"nova-3","prompt":""}"#
        )
        XCTAssertEqual(slot.backend, .deepgram)
        XCTAssertEqual(slot.model, "nova-3")
    }

    // 選択肢に残る groq(スタンダード)は維持し、当該バックエンドの保存モデルも保持する
    func testGroqPreserved() throws {
        let slot = try decode(
            #"{"hotkey":["cmd_r"],"mode":"toggle","backend":"groq","model":"whisper-large-v3-turbo","prompt":""}"#
        )
        XCTAssertEqual(slot.backend, .groq)
        XCTAssertEqual(slot.model, "whisper-large-v3-turbo")
    }

    // バックエンドは維持されても、保存モデルがそのバックエンドの既知モデルでなければ推奨へ揃える
    func testModelRealignedWhenNotInKnownModels() throws {
        let slot = try decode(
            #"{"hotkey":["cmd_r"],"mode":"hold","backend":"deepgram","model":"scribe_v1","prompt":""}"#
        )
        XCTAssertEqual(slot.backend, .deepgram)
        XCTAssertEqual(slot.model, Backend.deepgram.defaultModel)
    }

    // MARK: - モード別整形既定（v1.8）

    // リアルタイム(deepgram)は速度全振りのため整形既定 OFF、スタンダード(groq)ほかは既定 ON
    func testDefaultFormatEnabledByBackend() {
        XCTAssertFalse(Backend.deepgram.defaultFormatEnabled)
        XCTAssertTrue(Backend.groq.defaultFormatEnabled)
        XCTAssertTrue(Backend.elevenlabs.defaultFormatEnabled)
        XCTAssertTrue(Backend.openai.defaultFormatEnabled)
    }

    // 初回起動: 既存ユーザーの deepgram スロット（formatEnabled=true 明示保存）を既定 OFF へ矯正する
    @MainActor
    func testModeDefaultsMigrationForcesDeepgramFormatOff() throws {
        let suite = "voicekey.test.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suite)!
        defer { defaults.removePersistentDomain(forName: suite) }

        let slot = SlotConfig(hotkey: ["cmd_r"], mode: .hold, backend: .deepgram,
                              model: "nova-3", prompt: "", formatEnabled: true)
        defaults.set(try JSONEncoder().encode(slot), forKey: "slot1")

        let store = ConfigStore(defaults: defaults)
        XCTAssertFalse(store.slot1.formatEnabled)  // 矯正されて OFF
        XCTAssertTrue(defaults.bool(forKey: "didMigrateModeDefaultsV18"))  // フラグが立つ
    }

    // 二度目は矯正しない＝ユーザーが deepgram で整形 ON に戻したら尊重する
    @MainActor
    func testModeDefaultsMigrationRunsOnlyOnce() throws {
        let suite = "voicekey.test.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suite)!
        defer { defaults.removePersistentDomain(forName: suite) }

        // すでにマイグレーション済み。ユーザーは deepgram の整形を ON に戻している
        defaults.set(true, forKey: "didMigrateModeDefaultsV18")
        let slot = SlotConfig(hotkey: ["cmd_r"], mode: .hold, backend: .deepgram,
                              model: "nova-3", prompt: "", formatEnabled: true)
        defaults.set(try JSONEncoder().encode(slot), forKey: "slot1")

        let store = ConfigStore(defaults: defaults)
        XCTAssertTrue(store.slot1.formatEnabled)  // 尊重されて ON のまま
    }
}
