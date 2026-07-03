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

    // MARK: - 長文並列分割の目標数・再探索・順序保証

    /// 発話 run（振幅・フレーム数）を無音フレームで区切って合成音声を作る。
    /// gapFrames = run 間の無音フレーム数（480 サンプル=30ms/フレーム）。
    /// speechRegions は各区間に前後 250ms（4000 サンプル）の余白を足すため、
    /// mergeRegions 上の区間ギャップ ≒ gapFrames*480 - 8000 サンプルになる。
    /// → 0.7 秒境界(11200)で割れるのは gap ≧ 41 frames、0.35 秒(5600)では gap ≧ 29 frames。
    private func build(runs: [(amp: Float, frames: Int)], gapFrames: Int) -> [Float] {
        var s: [Float] = []
        for (idx, run) in runs.enumerated() {
            if idx > 0 {
                s.append(contentsOf: [Float](repeating: 0, count: frameLen * gapFrames))
            }
            s.append(contentsOf: [Float](repeating: run.amp, count: frameLen * run.frames))
        }
        return s
    }

    // 多数の無音境界を持つ約30秒の長文は 3 セグメント以下に均等化される
    func testManyBoundariesCappedAtThree() {
        // 6 run × 130 frames(3.9s) ＋ gap 42(0.7s 境界で必ず割れる) ≒ 29.7 秒 → 目標 3
        let runs: [(amp: Float, frames: Int)] = Array(repeating: (0.5, 130), count: 6)
        let segs = VoiceActivity.segment(build(runs: runs, gapFrames: 42))
        XCTAssertLessThanOrEqual(segs.count, 3, "目標数 3 を超えて分割された")
        XCTAssertGreaterThanOrEqual(segs.count, 2, "並列分割されていない")
    }

    // 24 秒未満の長文は 2 セグメント以下に均等化される
    func testShorterLongFormCappedAtTwo() {
        // 4 run × 100 frames(3s) ＋ gap 42 ≒ 15.8 秒(< 24 秒) → 目標 2
        let runs: [(amp: Float, frames: Int)] = Array(repeating: (0.5, 100), count: 4)
        let segs = VoiceActivity.segment(build(runs: runs, gapFrames: 42))
        XCTAssertLessThanOrEqual(segs.count, 2, "目標数 2 を超えて分割された")
        XCTAssertGreaterThanOrEqual(segs.count, 2, "並列分割されていない")
    }

    // 0.7 秒では割れない短いポーズ(gap 35 frames)でも、再探索で 2 本以上に分割される
    func testRetrySplitsWhenNoLongSilence() {
        // gap 35 は 0.7s 境界では割れず(≦40)、再探索の 0.35s では割れる(≧29)
        let runs: [(amp: Float, frames: Int)] = Array(repeating: (0.5, 167), count: 3)
        let segs = VoiceActivity.segment(build(runs: runs, gapFrames: 35))
        XCTAssertGreaterThanOrEqual(segs.count, 2, "再探索で分割されていない(1 本送信のまま)")
        XCTAssertLessThanOrEqual(segs.count, 3, "目標数を超えて分割された")
    }

    // 分割結果を順に連結すると発話が元の順序どおり並ぶ(隣接結合のみ・入れ替えなし)
    func testSegmentsPreserveOrder() {
        // 振幅で run を識別(0.3 → 0.5 → 0.7)。gap 42 で 0.7s 境界により 3 区間に割れる
        let runs: [(amp: Float, frames: Int)] = [(0.3, 130), (0.5, 130), (0.7, 130)]
        let segs = VoiceActivity.segment(build(runs: runs, gapFrames: 42))
        XCTAssertGreaterThanOrEqual(segs.count, 2)
        // 連結して各振幅が最初に現れる位置が 0.3 < 0.5 < 0.7 の順であることを確認
        let flat = segs.flatMap { $0 }
        guard let i03 = flat.firstIndex(of: 0.3),
              let i05 = flat.firstIndex(of: 0.5),
              let i07 = flat.firstIndex(of: 0.7) else {
            return XCTFail("いずれかの発話振幅が結果に含まれていない")
        }
        XCTAssertLessThan(i03, i05, "0.3 が 0.5 より後ろ＝順序が崩れている")
        XCTAssertLessThan(i05, i07, "0.5 が 0.7 より後ろ＝順序が崩れている")
    }

    // limitSegmentCount 単体: 末尾に長いセグメントがあってもバケットが目標未満に潰れない
    func testLimitSegmentCountKeepsTargetWithTrailingLong() {
        let short = [Float](repeating: 0.5, count: 32000)   // 2s
        let long = [Float](repeating: 0.5, count: 128000)   // 8s
        let segs = VoiceActivity.limitSegmentCount([short, short, long], target: 2)
        XCTAssertEqual(segs.count, 2, "末尾の長区間に飲まれてバケットが 1 本に潰れた")
        // 隣接結合のみ＝入れ替え無しなので、連結長は入力の総和と一致する
        XCTAssertEqual(segs.flatMap { $0 }.count, 32000 + 32000 + 128000)
    }
}
