//
//  TextNormalizeTests.swift
//  CJK 隣接スペース除去（#21）の単体テスト
//
//  日本語・中国語・英数字の境界を含めて、streaming / REST / Windows と
//  同じ結果になることを保証する。Windows 版 tests/test_text_utils.py と同じ観点。
//

import XCTest
@testable import voicekey

final class TextNormalizeTests: XCTestCase {

    // 日本語の単語間スペースは除去される
    func testJapaneseSpacesRemoved() {
        XCTAssertEqual(TextNormalize.stripCJKSpaces("今日 は 晴れ"), "今日は晴れ")
    }

    // 中国語（簡体・繁体とも CJK 統合漢字）の単語間スペースも除去される
    func testChineseSpacesRemoved() {
        XCTAssertEqual(TextNormalize.stripCJKSpaces("今天 天气 很好"), "今天天气很好")
    }

    // 英単語間のスペースは保持される（英語表記を壊さない）
    func testAsciiSpacesKept() {
        XCTAssertEqual(TextNormalize.stripCJKSpaces("hello world"), "hello world")
    }

    // 日本語と英数字の境界: 片側が CJK ならスペースを除去する
    func testJapaneseAsciiBoundaryRemoved() {
        XCTAssertEqual(TextNormalize.stripCJKSpaces("私は GPT を使う"), "私はGPTを使う")
    }

    // 英数字どうしの境界（CJK 非隣接）は保持される
    func testAsciiNumberBoundaryKept() {
        XCTAssertEqual(TextNormalize.stripCJKSpaces("GPT 4"), "GPT 4")
    }

    // スペースを含まない文字列は変化しない（早期 return）
    func testNoSpaceUnchanged() {
        XCTAssertEqual(TextNormalize.stripCJKSpaces("今日は晴れ"), "今日は晴れ")
    }

    // 全角・半角形（全角英数）も CJK とみなしスペースを除去する
    func testFullwidthBoundaryRemoved() {
        XCTAssertEqual(TextNormalize.stripCJKSpaces("Ａ は B"), "ＡはB")
    }

    // 冪等: 2 回適用しても結果は変わらない
    func testIdempotent() {
        let once = TextNormalize.stripCJKSpaces("今日 は 晴れ")
        XCTAssertEqual(TextNormalize.stripCJKSpaces(once), once)
    }
}
