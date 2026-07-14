//
//  EphemeralTokenReuseTests.swift
//  短命トークンのキャッシュ再利用閾値のテスト（即時入力の周期コールド窓の根治）
//
//  warm 先読みは残 warmMinRemaining(360 秒)未満で強制再取得し、録音開始は残 15 秒で再利用する。
//  この 2 閾値の分離が「満了〜次 warm tick」の周期コールド窓を消す核心なので回帰化する。
//  （以前は warm も 15 秒閾値で短絡し、満了直前まで一切更新されなかった）
//

import XCTest
@testable import voicekey

final class EphemeralTokenReuseTests: XCTestCase {

    // warm 先読み(360s): 残 300 秒は満了に近すぎるので再取得させる（＝コールド窓を作らせない）
    func testWarmThresholdRefetchesNearExpiry() {
        XCTAssertFalse(BackendClient.canReuseCachedToken(remaining: 300, minRemaining: 360),
                       "残 300 秒は warm 閾値 360 秒未満＝満了前に強制再取得すべき")
        XCTAssertTrue(BackendClient.canReuseCachedToken(remaining: 400, minRemaining: 360),
                      "残 400 秒はまだ余裕があるので再利用でよい")
    }

    // 録音開始(15s): 残 300 秒はそのまま再利用する（クリティカルパスは従来どおり往復ゼロ）
    func testRecordStartReusesWhilePlentyLeft() {
        XCTAssertTrue(BackendClient.canReuseCachedToken(remaining: 300, minRemaining: 15))
        XCTAssertTrue(BackendClient.canReuseCachedToken(remaining: 16, minRemaining: 15))
        XCTAssertFalse(BackendClient.canReuseCachedToken(remaining: 10, minRemaining: 15),
                       "残 10 秒は接続確立に不足＝再取得すべき")
    }

    // warmMinRemaining は暖機間隔 240s + 満了前マージン 120s ＝ 360s（満了 120 秒以上前に必ず更新される）
    func testWarmMinRemainingLeavesMarginOverWarmInterval() {
        XCTAssertEqual(BackendClient.warmMinRemaining, 360)
        XCTAssertGreaterThan(BackendClient.warmMinRemaining, 240,
                             "warm 閾値は暖機間隔 240s より大きくないと満了前更新を保証できない")
    }
}
