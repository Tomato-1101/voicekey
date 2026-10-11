//
//  TranslateOnceTests.swift
//  「この 1 回だけ翻訳」キーの判定と、翻訳元の推定の単体テスト
//
//  ユーザー要望「翻訳して入力はたまに 1 回だけ使う」への対処。録音キーと一緒に（または録音中に）
//  1 回翻訳キーを押した録音だけを訳す。判定は純関数にしてあるので、押下の組み合わせだけを固定する。
//  翻訳元は「話す言語の設定＝ja」と決め打たず入力から推定するので、その判定もここで固定する
//  （NaturalLanguage はオフラインで動く）。
//

import NaturalLanguage
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

    // MARK: - macOS 26 未満では無効

    // 翻訳できない OS では 1 回翻訳キーを空にする（右⇧で「→EN」だけ出て原文が入るのを防ぐ）
    func testTranslateOnceKeyDisabledWhenUnsupported() {
        XCTAssertEqual(DictationTranslation.effectiveTranslateOnceKey(["shift_r"], supported: false), [])
        XCTAssertEqual(DictationTranslation.effectiveTranslateOnceKey(["shift_r"], supported: true), ["shift_r"])
        // 無効化したキーでは押しても翻訳扱いにならない
        let key = DictationTranslation.effectiveTranslateOnceKey(["shift_r"], supported: false)
        XCTAssertFalse(DictationTranslation.isTranslateOnceRequested(
            key: key, slotHotkey: ["cmd_r"], pressed: ["cmd_r", "shift_r"]))
        XCTAssertFalse(DictationTranslation.isTranslateOnceToken("shift_r", key: key))
    }

    // 翻訳できない OS では常時 ON でも 1 回翻訳でも翻訳しない（バッジも出ない）
    func testShouldTranslateRequiresSupport() {
        XCTAssertFalse(DictationTranslation.shouldTranslate(alwaysOn: true, translateOnce: true, supported: false))
        XCTAssertTrue(DictationTranslation.shouldTranslate(alwaysOn: false, translateOnce: true, supported: true))
        XCTAssertTrue(DictationTranslation.shouldTranslate(alwaysOn: true, translateOnce: false, supported: true))
        XCTAssertFalse(DictationTranslation.shouldTranslate(alwaysOn: false, translateOnce: false, supported: true))
    }

    // MARK: - 録音中のトグル

    // 1 回翻訳キーで付けた翻訳は、もう一度押すと外れ、さらに押すとまた付く
    func testToggleCancelsTranslateOnce() {
        var translate = false
        translate = DictationTranslation.toggledTranslate(current: translate, alwaysOn: false)
        XCTAssertTrue(translate)
        translate = DictationTranslation.toggledTranslate(current: translate, alwaysOn: false)
        XCTAssertFalse(translate)
        translate = DictationTranslation.toggledTranslate(current: translate, alwaysOn: false)
        XCTAssertTrue(translate)
    }

    // 常時 ON の翻訳はキーでは外れない
    func testToggleKeepsAlwaysOnTranslation() {
        XCTAssertTrue(DictationTranslation.toggledTranslate(current: true, alwaysOn: true))
    }

    // MARK: - キーの衝突警告

    private func warnings(
        _ key: [String], slots: [[String]] = [["cmd_r"], ["alt_r"]], handsfree: [String] = [], repaste: [String] = []
    ) -> [String] {
        DictationTranslation.translateOnceKeyWarnings(
            key: key, slotHotkeys: slots, handsfreeKey: handsfree, repasteKey: repaste)
    }

    func testNoWarningForIndependentKey() {
        XCTAssertTrue(warnings(["shift_r"]).isEmpty)
        XCTAssertTrue(warnings([]).isEmpty)
    }

    // 録音キーと同じ（汎用修飾は左右どちらとも重なる）なら警告する
    func testWarnsWhenSameAsSlotKey() {
        XCTAssertEqual(warnings(["cmd_r"]).count, 1)
        XCTAssertTrue(warnings(["cmd"]).first?.contains("録音キー 1") == true)
        XCTAssertTrue(warnings(["alt_r"]).first?.contains("録音キー 2") == true)
    }

    // 個数が違っても、録音キーに含まれてしまうなら警告する（右⌘＋Space の録音キーに右⌘）
    func testWarnsWhenCoveredBySlotKeyWithDifferentCount() {
        XCTAssertFalse(warnings(["cmd_r"], slots: [["cmd_r", "space"], []]).isEmpty)
    }

    // 録音キーを含む 1 回翻訳キー（同時押しの使い方）は警告しない
    func testNoWarningWhenKeyContainsSlotKey() {
        XCTAssertTrue(warnings(["cmd_r", "shift_r"]).isEmpty)
    }

    // ハンズフリーキーと同じ・録音キーと併せて覆われる場合は、ハンズフリー優先で効かないと警告する
    func testWarnsWhenSameAsHandsfreeKey() {
        let same = warnings(["shift_r"], handsfree: ["shift_r"])
        XCTAssertEqual(same.count, 1)
        XCTAssertTrue(same[0].contains("ハンズフリー"))
        XCTAssertFalse(warnings(["cmd_r", "shift_r"], handsfree: ["shift_r"]).isEmpty)
        XCTAssertTrue(warnings(["shift_r"], handsfree: ["ctrl_r"]).isEmpty)
    }

    // 再貼り付けキーが 1 回翻訳キーに含まれる（同じを含む）と、待機中に押すと再貼り付けが走る
    func testWarnsWhenOverlapsRepasteKey() {
        XCTAssertTrue(warnings(["shift_r"], repaste: ["shift_r"]).first?.contains("再貼り付け") == true)
        XCTAssertFalse(warnings(["shift_r", "space"], repaste: ["shift_r"]).isEmpty)
        XCTAssertTrue(warnings(["shift_r"], repaste: ["ctrl_r", "v"]).isEmpty)
    }

    // MARK: - 言語判定の候補

    // 日中で共通の漢字だけの短文は、候補を絞らないと中国語と判定される（話す言語＝日本語なら ja）
    func testShortKanjiIsJapaneseWhenSpeakingJapanese() {
        for text in ["了解", "人工知能", "以上", "大丈夫"] {
            XCTAssertEqual(DictationTranslation.detectLanguageCode(text, fallback: "ja", target: "en"), "ja", text)
        }
        // 翻訳先が中国語でも、話す言語の日本語を優先する
        XCTAssertEqual(DictationTranslation.detectLanguageCode("了解", fallback: "ja", target: "zh-Hans"), "ja")
    }

    // 本物の中国語の文は、翻訳先が中国語なら中国語と判定できる（候補に入っている）
    func testRealChineseSentenceIsDetected() {
        XCTAssertEqual(
            DictationTranslation.detectLanguageCode("我们明天去上海吧，会议开始了", fallback: "ja", target: "zh-Hans"),
            "zh-Hans")
        XCTAssertEqual(
            DictationTranslation.detectLanguageCode("我们明天去上海吧", fallback: "ja", target: "zh-Hans"), "zh-Hans")
    }

    // 候補は話す言語・翻訳先・日本語・英語の順で重複しない。中国語は字体の指定を見る
    func testLanguageCandidates() {
        XCTAssertEqual(DictationTranslation.languageCandidates(spoken: "ja", target: "en"), [.japanese, .english])
        XCTAssertEqual(DictationTranslation.languageCandidates(spoken: "en-US", target: "ja"), [.english, .japanese])
        XCTAssertEqual(DictationTranslation.languageCandidates(spoken: "ja", target: "zh-Hant"),
                       [.japanese, .traditionalChinese, .english])
        XCTAssertEqual(DictationTranslation.languageCandidates(spoken: "ko", target: "zh"),
                       [.korean, .simplifiedChinese, .traditionalChinese, .japanese, .english])
    }
}
