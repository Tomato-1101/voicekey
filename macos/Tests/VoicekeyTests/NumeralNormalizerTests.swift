//
//  NumeralNormalizerTests.swift
//  数字表記正規化（NumeralNormalizer）と Whisper prompt 組み立ての単体テスト
//
//  変換系（全角→半角・連続漢数字→算用数字）と、普通の日本語語彙を壊さない保護系を検証する。
//  Windows 版 tests/test_numeral_normalizer.py と同じ観点。
//

import XCTest
@testable import voicekey

final class NumeralNormalizerTests: XCTestCase {

    // MARK: - 変換系（半角化されるべき）

    func testFullwidthDigitsToHalfwidth() {
        XCTAssertEqual(NumeralNormalizer.normalize("１２３４５"), "12345")
    }

    func testFullwidthLettersToHalfwidth() {
        XCTAssertEqual(NumeralNormalizer.normalize("ＡＢＣａｂｃ"), "ABCabc")
    }

    // 位取りを含まない連続漢数字（2 文字以上）は算用数字化する
    func testConsecutiveKanjiDigitsToArabic() {
        XCTAssertEqual(NumeralNormalizer.normalize("三五八〇九一"), "358091")
    }

    // 二〇二六 のような読み上げ年号は連続漢数字なので変換される
    func testKanjiYearSequence() {
        XCTAssertEqual(NumeralNormalizer.normalize("二〇二六年"), "2026年")
    }

    func testMixedSentence() {
        XCTAssertEqual(
            NumeralNormalizer.normalize("電話番号は〇九〇一二三四五六七八です"),
            "電話番号は09012345678です"
        )
    }

    // MARK: - 保護系（普通の語彙を壊してはならない）

    // 単独の漢数字は普通の語の一部なので変換しない
    func testSingleKanjiWordsUnchanged() {
        for word in ["一人", "二階建て", "一番", "五月雨", "四月", "六本木"] {
            XCTAssertEqual(NumeralNormalizer.normalize(word), word)
        }
    }

    // 位取り（十百千万）を含む数はそのまま
    func testPositionalKanjiUnchanged() {
        XCTAssertEqual(NumeralNormalizer.normalize("十時三十分"), "十時三十分")
        XCTAssertEqual(NumeralNormalizer.normalize("千二百円"), "千二百円")
        XCTAssertEqual(NumeralNormalizer.normalize("百二十三"), "百二十三")
    }

    func testNormalTextUnchanged() {
        XCTAssertEqual(NumeralNormalizer.normalize("今日は晴れです"), "今日は晴れです")
    }

    // MARK: - 端条件

    func testEmpty() {
        XCTAssertEqual(NumeralNormalizer.normalize(""), "")
    }

    func testHalfwidthAlreadyUnchanged() {
        XCTAssertEqual(NumeralNormalizer.normalize("abc123"), "abc123")
    }

    // 全角の記号・句読点は変換対象外（日本語表記を壊さない）
    func testFullwidthPunctuationKept() {
        XCTAssertEqual(NumeralNormalizer.normalize("はい、（そうです）！"), "はい、（そうです）！")
    }

    func testIdempotent() {
        let once = NumeralNormalizer.normalize("三五八〇九一と１２３と一人")
        XCTAssertEqual(NumeralNormalizer.normalize(once), once)
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
