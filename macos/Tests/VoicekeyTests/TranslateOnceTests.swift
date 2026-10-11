//
//  TranslateOnceTests.swift
//  「この 1 回だけ翻訳」キーの判定と、翻訳元の推定の単体テスト
//
//  ユーザー要望「翻訳して入力はたまに 1 回だけ使う」への対処。録音キーと一緒に（または録音中に）
//  1 回翻訳キーを押した録音だけを訳す。判定は純関数にしてあるので、押下の組み合わせだけを固定する。
//  翻訳元は「話す言語の設定＝ja」と決め打たず入力から推定するので、その判定もここで固定する
//  （NaturalLanguage はオフラインで動く）。
//

import XCTest

@testable import voicekey

@MainActor
final class TranslateOnceTests: XCTestCase {

    // 録音キー（右⌘）＋ 1 回翻訳キー（右⇧）が押されていれば翻訳する
    func testRequestedWhenKeyPressedWithSlotKey() {
        XCTAssertTrue(DictationTranslation.isTranslateOnceRequested(
            key: ["shift_r"], slotHotkey: ["cmd_r"], pressed: ["cmd_r", "shift_r"]))
    }

    // 1 回翻訳キーが押されていなければ翻訳しない
    func testNotRequestedWithoutKey() {
        XCTAssertFalse(DictationTranslation.isTranslateOnceRequested(
            key: ["shift_r"], slotHotkey: ["cmd_r"], pressed: ["cmd_r"]))
    }

    // 左⇧ は右⇧ の代わりにならない（左右を区別する設定のとき）
    func testSideSpecificKeyIsStrict() {
        XCTAssertFalse(DictationTranslation.isTranslateOnceRequested(
            key: ["shift_r"], slotHotkey: ["cmd_r"], pressed: ["cmd_r", "shift_l"]))
    }

    // 汎用の ⇧ は左右どちらでもよい
    func testGenericModifierAcceptsEitherSide() {
        XCTAssertTrue(DictationTranslation.isTranslateOnceRequested(
            key: ["shift"], slotHotkey: ["cmd_r"], pressed: ["cmd_r", "shift_l"]))
    }

    // キーが空（無効化）なら何を押しても翻訳しない
    func testEmptyKeyDisables() {
        XCTAssertFalse(DictationTranslation.isTranslateOnceRequested(
            key: [], slotHotkey: ["cmd_r"], pressed: ["cmd_r", "shift_r"]))
    }

    // 1 回翻訳キーが録音キーそのものに含まれる設定なら、毎回翻訳にならないよう無視する
    func testIgnoredWhenCoveredBySlotHotkey() {
        XCTAssertFalse(DictationTranslation.isTranslateOnceRequested(
            key: ["shift_r"], slotHotkey: ["shift_r", "space"], pressed: ["shift_r", "space"]))
        // 録音キーが汎用 ⇧ を含むなら右⇧ も覆われる
        XCTAssertFalse(DictationTranslation.isTranslateOnceRequested(
            key: ["shift_r"], slotHotkey: ["shift", "space"], pressed: ["shift_r", "space"]))
    }

    // 録音中に押されたキーが 1 回翻訳キーの一部かの判定（関係ないキーは拾わない）
    func testTranslateOnceToken() {
        XCTAssertTrue(DictationTranslation.isTranslateOnceToken("shift_r", key: ["shift_r"]))
        XCTAssertTrue(DictationTranslation.isTranslateOnceToken("shift_l", key: ["shift"]))
        XCTAssertFalse(DictationTranslation.isTranslateOnceToken("cmd_r", key: ["shift_r"]))
        XCTAssertFalse(DictationTranslation.isTranslateOnceToken("shift_r", key: []))
    }

    // HUD の印は言語コードだけを大文字で出す
    func testBadge() {
        XCTAssertEqual(DictationTranslation.badge(for: "en"), "→EN")
        XCTAssertEqual(DictationTranslation.badge(for: "zh-Hans"), "→ZH")
    }

    // 翻訳元は入力から推定する（話す言語の設定に関係なく、英語で話せば en と判定される）
    func testDetectLanguage() {
        XCTAssertEqual(DictationTranslation.detectLanguageCode("今日はいい天気ですね、散歩に行きましょう", fallback: "en"), "ja")
        XCTAssertEqual(DictationTranslation.detectLanguageCode("This is a quick test of the dictation feature.", fallback: "ja"), "en")
        // 判定できない入力は設定の言語へ落とす
        XCTAssertEqual(DictationTranslation.detectLanguageCode("123 456", fallback: "ja"), "ja")
    }

    // 同じ言語の判定は地域・字体を無視する
    func testSameLanguage() {
        XCTAssertTrue(DictationTranslation.isSameLanguage("en", "en-US"))
        XCTAssertTrue(DictationTranslation.isSameLanguage("zh-Hans", "zh"))
        XCTAssertFalse(DictationTranslation.isSameLanguage("ja", "en"))
    }

    // 1 回翻訳キーの既定は右⇧。空配列に変えたら無効化として保存される
    func testConfigDefaultAndPersist() {
        let suite = testDefaultsSuite()
        let defaults = UserDefaults(suiteName: suite)!
        defer { removeTestDefaults(defaults, suite) }

        let store = ConfigStore(defaults: defaults)
        XCTAssertEqual(store.translateOnceKey, ["shift_r"])
        store.translateOnceKey = []
        let reloaded = ConfigStore(defaults: defaults)
        XCTAssertEqual(reloaded.translateOnceKey, [])
    }
}
