//
//  ConfigStoreDefaultsTests.swift
//  Phase A で追加した設定フィールドの既定値・保存・読み込みの単体テスト
//
//  操作音・ダッキング・再貼り付けキー・ピル常時表示・Dock 常時表示・サイドノッチ・
//  履歴保存のトグルが正しく永続化されることを検証する（実 UserDefaults に触れないよう suite 注入）。
//

import XCTest
@testable import voicekey

@MainActor
final class ConfigStoreDefaultsTests: XCTestCase {

    /// テスト用の隔離された UserDefaults を作る
    private func makeDefaults() -> (UserDefaults, String) {
        let suite = "voicekey.test.\(UUID().uuidString)"
        return (UserDefaults(suiteName: suite)!, suite)
    }

    // 未保存時の既定値（操作音・ダッキング・履歴・Dock 常時表示＝ON / ピル常時表示＝OFF /
    // サイドノッチ＝ON / 再貼り付け＝⌃⌘V）
    // Dock 常時表示は personal でライブ字幕を統合した際に既定 ON へ変更した（2026-08-10 ユーザー要望）
    func testNewFieldDefaults() {
        let (defaults, suite) = makeDefaults()
        defer { defaults.removePersistentDomain(forName: suite) }

        let store = ConfigStore(defaults: defaults)
        XCTAssertTrue(store.soundEffectsEnabled)
        XCTAssertTrue(store.duckMediaEnabled)
        XCTAssertEqual(store.repasteKey, ["ctrl", "cmd", "v"])
        XCTAssertFalse(store.hudAlwaysVisible)
        XCTAssertTrue(store.dockIconAlwaysVisible)
        XCTAssertTrue(store.sideNotchEnabled)
        XCTAssertTrue(store.historyEnabled)
    }

    // ライブ字幕のミラーが CaptionSettings（正本）と一致して初期化される
    //
    // 字幕設定の正本は UserDefaults.standard 側の caption* キーなので、ここでは
    // **書かずに読むだけ**にする（テストが実ユーザーの字幕設定を書き換えないため）。
    func testCaptionMirrorsReflectCaptionSettings() {
        let (defaults, suite) = makeDefaults()
        defer { defaults.removePersistentDomain(forName: suite) }

        let store = ConfigStore(defaults: defaults)
        XCTAssertEqual(store.captionAutoStart, CaptionSettings.startsOnLaunch)
        XCTAssertEqual(store.captionEngine, CaptionSettings.translationEngine)
        XCTAssertEqual(store.captionFrontmostOnly, CaptionSettings.captureScopeMode == .frontmost)
        XCTAssertEqual(store.captionSpeak, CaptionSettings.speakTranslation)
        XCTAssertEqual(store.captionShowSource, CaptionSettings.showSourceText)
        XCTAssertEqual(store.captionGeminiModel, CaptionSettings.geminiModelID)
        XCTAssertEqual(store.captionGroqModel, CaptionSettings.groqModelID)
        // モデル ID は未設定でも既定値が返る（設定 UI のプレースホルダと一致させるため）
        XCTAssertFalse(store.captionGeminiModel.isEmpty)
        XCTAssertFalse(store.captionGroqModel.isEmpty)
    }

    // 設定 UI（ConfigStore のミラー）を変えると CaptionSettings（正本）へ書き抜ける
    //
    // 正本は UserDefaults.standard 側なので、**元の値を退避して必ず戻す**
    // （テストがユーザーの字幕設定を書き換えたままにしないため）。
    func testCaptionMirrorWritesThroughToCaptionSettings() {
        let (defaults, suite) = makeDefaults()
        defer { defaults.removePersistentDomain(forName: suite) }

        let savedSpeak = CaptionSettings.speakTranslation
        let savedShowSource = CaptionSettings.showSourceText
        let savedGeminiModel = CaptionSettings.geminiModelID
        defer {
            CaptionSettings.speakTranslation = savedSpeak
            CaptionSettings.showSourceText = savedShowSource
            CaptionSettings.geminiModelID = savedGeminiModel
        }

        let store = ConfigStore(defaults: defaults)
        store.captionSpeak = !savedSpeak
        store.captionShowSource = !savedShowSource
        store.captionGeminiModel = "gemini-test-model"

        XCTAssertEqual(CaptionSettings.speakTranslation, !savedSpeak)
        XCTAssertEqual(CaptionSettings.showSourceText, !savedShowSource)
        XCTAssertEqual(CaptionSettings.geminiModelID, "gemini-test-model")

        // 空欄は「既定に戻す」の意味（設定 UI のプレースホルダと揃える）
        store.captionGeminiModel = ""
        XCTAssertEqual(CaptionSettings.geminiModelID, CaptionSettings.defaultGeminiModelID)
    }

    // 変更した値が保存され、作り直したストアで復元される
    func testNewFieldsPersist() {
        let (defaults, suite) = makeDefaults()
        defer { defaults.removePersistentDomain(forName: suite) }

        do {
            let store = ConfigStore(defaults: defaults)
            store.soundEffectsEnabled = false
            store.duckMediaEnabled = false
            store.hudAlwaysVisible = true
            store.dockIconAlwaysVisible = true
            store.sideNotchEnabled = false
            store.historyEnabled = false
            store.repasteKey = ["f5"]
        }

        let reloaded = ConfigStore(defaults: defaults)
        XCTAssertFalse(reloaded.soundEffectsEnabled)
        XCTAssertFalse(reloaded.duckMediaEnabled)
        XCTAssertTrue(reloaded.hudAlwaysVisible)
        XCTAssertTrue(reloaded.dockIconAlwaysVisible)
        XCTAssertFalse(reloaded.sideNotchEnabled)
        XCTAssertFalse(reloaded.historyEnabled)
        XCTAssertEqual(reloaded.repasteKey, ["f5"])
    }

    // 再貼り付けキーを空にしたら「無効」として保存され、既定（⌃⌘V）に戻らない
    func testRepasteKeyEmptyIsDistinctFromDefault() {
        let (defaults, suite) = makeDefaults()
        defer { defaults.removePersistentDomain(forName: suite) }

        do {
            let store = ConfigStore(defaults: defaults)
            store.repasteKey = []
        }

        let reloaded = ConfigStore(defaults: defaults)
        XCTAssertEqual(reloaded.repasteKey, [])  // 空のまま（明示的な無効化が保持される）
    }
}
