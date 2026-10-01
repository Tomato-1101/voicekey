//
//  ChunkRoutingTests.swift
//  ストリーミング送信先の結び付け（保留／稼働の 2 段）のテスト
//
//  離鍵直後の「最後の 2〜3 文字」が欠ける不具合の回帰。stop 内のフラッシュ（離鍵直前の声）が
//  その録音の送り先へ届くこと、次の録音の送り先を前の stop が消さないこと、前の録音の音を
//  次の送り先へ流さないことを、実デバイス無しで状態機械だけ確かめる。
//  AudioRecorder での呼び順は start 呼び出し（main）→ claim、start 成功（制御キュー）→ activate、
//  stop のフラッシュ後（制御キュー）→ deactivate。handleBuffer は active へだけ送る。
//

import XCTest
@testable import voicekey

final class ChunkRoutingTests: XCTestCase {

    /// 停止する録音の末尾は、次の録音が送り先を張った後でもその録音の送り先へ届く
    func testTailOfStoppingRecordingReachesItsHandler() {
        var r = ChunkRouting<String>()
        r.set("A")
        r.activate(r.claim())          // 録音 A 開始
        r.set("B")                     // 素早い再押下: A の stop が走る前に B が張る
        let claimB = r.claim()         // B の start 呼び出し（main）
        XCTAssertEqual(r.active, "A", "A の stop 内フラッシュが A へ届かない（末尾の取りこぼし）")
        r.deactivate()                 // A の stop（フラッシュの後）
        XCTAssertNil(r.active, "stop 後も古い送り先を握っている")
        r.activate(claimB)             // B の start 成功
        XCTAssertEqual(r.active, "B", "前の stop が次の録音の送り先を消した")
    }

    /// B が張っただけ（start 前）の段階で A の stop が走っても、B の保留は残り A にも混ざらない
    func testStopDoesNotClearPendingOfNextRecording() {
        var r = ChunkRouting<String>()
        r.set("A")
        r.activate(r.claim())
        r.set("B")
        XCTAssertEqual(r.active, "A", "保留中の B へ A の音が流れる")
        r.deactivate()
        XCTAssertEqual(r.pending, "B")
        r.activate(r.claim())
        XCTAssertEqual(r.active, "B")
    }

    /// ストリーミングしない録音（送り先なし）は前の録音の送り先を引き継がない
    func testRecordingWithoutHandlerDoesNotInheritPrevious() {
        var r = ChunkRouting<String>()
        r.set("A")
        r.activate(r.claim())
        r.deactivate()
        r.activate(r.claim())          // 送り先を張らずに開始（REST 経路）
        XCTAssertNil(r.active)
    }

    /// 引き取った後に張られた送り先は、引き取り済みの録音に影響しない（次の録音の分になる）
    func testSetAfterClaimGoesToNextRecording() {
        var r = ChunkRouting<String>()
        r.set("A")
        let claimA = r.claim()
        r.set("B")
        r.activate(claimA)
        XCTAssertEqual(r.active, "A")
        XCTAssertEqual(r.pending, "B")
    }

    /// nil（キャンセル）は稼働中の送信も止める
    func testCancelStopsActiveDelivery() {
        var r = ChunkRouting<String>()
        r.set("A")
        r.activate(r.claim())
        r.set(nil)
        XCTAssertNil(r.active)
        XCTAssertNil(r.pending)
    }

    /// 引き取った後（開始待ちの間）にキャンセルされたら、開始しても稼働にしない
    /// （録音開始タイムアウトで畳んだ後に、詰まっていた start が遅れて成功するケース）
    func testCancelAfterClaimPreventsActivation() {
        var r = ChunkRouting<String>()
        r.set("A")
        let claimA = r.claim()
        r.set(nil)
        r.activate(claimA)
        XCTAssertNil(r.active, "キャンセル済みの送り先が録音開始で復活した")

        // キャンセルの後に張った次の録音は普通に稼働する
        r.set("B")
        r.activate(r.claim())
        XCTAssertEqual(r.active, "B")
    }
}
