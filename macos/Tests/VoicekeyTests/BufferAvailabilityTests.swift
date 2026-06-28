//
//  BufferAvailabilityTests.swift
//  録音 buffer の取り出し可否ロジックのテスト（#20）
//
//  デバイス切断で recording=false になっても、録音済み buffer を一度だけ
//  取り出せること（取りこぼさない・二重取得しない）を検証する。
//  AVAudioEngine には触れず、状態機械だけを単体で確かめる。
//

import XCTest
@testable import voicekey

final class BufferAvailabilityTests: XCTestCase {

    /// 通常停止: 録音中なら取り出せて、二度目は取り出せない（二重取得防止）
    func testNormalStopDrainsOnce() {
        let gate = BufferAvailability()
        gate.markAvailable()

        XCTAssertTrue(gate.consume(recording: true))   // stop で取り出す
        XCTAssertFalse(gate.consume(recording: false))  // 二度目は空
    }

    /// デバイス切断: recording=false でも未取得 buffer は一度だけ取り出せる（#20 本丸）
    func testDeviceLostStillDrainsCapturedBufferOnce() {
        let gate = BufferAvailability()
        gate.markAvailable()              // 録音開始
        // ここで構成変更からの復帰失敗 → 呼び出し側が recording=false にする（gate は触らない）

        XCTAssertTrue(gate.consume(recording: false))   // 切断後でも取得済み音声を回収
        XCTAssertFalse(gate.consume(recording: false))  // 二重取得は防ぐ
    }

    /// 録音していない・buffer も無いなら何も取り出さない
    func testNothingToDrain() {
        let gate = BufferAvailability()
        XCTAssertFalse(gate.consume(recording: false))
    }

    /// 取り出した後に再度録音開始すれば、また取り出せる（セッションをまたいで再利用）
    func testReusableAcrossSessions() {
        let gate = BufferAvailability()
        gate.markAvailable()
        XCTAssertTrue(gate.consume(recording: true))
        XCTAssertFalse(gate.consume(recording: false))

        gate.markAvailable()                            // 次の録音
        XCTAssertTrue(gate.consume(recording: true))
        XCTAssertFalse(gate.consume(recording: false))
    }
}
