//
//  AIDestinationTests.swift
//  「貼り付け先が AI のときは整形しない」の判定（AIDestination）と設定・ログ表記のテスト
//
//  判定は録音開始時の最前面アプリの bundle ID だけで決まる純関数なので、ここで固定する。
//

import XCTest
@testable import voicekey

final class AIDestinationTests: XCTestCase {

    // AI チャット・ターミナル・Cursor は AI とみなす
    func testKnownAIAppsAreDetected() {
        for id in ["com.anthropic.claudefordesktop", "com.openai.chat", "com.apple.Terminal",
                   "com.googlecode.iterm2", "com.mitchellh.ghostty", "dev.warp.Warp-Stable",
                   "net.kovidgoyal.kitty", "com.github.wez.wezterm", "com.todesktop.230313mzl4w4u92"] {
            XCTAssertTrue(AIDestination.isAI(bundleID: id), "\(id) が AI と判定されない")
        }
    }

    // bundle ID の大小は区別しない（LaunchServices と同じ扱い）
    func testCaseInsensitive() {
        XCTAssertTrue(AIDestination.isAI(bundleID: "COM.APPLE.TERMINAL"))
    }

    // ふつうのアプリ・ブラウザ（chatgpt.com を開いていても）・取れない場合は AI ではない
    func testOrdinaryAppsAndBrowsersAreNotAI() {
        for id in ["com.apple.TextEdit", "com.apple.Safari", "com.google.Chrome", "com.tinyspeck.slackmacgap",
                   "com.apple.mail", ""] {
            XCTAssertFalse(AIDestination.isAI(bundleID: id), "\(id) が AI と判定された")
        }
        XCTAssertFalse(AIDestination.isAI(bundleID: nil))
    }

    // 整形を省くのは「録音キーの整形 ON × 設定 ON × 貼り付け先が AI」のときだけ
    func testShouldSkipFormat() {
        let claude = "com.anthropic.claudefordesktop"
        XCTAssertTrue(AIDestination.shouldSkipFormat(formatEnabled: true, skipForAIEnabled: true, bundleID: claude))
        // 設定 OFF なら今までどおり整形する
        XCTAssertFalse(AIDestination.shouldSkipFormat(formatEnabled: true, skipForAIEnabled: false, bundleID: claude))
        // 整形 OFF の録音キーはそもそも整形しない（「省いた」とは数えない）
        XCTAssertFalse(AIDestination.shouldSkipFormat(formatEnabled: false, skipForAIEnabled: true, bundleID: claude))
        // AI でない貼り付け先は整形する
        XCTAssertFalse(AIDestination.shouldSkipFormat(
            formatEnabled: true, skipForAIEnabled: true, bundleID: "com.apple.TextEdit"))
        XCTAssertFalse(AIDestination.shouldSkipFormat(formatEnabled: true, skipForAIEnabled: true, bundleID: nil))
    }

    // `[計測]` の整形欄は、省いた回だけ時間の代わりに理由を出す（空白を含めない）
    func testSummaryLineShowsSkipReason() {
        var t = DictationTimeline(generation: 5, slot: 1, backend: "soniox", model: "m", pressedAt: 0)
        t.formatSkipNote = AIDestination.skippedLabel
        XCTAssertTrue(t.summaryLine().contains(" 整形=省略(AI宛て) "), t.summaryLine())
        XCTAssertFalse(AIDestination.skippedLabel.contains(" "))
        // 省いていない回は従来どおり（時間 or -）
        let plain = DictationTimeline(generation: 6, slot: 1, backend: "soniox", model: "m", pressedAt: 0)
        XCTAssertTrue(plain.summaryLine().contains(" 整形=- "))
    }

    // 設定は既定 ON で、保存・読み込みできる
    @MainActor
    func testConfigDefaultAndPersistence() {
        let suite = testDefaultsSuite()
        let defaults = UserDefaults(suiteName: suite)!
        defer { removeTestDefaults(defaults, suite) }

        let store = ConfigStore(defaults: defaults)
        XCTAssertTrue(store.skipFormatForAI)
        store.skipFormatForAI = false
        XCTAssertEqual(defaults.object(forKey: "skipFormatForAI") as? Bool, false)
        XCTAssertFalse(ConfigStore(defaults: defaults).skipFormatForAI)
    }
}
