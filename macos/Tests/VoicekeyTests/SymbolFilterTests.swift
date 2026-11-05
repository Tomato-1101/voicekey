//
//  SymbolFilterTests.swift
//  記号の自動削除（SymbolFilter）と、その設定の既定・保存の単体テスト
//
//  期待値の表は Windows 版 tests/test_symbol_filter.py と同じ。両 OS で同じ入力に同じ出力になること。
//

import XCTest
@testable import voicekey

@MainActor
final class SymbolFilterTests: XCTestCase {

    /// 既定設定（残す: 、。, .）での期待値表（両 OS 共通）
    private let defaultTable: [(String, String)] = [
        ("こんにちは！元気？「はい」（笑）…", "こんにちは元気はい笑"),
        ("今日は、晴れ。", "今日は、晴れ。"),
        ("Hello, world.", "Hello, world."),
        ("3.5と1,000と10:30", "3.5と1,000と10:30"),
        ("example.comにアクセス", "example.comにアクセス"),
        ("don't と it’s", "don't と it’s"),
        ("A: B", "A B"),
        ("「はい」と言った。", "はいと言った。"),
        ("a-b/c@d#e~f〜g", "a-b/c@d#e~f〜g"),
        ("ー長音ー", "ー長音ー"),
        ("これは ！ 例です", "これは 例です"),
        ("！ 先頭", "先頭"),
        ("末尾 ！", "末尾"),
        ("一行目 ！\n二行目", "一行目\n二行目"),
        ("元の  二重スペース", "元の  二重スペース"),
        ("'quoted' and \"x\"", "quoted and x"),
        ("【重要】[a](b)", "重要ab"),
        ("A・B；C：D", "ABCD"),
        ("", ""),
    ]

    func testDefaultTable() {
        for (input, expected) in defaultTable {
            XCTAssertEqual(SymbolFilter.filter(input), expected, input)
        }
    }

    func testDisabledIsPassthrough() {
        let text = "こんにちは！元気？「はい」（笑）… A: B"
        XCTAssertEqual(SymbolFilter.filter(text, enabled: false), text)
    }

    func testCustomKeepGroups() {
        // 感嘆符・疑問符・かぎかっこだけ残す
        let keep = ["exclamation", "question", "kagikakko"]
        XCTAssertEqual(
            SymbolFilter.filter("こんにちは！元気？「はい」（笑）…、。", keepGroups: keep),
            "こんにちは！元気？「はい」笑")
        // 空配列なら全グループを消す
        XCTAssertEqual(SymbolFilter.filter("あ、い。う", keepGroups: []), "あいう")
    }

    func testIdempotent() {
        for (input, _) in defaultTable {
            let once = SymbolFilter.filter(input)
            XCTAssertEqual(SymbolFilter.filter(once), once, input)
        }
    }

    func testGroupIdsAreUniqueAndDefaultsExist() {
        let ids = SymbolFilter.groups.map(\.id)
        XCTAssertEqual(Set(ids).count, ids.count)
        XCTAssertEqual(ids.count, 14)
        for id in SymbolFilter.defaultKeepGroups { XCTAssertTrue(ids.contains(id), id) }
    }

    // MARK: - 設定の既定値と保存・読み込み

    func testConfigDefaults() {
        let suite = testDefaultsSuite()
        let defaults = UserDefaults(suiteName: suite)!
        defer { removeTestDefaults(defaults, suite) }
        let store = ConfigStore(defaults: defaults)
        XCTAssertTrue(store.symbolRemovalEnabled)
        XCTAssertEqual(store.symbolKeepGroups, ["comma", "period", "touten", "kuten"])
    }

    func testConfigPersists() {
        let suite = testDefaultsSuite()
        let defaults = UserDefaults(suiteName: suite)!
        defer { removeTestDefaults(defaults, suite) }
        let store = ConfigStore(defaults: defaults)
        store.symbolRemovalEnabled = false
        store.symbolKeepGroups = ["question"]
        let reloaded = ConfigStore(defaults: defaults)
        XCTAssertFalse(reloaded.symbolRemovalEnabled)
        XCTAssertEqual(reloaded.symbolKeepGroups, ["question"])
        // 空配列（何も残さない）も保存値として尊重する
        store.symbolKeepGroups = []
        XCTAssertEqual(ConfigStore(defaults: defaults).symbolKeepGroups, [])
    }
}
