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
