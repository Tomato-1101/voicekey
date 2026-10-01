//
//  MainWindowScreenTests.swift
//  メインウィンドウの「前回の画面」（ホーム or 設定のどのタブ）の保存・復元の単体テスト
//
//  Dock から戻したとき・再起動後に開いたときに前回の画面で開く／消えたタブは一般に落とす、の回帰判定。
//  実 UserDefaults に触れないよう suite を注入する。
//

import XCTest
@testable import voicekey

@MainActor
final class MainWindowScreenTests: XCTestCase {

    /// テスト用の隔離された UserDefaults を作る
    private func makeDefaults() -> (UserDefaults, String) {
        let suite = "voicekey.test.\(UUID().uuidString)"
        return (UserDefaults(suiteName: suite)!, suite)
    }

    // 未保存ならホーム（設定タブは一般）
    func testLoadDefaultsToHomeWhenNothingSaved() {
        let (defaults, suite) = makeDefaults()
        defer { defaults.removePersistentDomain(forName: suite) }

        let screen = MainWindowScreen.load(from: defaults, validTabs: [0, 1, 2, 7])
        XCTAssertEqual(screen, MainWindowScreen(showingSettings: false, settingsTab: 0))
    }

    // 保存した画面がそのまま読める
    func testSaveAndLoadRoundTrip() {
        let (defaults, suite) = makeDefaults()
        defer { defaults.removePersistentDomain(forName: suite) }

        MainWindowScreen(showingSettings: true, settingsTab: 8).save(to: defaults)
        let screen = MainWindowScreen.load(from: defaults, validTabs: [0, 1, 2, 8, 7])
        XCTAssertEqual(screen, MainWindowScreen(showingSettings: true, settingsTab: 8))
    }

    // 保存したタブがいまのサイドバーに無い（表示条件で消えた）なら一般タブ（0）に落ちる
    func testLoadFallsBackToGeneralWhenSavedTabIsGone() {
        let (defaults, suite) = makeDefaults()
        defer { defaults.removePersistentDomain(forName: suite) }

        MainWindowScreen(showingSettings: true, settingsTab: 9).save(to: defaults)
        let screen = MainWindowScreen.load(from: defaults, validTabs: [0, 1, 2, 8, 7])
        XCTAssertEqual(screen, MainWindowScreen(showingSettings: true, settingsTab: 0))
    }

    func testSanitizedTab() {
        XCTAssertEqual(MainWindowScreen.sanitizedTab(7, validTabs: [0, 7]), 7)
        XCTAssertEqual(MainWindowScreen.sanitizedTab(5, validTabs: [0, 7]), 0)
        XCTAssertEqual(MainWindowScreen.sanitizedTab(-1, validTabs: [0, 7]), 0)
    }

    // 保存先付きのモデルは画面を切り替えるたびに書き戻し、次に復元すると同じ画面になる
    func testRestoredModelPersistsScreenChanges() {
        let (defaults, suite) = makeDefaults()
        defer { defaults.removePersistentDomain(forName: suite) }

        let model = MainWindowModel.restored(from: defaults)
        XCTAssertFalse(model.showingSettings)
        model.settingsTab = 2
        model.showingSettings = true

        let reopened = MainWindowModel.restored(from: defaults)
        XCTAssertTrue(reopened.showingSettings)
        XCTAssertEqual(reopened.settingsTab, 2)

        // ホームへ戻しても、最後に選んだ設定タブは覚えている（次の「設定…」がそのタブで開くため）
        reopened.showingSettings = false
        let again = MainWindowModel.restored(from: defaults)
        XCTAssertFalse(again.showingSettings)
        XCTAssertEqual(again.settingsTab, 2)
    }

    // 保存先なしのモデル（UI 撮影ハーネス用）は既定の UserDefaults にも書かない
    func testModelWithoutStoreDoesNotPersist() {
        let standard = UserDefaults.standard
        let beforeShowing = standard.object(forKey: MainWindowScreen.showingSettingsKey) as? Bool
        let beforeTab = standard.object(forKey: MainWindowScreen.settingsTabKey) as? Int

        let model = MainWindowModel(showingSettings: false, settingsTab: 0)
        model.settingsTab = 7
        model.showingSettings = true

        XCTAssertEqual(standard.object(forKey: MainWindowScreen.showingSettingsKey) as? Bool, beforeShowing)
        XCTAssertEqual(standard.object(forKey: MainWindowScreen.settingsTabKey) as? Int, beforeTab)
    }
}
