//
//  VoiceActivityTests.swift
//  VAD の孤立ノイズ判定（#22）と短セグメント結合（#23）の単体テスト
//
//  Python 版 tests/test_vad.py（TestSegment / TestSegmentMerge）と同じ観点で、
//  両 OS の閾値・挙動を揃える。エネルギー基準（RMS>=0.02・480 サンプル/フレーム）の
//  孤立ノイズ判定は condense() 経由、短区間結合は純粋関数 mergeShortSegments() で検証する。
//

import XCTest
@testable import voicekey

final class VoiceActivityTests: XCTestCase {

    private let frameLen = 480

    /// 指定フレームだけ発話（0.5）にした音声を作る
    private func samples(activeFrames: [Int], totalFrames: Int) -> [Float] {
        var s = [Float](repeating: 0, count: frameLen * totalFrames)
        for f in activeFrames {
            for i in 0..<frameLen { s[f * frameLen + i] = 0.5 }
        }
        return s
    }

    // MARK: - #22 孤立ノイズ判定

    // 離れた単発フレーム 2 個（各 run 長 1）はノイズ → 発話なし（condense は nil）
    func testTwoIsolatedFramesAreNoise() {
        let s = samples(activeFrames: [2, 20], totalFrames: 30)
        XCTAssertNil(VoiceActivity.condense(s))
    }

    // 連続 2 フレーム（≒minSpeechFrames）の run は発話とみなす（condense は非 nil）
    func testConsecutiveRunIsSpeech() {
        let s = samples(activeFrames: [2, 3], totalFrames: 30)
        XCTAssertNotNil(VoiceActivity.condense(s))
    }

    // 単発ノイズ + 連続 run の混在 → run だけ採用（発話あり）
    func testIsolatedNoiseDroppedRunKept() {
        let s = samples(activeFrames: [1, 10, 11, 12], totalFrames: 30)
        XCTAssertNotNil(VoiceActivity.condense(s))
    }

    // MARK: - #23 短セグメント結合

    private let minLen = 32000  // 2 秒（16kHz）
    private func block(_ count: Int) -> [Float] { [Float](repeating: 0.5, count: count) }

    // 先頭の短区間は直後の長区間と結合される
    func testLeadingShortMergedForward() {
        let segs = VoiceActivity.mergeShortSegments(
            [block(16000), block(48000), block(48000)], minLen: minLen
        )
        assertNoShortSegment(segs, count: 2)
    }

    // 中間の短区間は直前へ結合される
    func testMiddleShortMerged() {
        let segs = VoiceActivity.mergeShortSegments(
            [block(48000), block(16000), block(48000)], minLen: minLen
        )
        assertNoShortSegment(segs, count: 2)
    }

    // 末尾の短区間（報告バグ）は直前へ結合される
    func testTrailingShortMerged() {
        let segs = VoiceActivity.mergeShortSegments(
            [block(48000), block(48000), block(16000)], minLen: minLen
        )
        assertNoShortSegment(segs, count: 2)
    }

    // すべて長区間なら結合せずそのまま
    func testAllLongUnchanged() {
        let segs = VoiceActivity.mergeShortSegments(
            [block(48000), block(48000)], minLen: minLen
        )
        assertNoShortSegment(segs, count: 2)
    }

    private func assertNoShortSegment(_ segs: [[Float]], count: Int) {
        XCTAssertEqual(segs.count, count)
        for seg in segs {
            XCTAssertGreaterThanOrEqual(seg.count, minLen, "結合されず短区間が残っている")
        }
    }
}
