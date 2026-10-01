//
//  TranscriptionErrorDetailTests.swift
//  API エラー応答本文の 1 行化・切り詰め（TranscriptionError.responseDetail）の単体テスト
//
//  HUD に生 JSON を出さないため本文は detail に分け、行動ログ 1 行に収まる形へ整える。
//  純関数の検証のみ。ネットワークには触れない。
//

import XCTest
@testable import voicekey

final class TranscriptionErrorDetailTests: XCTestCase {

    /// 改行・タブ・その他の制御文字は空白にし、連続する空白は 1 つに詰める
    func testFlattensControlCharacters() {
        let body = "{\n  \"error\": {\r\n\t\"message\": \"bad\u{0007}request\"\n  }\n}"
        XCTAssertEqual(
            TranscriptionError.responseDetail(Data(body.utf8)),
            "{ \"error\": { \"message\": \"bad request\" } }")
    }

    /// 長い本文は limit 文字で切って「…」を付ける（日本語でも文字数で数える）
    func testTruncatesByCharacters() {
        let body = String(repeating: "あ", count: 300)
        let detail = TranscriptionError.responseDetail(Data(body.utf8), limit: 200)
        XCTAssertEqual(detail, String(repeating: "あ", count: 200) + "…")
    }

    /// limit ちょうどなら切らない
    func testKeepsShortBody() {
        XCTAssertEqual(TranscriptionError.responseDetail(Data("abc".utf8), limit: 3), "abc")
    }

    /// UTF-8 として不正なバイトや空本文でも落ちない
    func testHandlesInvalidAndEmptyData() {
        XCTAssertEqual(TranscriptionError.responseDetail(Data()), "")
        XCTAssertFalse(TranscriptionError.responseDetail(Data([0xFF, 0xFE, 0x41])).isEmpty)
    }

    /// HUD に出す message には本文が入らない（既定の detail は nil）
    func testMessageDoesNotCarryDetailByDefault() {
        let error = TranscriptionError(message: "Groq API エラー (HTTP 500)")
        XCTAssertNil(error.detail)
        XCTAssertEqual(error.errorDescription, "Groq API エラー (HTTP 500)")
    }
}
