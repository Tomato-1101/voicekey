//
//  NumeralNormalizerTests.swift
//  数字表記正規化（NumeralNormalizer v2）と Whisper prompt 組み立ての単体テスト
//
//  半角化・漢数字→算用数字（裸数字列／位取り）・助数詞つき単独漢数字・保護リスト・
//  2 トグルを検証する。Windows 版 tests/test_numeral_normalizer.py と同じ観点。
//

import XCTest
@testable import voicekey

final class NumeralNormalizerTests: XCTestCase {

    /// 既定シード保護語（ConfigStore.defaultNumeralProtectWords と一致させる）
    private let seed: Set<String> = [
        "一時的", "一時停止", "一人", "二人", "十分", "一日中", "一部始終", "一石二鳥",
    ]

    // MARK: - 半角化

    func testFullwidthDigitsToHalfwidth() {
        XCTAssertEqual(NumeralNormalizer.normalize("１２３４５"), "12345")
    }

    func testFullwidthSingleDigit() {
        XCTAssertEqual(NumeralNormalizer.normalize("１０"), "10")
    }

    func testFullwidthLettersToHalfwidth() {
        XCTAssertEqual(NumeralNormalizer.normalize("ＡＢＣａｂｃ"), "ABCabc")
    }

    // MARK: - 裸数字ラン（各桁を算用数字化）

    func testConsecutiveKanjiDigitsToArabic() {
        XCTAssertEqual(NumeralNormalizer.normalize("三五八〇九一"), "358091")
    }

    func testKanjiYearSequence() {
        XCTAssertEqual(NumeralNormalizer.normalize("二〇二六年"), "2026年")
    }

    func testPhoneNumberWithLeadingZero() {
        XCTAssertEqual(NumeralNormalizer.normalize("〇九〇一二三四五六七八"), "09012345678")
    }

    func testMixedSentence() {
        XCTAssertEqual(
            NumeralNormalizer.normalize("電話番号は〇九〇一二三四五六七八です"),
            "電話番号は09012345678です"
        )
    }

    // MARK: - 位取り（日本語数詞パース）

    func testPositionalJuuni() {
        XCTAssertEqual(NumeralNormalizer.normalize("十二人"), "12人")
    }

    func testPositionalNijuusan() {
        XCTAssertEqual(NumeralNormalizer.normalize("二十三"), "23")
    }

    func testPositionalSenNihyaku() {
        XCTAssertEqual(NumeralNormalizer.normalize("千二百三十四円"), "1234円")
    }

    func testPositionalSanmanGosen() {
        XCTAssertEqual(NumeralNormalizer.normalize("三万五千"), "35000")
    }

    // MARK: - 単独漢数字＋助数詞

    func testSingleWithCounter() {
        XCTAssertEqual(NumeralNormalizer.normalize("三時"), "3時")
        XCTAssertEqual(NumeralNormalizer.normalize("十時"), "10時")
        XCTAssertEqual(NumeralNormalizer.normalize("百人"), "100人")
        XCTAssertEqual(NumeralNormalizer.normalize("千円"), "1000円")
    }

    // MARK: - 地名（助数詞集合外は変換しない）

    func testPlaceNamesUnchanged() {
        XCTAssertEqual(NumeralNormalizer.normalize("千葉"), "千葉")
        XCTAssertEqual(NumeralNormalizer.normalize("六本木"), "六本木")
        XCTAssertEqual(NumeralNormalizer.normalize("五反田"), "五反田")
    }

    // MARK: - 保護リスト（先頭アンカー照合）

    func testProtectedSeedWords() {
        XCTAssertEqual(NumeralNormalizer.normalize("一時的", protectWords: seed), "一時的")
        XCTAssertEqual(NumeralNormalizer.normalize("一人", protectWords: seed), "一人")
        XCTAssertEqual(NumeralNormalizer.normalize("十分", protectWords: seed), "十分")
    }

    // 「一人」保護は先頭アンカーなので「十一人」の中の「一人」に誤爆せず 11人 に変換される
    func testJuuichininNotFalselyProtected() {
        XCTAssertEqual(NumeralNormalizer.normalize("十一人", protectWords: seed), "11人")
    }

    // MARK: - トグル

    func testConvertCounterOffKeepsSingle() {
        XCTAssertEqual(NumeralNormalizer.normalize("三時", convertCounter: false), "三時")
    }

    func testConvertCounterOffStillConvertsPositional() {
        // 位取り≥2 はマスターのみで常時変換（convertCounter の影響を受けない）
        XCTAssertEqual(NumeralNormalizer.normalize("十二", convertCounter: false), "12")
    }

    func testDisabledIsFullPassthrough() {
        XCTAssertEqual(NumeralNormalizer.normalize("三時の１２３", enabled: false), "三時の１２３")
    }

    // MARK: - 端条件

    func testEmpty() {
        XCTAssertEqual(NumeralNormalizer.normalize(""), "")
    }

    func testHalfwidthAlreadyUnchanged() {
        XCTAssertEqual(NumeralNormalizer.normalize("abc123"), "abc123")
    }

    func testNormalTextUnchanged() {
        XCTAssertEqual(NumeralNormalizer.normalize("今日は晴れです"), "今日は晴れです")
    }

    // 全角の記号・句読点は変換対象外（日本語表記を壊さない）
    func testFullwidthPunctuationKept() {
        XCTAssertEqual(NumeralNormalizer.normalize("はい、（そうです）！"), "はい、（そうです）！")
    }

    func testIdempotent() {
        for text in ["十二人", "千二百三十四円", "三時", "二〇二六年", "六本木", "１０と三万五千"] {
            let once = NumeralNormalizer.normalize(text, protectWords: seed)
            XCTAssertEqual(NumeralNormalizer.normalize(once, protectWords: seed), once, text)
        }
    }

    // カタカナ「ゼロ」は数字（ASCII/全角/漢数字）に隣接するときだけ 0/〇 に寄せる
    func testKatakanaZeroInNumberContext() {
        XCTAssertEqual(NumeralNormalizer.normalize("1234567ゼロ"), "12345670")  // 末尾の読み上げゼロ
        XCTAssertEqual(NumeralNormalizer.normalize("ゼロ九〇"), "090")
        XCTAssertEqual(NumeralNormalizer.normalize("ゼロゼロ九"), "009")        // 連鎖
        XCTAssertEqual(NumeralNormalizer.normalize("ゼロ一"), "01")
        // 数字に隣接しない「ゼロ」は語として温存する
        XCTAssertEqual(NumeralNormalizer.normalize("ゼロから始める"), "ゼロから始める")
    }
}

/// Whisper（Groq/OpenAI）へ渡す数字 style プロンプトの組み立てを検証する。
final class TranscriberWhisperPromptTests: XCTestCase {

    // ユーザープロンプトが空なら数字 style ヒントのみ（release はこの経路）
    func testEmptyUserPromptUsesHintOnly() {
        XCTAssertEqual(
            Transcriber.whisperPrompt(userPrompt: ""),
            Transcriber.numeralStyleHint
        )
    }

    // ユーザープロンプトがあれば style ヒントの後ろに連結する（main のプロンプト設定を活かす）
    func testUserPromptAppendedAfterHint() {
        let combined = Transcriber.whisperPrompt(userPrompt: "固有名詞: VoiceKey")
        XCTAssertTrue(combined.hasPrefix(Transcriber.numeralStyleHint))
        XCTAssertTrue(combined.contains("固有名詞: VoiceKey"))
    }
}
