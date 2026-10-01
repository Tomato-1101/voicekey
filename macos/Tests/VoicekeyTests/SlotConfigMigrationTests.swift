//
//  SlotConfigMigrationTests.swift
//  SlotConfig の decode マイグレーション（旧保存値 → 現在の選択肢）の単体テスト
//
//  2026-10-01（Mac のみ）: Deepgram を選択肢から外して Soniox に置き換え、OpenAI（gpt-transcribe）・
//  Microsoft MAI・ElevenLabs（scribe_v2）を選択肢に加えた。保存済み deepgram は soniox へ、
//  それ以外の選択肢外は groq へ移行する。decode ではモデルは空のときだけ推奨へ戻し、
//  一覧外の自由入力は保持する。廃止モデルの置換は ConfigStore の一回限りの移行（V19）で行う。
//  enum の case は decode 互換と EL の内部利用のため残す。
//  decode の検証は純関数（SlotConfig.init(from:)）だけ。ConfigStore の移行は使い捨ての UserDefaults で検証する。
//  Windows 版 tests/test_config_manager.py の release 制約テストと同じ観点。
//

import XCTest
@testable import voicekey

final class SlotConfigMigrationTests: XCTestCase {

    /// JSON 文字列を SlotConfig へ decode する（保存済みデータの再現）
    private func decode(_ json: String) throws -> SlotConfig {
        try JSONDecoder().decode(SlotConfig.self, from: Data(json.utf8))
    }

    // elevenlabs は選択肢に戻ったので維持し、保存モデル（scribe_v1 も一覧内）も保持する
    func testElevenLabsPreserved() throws {
        let slot = try decode(
            #"{"hotkey":["alt_r"],"mode":"toggle","backend":"elevenlabs","model":"scribe_v1","prompt":""}"#
        )
        XCTAssertEqual(slot.backend, .elevenlabs)
        XCTAssertEqual(slot.model, "scribe_v1")
        // モード・ホットキーは保持される
        XCTAssertEqual(slot.mode, .toggle)
        XCTAssertEqual(slot.hotkey, ["alt_r"])
    }

    // 未知の backend（新しい版で保存した値など）でも decode を失敗させず、groq へ写して
    // ホットキー・モード・プロンプト・整形設定は保持する（失敗すると全部既定に戻ってしまう）
    func testUnknownBackendPreservesOtherFields() throws {
        let slot = try decode(
            #"{"hotkey":["ctrl_l","space"],"mode":"toggle","backend":"future_engine","model":"x-1","prompt":"固有名詞","formatEnabled":false,"formatPresetId":"clean"}"#
        )
        XCTAssertEqual(slot.backend, .groq)
        XCTAssertEqual(slot.model, Backend.groq.defaultModel)
        XCTAssertEqual(slot.hotkey, ["ctrl_l", "space"])
        XCTAssertEqual(slot.mode, .toggle)
        XCTAssertEqual(slot.prompt, "固有名詞")
        XCTAssertFalse(slot.formatEnabled)
        XCTAssertEqual(slot.formatPresetId, "clean")
    }

    // 実ユーザーの保存値（ローカル Apple）が decode → encode で 1 項目も変わらない
    func testRealUserAppleLocalSlotRoundTrips() throws {
        guard #available(macOS 26.0, *) else { throw XCTSkip("apple_local は macOS 26 以降だけの選択肢") }
        let json = #"{"backend":"apple_local","model":"オンデバイス音声認識","hotkey":["alt_r"],"mode":"hold","formatEnabled":false,"formatPresetId":"standard","prompt":""}"#
        let slot = try decode(json)
        XCTAssertEqual(slot.backend, .appleLocal)
        let original = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(json.utf8)) as? NSDictionary)
        let reencoded = try XCTUnwrap(
            JSONSerialization.jsonObject(with: JSONEncoder().encode(slot)) as? NSDictionary
        )
        XCTAssertEqual(reencoded, original)
    }

    // 選択肢から外した deepgram は同じライブ型の soniox へ移行し、モデルも soniox の推奨へ揃う
    func testDeepgramMigratesToSoniox() throws {
        for model in ["nova-3", "nova-2"] {
            let slot = try decode(
                #"{"hotkey":["cmd_r"],"mode":"hold","backend":"deepgram","model":"\#(model)","prompt":"固有名詞"}"#
            )
            XCTAssertEqual(slot.backend, .soniox)
            XCTAssertEqual(slot.model, "stt-rt-v5")
            // バックエンド以外の保存値は保持される
            XCTAssertEqual(slot.hotkey, ["cmd_r"])
            XCTAssertEqual(slot.prompt, "固有名詞")
        }
    }

    // 保存済み soniox はそのまま読める（保存 → 読み込みの往復）
    func testSonioxRoundTrip() throws {
        let original = SlotConfig(hotkey: ["cmd_r"], mode: .hold, backend: .soniox,
                                  model: "stt-rt-v5", prompt: "", formatEnabled: false)
        let decoded = try JSONDecoder().decode(SlotConfig.self, from: JSONEncoder().encode(original))
        XCTAssertEqual(decoded, original)
    }

    // decode 自体は廃止モデルも保存値のまま返す（置換は ConfigStore の一回限りの移行だけが行う）
    func testDecodeKeepsRetiredModelName() throws {
        let slot = try decode(
            #"{"hotkey":["cmd_r"],"mode":"hold","backend":"groq","model":"whisper-large-v3","prompt":""}"#
        )
        XCTAssertEqual(slot.backend, .groq)
        XCTAssertEqual(slot.model, "whisper-large-v3")
    }

    // MARK: - 廃止モデルの一回限りの移行（V19）

    private func storeSlot(_ defaults: UserDefaults, _ key: String, backend: Backend, model: String) throws {
        let slot = SlotConfig(hotkey: ["cmd_r"], mode: .hold, backend: backend,
                              model: model, prompt: "固有名詞", formatEnabled: true)
        defaults.set(try JSONEncoder().encode(slot), forKey: key)
    }

    private func savedSlot(_ defaults: UserDefaults, _ key: String) throws -> SlotConfig {
        try JSONDecoder().decode(SlotConfig.self, from: XCTUnwrap(defaults.data(forKey: key)))
    }

    // 初回起動: 廃止したモデルは、バックエンドを維持したまま推奨へ戻し、両スロットを保存する
    @MainActor
    func testRetiredModelsMigratedOnceToDefault() throws {
        let suite = "voicekey.test.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suite)!
        defer { defaults.removePersistentDomain(forName: suite) }

        try storeSlot(defaults, "slot1", backend: .openai, model: "gpt-4o-mini-transcribe")
        try storeSlot(defaults, "slot2", backend: .groq, model: "whisper-large-v3")

        let store = ConfigStore(defaults: defaults)
        XCTAssertEqual(store.slot1.backend, .openai)
        XCTAssertEqual(store.slot1.model, "gpt-transcribe")
        XCTAssertEqual(store.slot2.backend, .groq)
        XCTAssertEqual(store.slot2.model, "whisper-large-v3-turbo")
        // ほかの保存値は保持される
        XCTAssertEqual(store.slot1.prompt, "固有名詞")
        XCTAssertTrue(defaults.bool(forKey: "didMigrateRetiredModelsV19"))
        // 保存値にも反映されている（次回起動で再び古い名前を読まない）
        XCTAssertEqual(try savedSlot(defaults, "slot1").model, "gpt-transcribe")
        XCTAssertEqual(try savedSlot(defaults, "slot2").model, "whisper-large-v3-turbo")

        // ライブ型・ElevenLabs の廃止モデルも同じ
        let suite2 = "voicekey.test.\(UUID().uuidString)"
        let defaults2 = UserDefaults(suiteName: suite2)!
        defer { defaults2.removePersistentDomain(forName: suite2) }
        try storeSlot(defaults2, "slot1", backend: .openaiLive, model: "gpt-realtime-whisper")
        try storeSlot(defaults2, "slot2", backend: .elevenlabs, model: "scribe_v1_experimental")
        let store2 = ConfigStore(defaults: defaults2)
        XCTAssertEqual(store2.slot1.model, "gpt-live-transcribe")
        XCTAssertEqual(store2.slot2.model, "scribe_v2")
    }

    // 二度目は置換しない＝移行後にユーザーが自由入力で旧モデル名を選び直したら尊重する
    @MainActor
    func testRetiredModelsMigrationRunsOnlyOnce() throws {
        let suite = "voicekey.test.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suite)!
        defer { defaults.removePersistentDomain(forName: suite) }

        defaults.set(true, forKey: "didMigrateRetiredModelsV19")
        try storeSlot(defaults, "slot1", backend: .groq, model: "whisper-large-v3")

        let store = ConfigStore(defaults: defaults)
        XCTAssertEqual(store.slot1.model, "whisper-large-v3")
    }

    // 初回の移行でも、廃止モデルでない自由入力は保持する
    @MainActor
    func testRetiredModelsMigrationKeepsCustomModel() throws {
        let suite = "voicekey.test.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suite)!
        defer { defaults.removePersistentDomain(forName: suite) }

        try storeSlot(defaults, "slot1", backend: .openai, model: "gpt-transcribe-preview-x")
        try storeSlot(defaults, "slot2", backend: .groq, model: "distil-whisper-x")

        let store = ConfigStore(defaults: defaults)
        XCTAssertEqual(store.slot1.model, "gpt-transcribe-preview-x")
        XCTAssertEqual(store.slot2.model, "distil-whisper-x")
        XCTAssertTrue(defaults.bool(forKey: "didMigrateRetiredModelsV19"))
    }

    // 選択肢に残る groq(スタンダード)は維持し、当該バックエンドの保存モデルも保持する
    func testGroqPreserved() throws {
        let slot = try decode(
            #"{"hotkey":["cmd_r"],"mode":"toggle","backend":"groq","model":"whisper-large-v3-turbo","prompt":""}"#
        )
        XCTAssertEqual(slot.backend, .groq)
        XCTAssertEqual(slot.model, "whisper-large-v3-turbo")
    }

    // formatPresetId フィールドが無い旧保存 JSON でも decode が壊れず、既定 standard へフォールバックする
    // （フィールド追加で既存ユーザーのホットキー設定がリセットされないことの保証）
    func testFormatPresetIdDefaultsToStandardWhenMissing() throws {
        let slot = try decode(
            #"{"hotkey":["cmd_r"],"mode":"hold","backend":"groq","model":"whisper-large-v3-turbo","prompt":"","formatEnabled":true}"#
        )
        XCTAssertEqual(slot.formatPresetId, "standard")
        // 他フィールドも従来どおり読める
        XCTAssertEqual(slot.backend, .groq)
        XCTAssertTrue(slot.formatEnabled)
    }

    // 保存済み formatPresetId は尊重される（standard 以外を選んだユーザーの設定が保持される）
    func testFormatPresetIdPreserved() throws {
        let slot = try decode(
            #"{"hotkey":["cmd_r"],"mode":"hold","backend":"groq","model":"whisper-large-v3-turbo","prompt":"","formatPresetId":"clean"}"#
        )
        XCTAssertEqual(slot.formatPresetId, "clean")
    }

    // 一覧外のモデル名（自由入力）は、バックエンドが同じなら再起動しても消さない
    // （以前は「一覧外なら推奨へ」で、自由入力のモデル名が再起動のたびに戻っていた）
    func testCustomModelPreservedWhenBackendUnchanged() throws {
        let slot = try decode(
            #"{"hotkey":["cmd_r"],"mode":"hold","backend":"openai","model":"gpt-transcribe-preview-x","prompt":""}"#
        )
        XCTAssertEqual(slot.backend, .openai)
        XCTAssertEqual(slot.model, "gpt-transcribe-preview-x")
    }

    // 保存モデルが空なら推奨へ揃える
    func testEmptyModelRealignedToDefault() throws {
        let slot = try decode(
            #"{"hotkey":["cmd_r"],"mode":"hold","backend":"azure_mai","model":"","prompt":""}"#
        )
        XCTAssertEqual(slot.backend, .azureMAI)
        XCTAssertEqual(slot.model, "MAI-Transcribe-2")
    }

    // 選択肢の並びと、Deepgram が選択肢から外れていること
    func testSelectableCases() {
        var expected: [Backend] = [.soniox, .openaiLive, .openai, .azureMAI, .elevenlabs, .groq]
        if #available(macOS 26.0, *) { expected.insert(.appleLocal, at: 1) }
        XCTAssertEqual(Backend.selectableCases, expected)
        XCTAssertFalse(Backend.selectableCases.contains(.deepgram))
    }

    // 選択肢から外したモデルが既知モデル一覧に残っていないこと
    func testRetiredModelsNotListed() {
        for backend in Backend.allCases {
            for model in backend.knownModels {
                XCTAssertFalse(Backend.retiredModels.contains(model), "\(backend.rawValue): \(model)")
            }
        }
        XCTAssertEqual(Backend.elevenlabs.knownModels, ["scribe_v2", "scribe_v1"])
    }

    // ハンズフリーで内部利用する ElevenLabs は、既定が scribe_v2 になっても scribe_v1 に固定する
    @MainActor
    func testHandsfreeELModelPinnedToScribeV1() {
        XCTAssertEqual(AppController.handsfreeELModel, "scribe_v1")
        XCTAssertNotEqual(AppController.handsfreeELModel, Backend.elevenlabs.defaultModel)
    }

    // 新規ユーザーの既定: スロット1=Soniox（整形 OFF）、スロット2=Groq
    @MainActor
    func testFreshInstallDefaults() {
        let suite = "voicekey.test.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suite)!
        defer { defaults.removePersistentDomain(forName: suite) }

        let store = ConfigStore(defaults: defaults)
        XCTAssertEqual(store.slot1.backend, .soniox)
        XCTAssertEqual(store.slot1.model, "stt-rt-v5")
        XCTAssertFalse(store.slot1.formatEnabled)  // ライブ型は速度全振り
        XCTAssertEqual(store.slot2.backend, .groq)
        XCTAssertEqual(store.slot2.model, "whisper-large-v3-turbo")
    }

    // MARK: - モード別整形既定（v1.8）

    // ライブ型（deepgram / soniox）は速度全振りのため整形既定 OFF、録音後に送る型は既定 ON
    func testDefaultFormatEnabledByBackend() {
        XCTAssertFalse(Backend.deepgram.defaultFormatEnabled)
        XCTAssertFalse(Backend.soniox.defaultFormatEnabled)
        XCTAssertTrue(Backend.groq.defaultFormatEnabled)
        XCTAssertTrue(Backend.elevenlabs.defaultFormatEnabled)
        XCTAssertTrue(Backend.openai.defaultFormatEnabled)
        XCTAssertTrue(Backend.azureMAI.defaultFormatEnabled)
    }

    // 初回起動: 既存ユーザーの deepgram スロット（formatEnabled=true 明示保存）を既定 OFF へ矯正する。
    // deepgram は decode で soniox へ移行して届くので、移行後の soniox が OFF になる
    @MainActor
    func testModeDefaultsMigrationForcesDeepgramFormatOff() throws {
        let suite = "voicekey.test.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suite)!
        defer { defaults.removePersistentDomain(forName: suite) }

        let slot = SlotConfig(hotkey: ["cmd_r"], mode: .hold, backend: .deepgram,
                              model: "nova-3", prompt: "", formatEnabled: true)
        defaults.set(try JSONEncoder().encode(slot), forKey: "slot1")

        let store = ConfigStore(defaults: defaults)
        XCTAssertEqual(store.slot1.backend, .soniox)
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
