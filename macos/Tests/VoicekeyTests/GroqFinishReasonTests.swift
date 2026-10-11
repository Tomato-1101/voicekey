//
//  GroqFinishReasonTests.swift
//  Groq のストリーミング訳が途中で切れたときに成功扱いしないことの回帰テスト（2026-10-12）
//
//  以前は通信が途中で切れても、受け取った分だけで訳を確定させていたため、文の後半が欠けた訳が
//  字幕（と「翻訳して入力」の貼り付け）に出ていた。finish_reason を見て、最後まで届いたものだけを
//  成功にする。ネットワーク・鍵には触らない。
//

import XCTest

@testable import voicekey

final class GroqFinishReasonTests: XCTestCase {

    /// SSE 行から finish_reason を読む（null・usage だけの行・[DONE] は nil）
    func testFinishReasonParsing() throws {
        guard #available(macOS 26.0, *) else { throw XCTSkip("macOS 26 以降") }
        XCTAssertEqual(GroqTranslator.finishReason(
            fromSSELine: #"data: {"choices":[{"delta":{},"finish_reason":"stop"}]}"#), "stop")
        XCTAssertEqual(GroqTranslator.finishReason(
            fromSSELine: #"data: {"choices":[{"delta":{},"finish_reason":"length"}]}"#), "length")
        XCTAssertNil(GroqTranslator.finishReason(
            fromSSELine: #"data: {"choices":[{"delta":{"content":"こん"},"finish_reason":null}]}"#))
        XCTAssertNil(GroqTranslator.finishReason(fromSSELine: #"data: {"choices":[],"usage":{"total_tokens":3}}"#))
        XCTAssertNil(GroqTranslator.finishReason(fromSSELine: "data: [DONE]"))
        XCTAssertNil(GroqTranslator.finishReason(fromSSELine: ""))
    }

    /// 最後まで届いた（stop）ときだけ成功。打ち切り・切断・終わりの印なしは失敗
    func testCompletionError() throws {
        guard #available(macOS 26.0, *) else { throw XCTSkip("macOS 26 以降") }
        XCTAssertNil(GroqTranslator.completionError(finishReason: "stop", interrupted: false))
        XCTAssertNotNil(GroqTranslator.completionError(finishReason: "length", interrupted: false))
        XCTAssertNotNil(GroqTranslator.completionError(finishReason: nil, interrupted: false))
        XCTAssertNotNil(GroqTranslator.completionError(finishReason: "stop", interrupted: true))
    }
}
