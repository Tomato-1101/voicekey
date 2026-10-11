//
//  AppleFailureStreakTests.swift
//  Apple 先出し翻訳の「連続失敗で格下げ」判定の単体テスト
//
//  1 回の失敗でそのセッション中ずっと遅いクラウド先行になる不具合（レビュー指摘 中3）への回帰。
//

import XCTest

@testable import voicekey

final class AppleFailureStreakTests: XCTestCase {

    /// 1・2 回の失敗では格下げしない。3 回続いて初めて格下げ
    func testDegradesOnlyAfterThreeConsecutiveFailures() {
        var streak = AppleFailureStreak()
        XCTAssertEqual(AppleFailureStreak.limit, 3)
        XCTAssertFalse(streak.recordFailure())
        XCTAssertFalse(streak.recordFailure())
        XCTAssertTrue(streak.recordFailure())
    }

    /// 間に成功が挟まれば数え直す（飛び飛びの失敗では格下げしない）
    func testSuccessResetsTheStreak() {
        var streak = AppleFailureStreak()
        XCTAssertFalse(streak.recordFailure())
        XCTAssertFalse(streak.recordFailure())
        streak.recordSuccess()
        XCTAssertFalse(streak.recordFailure())
        XCTAssertFalse(streak.recordFailure())
        XCTAssertTrue(streak.recordFailure())
    }

    /// 再準備・セッション開始の reset でも数え直す
    func testResetClearsTheStreak() {
        var streak = AppleFailureStreak()
        _ = streak.recordFailure()
        _ = streak.recordFailure()
        streak.reset()
        XCTAssertEqual(streak.count, 0)
        XCTAssertFalse(streak.recordFailure())
    }

    /// Apple 先出しの翻訳器は、Apple が成功したら成功フックを、失敗したら失敗フックを呼ぶ
    func testConfirmedTranslatorCallsSuccessAndFailureHooks() async throws {
        guard #available(macOS 26.0, *) else { throw XCTSkip("ライブ字幕は macOS 26 以降") }
        let ok = CaptionService.confirmedTranslator(
            apple: MockTranslator(latency: 0), cloud: MockTranslator(latency: 0), appleFirst: true,
            onAppleFailure: { XCTFail("成功時に失敗フックが呼ばれた") },
            onAppleSuccess: { Self.successCount.withLock { $0 += 1 } }
        )
        _ = try await ok.translate("Hello.", context: [])
        XCTAssertEqual(Self.successCount.withLock { $0 }, 1)
    }

    private static let successCount = LockedCounter()
}

/// テスト用の簡易カウンタ
final class LockedCounter: @unchecked Sendable {
    private let lock = NSLock()
    private var value = 0
    func withLock<T>(_ body: (inout Int) -> T) -> T {
        lock.lock(); defer { lock.unlock() }
        return body(&value)
    }
}
