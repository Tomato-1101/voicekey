//
//  StreamingJoinTests.swift
//  Deepgram ライブの確定片の連結の単体テスト
//
//  確定片は trim されて届くので、空白なしで繋ぐと英語の単語がくっついていた。
//  空白で繋いだうえで、日本語に挟まった空白は stripCJKSpaces（確定時に必ず通す）で落ちることを固定する。
//

import XCTest

@testable import voicekey

final class StreamingJoinTests: XCTestCase {

    // 英語の確定片の間に空白が入る
    func testEnglishSegmentsKeepWordSpace() {
        let joined = StreamingTranscriber.joinSegments(["Hello there.", "How are you"])
        XCTAssertEqual(TextNormalize.stripCJKSpaces(joined), "Hello there. How are you")
    }

    // 日本語の確定片は確定時の正規化で空白が消え、従来どおり詰まる
    func testJapaneseSegmentsStayContiguous() {
        let joined = StreamingTranscriber.joinSegments(["今日は晴れです。", "明日は雨です"])
        XCTAssertEqual(TextNormalize.stripCJKSpaces(joined), "今日は晴れです。明日は雨です")
    }

    // 1 片だけ・空なら余計な空白を足さない
    func testSingleAndEmpty() {
        XCTAssertEqual(StreamingTranscriber.joinSegments(["only"]), "only")
        XCTAssertEqual(StreamingTranscriber.joinSegments([]), "")
    }
}
