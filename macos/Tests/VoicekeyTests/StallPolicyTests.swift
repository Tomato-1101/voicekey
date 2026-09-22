//
//  StallPolicyTests.swift
//  ハング検出（無応答と見なす時間）と世代ガード（遅れて届いた結果を捨てる）の単体テスト
//
//  実オーディオ・実タイマーの長待ちは不要。判断だけを純ロジックに切り出してあるので、
//  「何秒で打ち切るか」「どの結果を捨てるか」をここで固定する。
//

import XCTest
@testable import voicekey

final class StallPolicyTests: XCTestCase {

    // MARK: - タイムアウト値

    // ローカル（Apple）認識だけ上限が短い（ネットワークを待たないため）
    func testLocalRecognitionHasShorterTimeout() {
        XCTAssertEqual(StallPolicy.transcribeTimeout(for: .appleLocal), 20)
        XCTAssertLessThan(
            StallPolicy.transcribeTimeout(for: .appleLocal),
            StallPolicy.transcribeTimeout(for: .deepgram)
        )
    }

    // クラウド系は 60 秒（分割並列送信やリトライを含めても正当に超えない値）
    func testCloudBackendsUseLongTimeout() {
        for backend in [Backend.deepgram, .groq, .elevenlabs, .openai, .openaiLive] {
            XCTAssertEqual(
                StallPolicy.transcribeTimeout(for: backend), 60,
                "\(backend.rawValue) の上限が想定と違う"
            )
        }
    }

    // バックエンド不明（コンテキストが取れなかった）でも打ち切りは効く
    func testUnknownBackendFallsBackToLongTimeout() {
        XCTAssertEqual(StallPolicy.transcribeTimeout(for: nil), 60)
    }

    // 録音開始は正常時が数十 ms なので、5 秒あれば正当に遅いケースで誤発火しない
    func testRecordStartTimeoutIsShortButSafe() {
        XCTAssertEqual(StallPolicy.recordStartTimeout, 5)
        XCTAssertLessThan(StallPolicy.recordStartTimeout, StallPolicy.localTranscribeTimeout)
    }

    // MARK: - 詰まった制御キューからの復帰（自動再起動の上限）

    // まだ一度も再起動していなければ再起動してよい
    func testRelaunchAllowedWhenNeverRelaunched() {
        XCTAssertTrue(StallPolicy.shouldRelaunchForStall(relaunchTimes: [], now: 10_000))
    }

    // 窓内 2 回までなら次の 1 回を許す
    func testRelaunchAllowedBelowLimit() {
        XCTAssertTrue(
            StallPolicy.shouldRelaunchForStall(relaunchTimes: [9_500, 9_800], now: 10_000)
        )
    }

    // 窓内 3 回に達したら再起動しない（再起動ループを作らないための頭打ち）
    func testRelaunchBlockedAtLimit() {
        XCTAssertFalse(
            StallPolicy.shouldRelaunchForStall(relaunchTimes: [9_000, 9_500, 9_800], now: 10_000)
        )
    }

    // 窓（30 分）より古い記録は数えない＝時間が経てばまた自動復帰できる
    func testOldRelaunchesFallOutOfWindow() {
        let now: TimeInterval = 100_000
        let old = now - StallPolicy.stallRelaunchWindow - 1
        XCTAssertTrue(
            StallPolicy.shouldRelaunchForStall(relaunchTimes: [old, old, old], now: now)
        )
        XCTAssertEqual(StallPolicy.prunedRelaunchTimes([old, old, old], now: now), [])
    }

    // 時計が巻き戻ったときの未来の記録は捨てる（上限に永久に張り付かせない）
    func testFutureRelaunchTimesAreDropped() {
        XCTAssertEqual(StallPolicy.prunedRelaunchTimes([20_000], now: 10_000), [])
    }

    // MARK: - 構成変更後のタップ黙死判定

    // 通知のあとにバッファが届いていれば何もしない（通知は 1 日 100 回来る誤発火が大半）
    func testTapAliveWhenBufferArrivedAfterNotice() {
        XCTAssertFalse(
            TapStallPolicy.needsRestart(lastBufferUptime: 100.5, notifiedAt: 100.0)
        )
    }

    // 通知以降に 1 つも届いていなければ再構成する（19.8 秒押して 6.9 秒しか録れない事故）
    func testTapDeadWhenNoBufferSinceNotice() {
        XCTAssertTrue(
            TapStallPolicy.needsRestart(lastBufferUptime: 99.9, notifiedAt: 100.0)
        )
    }

    // MARK: - 世代ガード

    // 打ち切った世代の結果だけを捨てる（他の世代は素通し）
    func testAbandonedGenerationIsRejected() {
        var sessions = AbandonedSessions()
        sessions.abandon(7)
        XCTAssertTrue(sessions.contains(7))
        XCTAssertFalse(sessions.contains(8))
        XCTAssertFalse(sessions.contains(6))
    }

    // 何も打ち切っていなければ、すべての結果を受け取る（正常経路に影響しない）
    func testNothingIsRejectedByDefault() {
        let sessions = AbandonedSessions()
        XCTAssertFalse(sessions.contains(0))
        XCTAssertFalse(sessions.contains(1))
        XCTAssertEqual(sessions.generations, [])
    }

    // 遅れて届いた完了は「打ち切り済み」として 1 回だけ消費される
    // （2 回目以降は普通の完了として扱われる＝二重に片付けない）
    func testConsumeReportsAbandonedOnlyOnce() {
        var sessions = AbandonedSessions()
        sessions.abandon(3)
        XCTAssertTrue(sessions.consume(3))
        XCTAssertFalse(sessions.consume(3))
        XCTAssertFalse(sessions.contains(3))
    }

    // 打ち切っていない世代の完了は、そのまま通常の完了として扱う
    func testConsumeReturnsFalseForLiveGeneration() {
        var sessions = AbandonedSessions()
        sessions.abandon(1)
        XCTAssertFalse(sessions.consume(2))
        XCTAssertTrue(sessions.contains(1), "無関係な世代の完了で台帳を壊さない")
    }

    // 同じ世代を二重に打ち切っても台帳は増えない
    func testAbandonIsIdempotent() {
        var sessions = AbandonedSessions()
        sessions.abandon(5)
        sessions.abandon(5)
        XCTAssertEqual(sessions.generations, [5])
    }

    // 結果が一生届かない世代がたまり続けないよう、古いものから落とす
    func testLedgerKeepsOnlyRecentGenerations() {
        var sessions = AbandonedSessions()
        for generation in 1...(AbandonedSessions.capacity + 5) {
            sessions.abandon(generation)
        }
        XCTAssertEqual(sessions.generations.count, AbandonedSessions.capacity)
        XCTAssertFalse(sessions.contains(1), "いちばん古い世代が残っている")
        XCTAssertTrue(
            sessions.contains(AbandonedSessions.capacity + 5), "直近の世代が落とされている"
        )
    }

    // MARK: - 待機中のエンジン入れ替え

    func testIdleEngineRefreshWaitsForInterval() {
        XCTAssertFalse(StallPolicy.shouldRefreshIdleEngine(preparedAt: 1000, now: 1000))
        XCTAssertFalse(
            StallPolicy.shouldRefreshIdleEngine(
                preparedAt: 1000, now: 1000 + StallPolicy.engineRefreshInterval - 1))
    }

    func testIdleEngineRefreshAtInterval() {
        XCTAssertTrue(
            StallPolicy.shouldRefreshIdleEngine(
                preparedAt: 1000, now: 1000 + StallPolicy.engineRefreshInterval))
        XCTAssertTrue(StallPolicy.shouldRefreshIdleEngine(preparedAt: 1000, now: 99999))
    }

    /// 実測した詰まりの最短（6 分ほったらかし）より必ず手前で入れ替わること
    func testIdleEngineRefreshIsShorterThanObservedStallGap() {
        XCTAssertLessThan(StallPolicy.engineRefreshInterval, 6 * 60)
    }
}
