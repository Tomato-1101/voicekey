//
//  DoubleTapPolicyTests.swift
//  ダブルタップ（auto_enter）判定の単体テスト
//
//  ユーザー指摘「ダブルタップでの入力がたまに検出されない」への対処を恒久の回帰テストにする。
//  旧実装は 0.4 秒の窓を「1 打目のホールド」と「離鍵→2 打目の押下」の 2 か所に効かせており、
//  人間のタップ（400ms を普通に超える）が簡単に窓から外れていた。
//  testRescuedByNewThresholds が、その取りこぼしが救われることを機械的に示す。
//

import XCTest

@testable import voicekey

final class DoubleTapPolicyTests: XCTestCase {

    /// システム設定が既定（0.5 秒）のときの間隔窓
    private let window = DoubleTapPolicy.gapWindow(doubleClickInterval: 0.5)

    /// 1 打目を組み立てる（押下 t0、hold 秒だけ押しっぱなし）
    private func tap(slotId: Int = 1, at t0: TimeInterval, hold: TimeInterval) -> DoubleTapPolicy.Tap {
        DoubleTapPolicy.Tap(slotId: slotId, pressedAt: t0, releasedAt: t0 + hold)
    }

    // MARK: - 基本

    /// 素早い 2 回タップ → 成立
    func testQuickDoubleTapIsAccepted() {
        let first = tap(at: 100.0, hold: 0.12)
        let decision = DoubleTapPolicy.decide(
            first: first, slotId: 1, pressedAt: 100.0 + 0.12 + 0.18, gapWindow: window)
        XCTAssertEqual(decision, .accepted)
        XCTAssertTrue(decision.isDoubleTap)
    }

    /// 1 打目が長い（口述）→ 不成立。
    /// 長い口述の直後に素早く次の録音を始めただけで Enter が自動送信される事故を防ぐガード。
    func testLongHoldIsRejected() {
        let first = tap(at: 100.0, hold: 3.0)
        let decision = DoubleTapPolicy.decide(
            first: first, slotId: 1, pressedAt: 103.1, gapWindow: window)
        XCTAssertEqual(decision, .holdTooLong)
        XCTAssertFalse(decision.isDoubleTap)
    }

    /// 間隔が空きすぎ → 不成立
    func testTooSlowSecondTapIsRejected() {
        let first = tap(at: 100.0, hold: 0.2)
        // press-to-press の許容は kTapHoldMax + 窓 = 1.25 秒。2.0 秒はそれを大きく超える
        let decision = DoubleTapPolicy.decide(
            first: first, slotId: 1, pressedAt: 102.0, gapWindow: window)
        XCTAssertEqual(decision, .tooSlow)
    }

    /// 別スロットのタップ → 不成立（スロット 1 の直後にスロット 2 を叩いても Enter しない）
    func testOtherSlotIsRejected() {
        let first = tap(slotId: 1, at: 100.0, hold: 0.12)
        let decision = DoubleTapPolicy.decide(
            first: first, slotId: 2, pressedAt: 100.3, gapWindow: window)
        XCTAssertEqual(decision, .otherSlot)
    }

    /// 1 打目が無い（アプリ起動後の最初の押下など）→ 不成立
    func testNoFirstTapIsRejected() {
        let decision = DoubleTapPolicy.decide(
            first: nil, slotId: 1, pressedAt: 100.0, gapWindow: window)
        XCTAssertEqual(decision, .noFirstTap)
    }

    // MARK: - 境界値

    /// ホールドがちょうど上限 → 成立（境界は成立側に倒す）
    func testHoldExactlyAtLimitIsAccepted() {
        let first = tap(at: 100.0, hold: kTapHoldMax)
        let decision = DoubleTapPolicy.decide(
            first: first, slotId: 1, pressedAt: 100.0 + kTapHoldMax + 0.1, gapWindow: window)
        XCTAssertEqual(decision, .accepted)
    }

    /// ホールドがわずかに超過 → 不成立
    func testHoldSlightlyOverLimitIsRejected() {
        let first = tap(at: 100.0, hold: kTapHoldMax + 0.01)
        let decision = DoubleTapPolicy.decide(
            first: first, slotId: 1, pressedAt: 100.0 + kTapHoldMax + 0.11, gapWindow: window)
        XCTAssertEqual(decision, .holdTooLong)
    }

    /// press-to-press がちょうど上限（hold 上限＋窓）→ 成立
    func testPressToPressExactlyAtLimitIsAccepted() {
        let first = tap(at: 100.0, hold: 0.2)
        let decision = DoubleTapPolicy.decide(
            first: first, slotId: 1, pressedAt: 100.0 + kTapHoldMax + window, gapWindow: window)
        XCTAssertEqual(decision, .accepted)
    }

    /// press-to-press がわずかに超過 → 不成立
    func testPressToPressSlightlyOverLimitIsRejected() {
        let first = tap(at: 100.0, hold: 0.2)
        let decision = DoubleTapPolicy.decide(
            first: first, slotId: 1, pressedAt: 100.0 + kTapHoldMax + window + 0.01,
            gapWindow: window)
        XCTAssertEqual(decision, .tooSlow)
    }

    // MARK: - 間隔窓のクランプ

    /// システム設定が既定より短くても下限まで（狭めない＝取りこぼしに戻さない）
    func testGapWindowClampsToMinimum() {
        XCTAssertEqual(DoubleTapPolicy.gapWindow(doubleClickInterval: 0.2), kDoubleTapGapMin)
    }

    /// システム設定を伸ばしていれば追従する（ただし上限まで）
    func testGapWindowFollowsSystemSettingUpToMaximum() {
        XCTAssertEqual(DoubleTapPolicy.gapWindow(doubleClickInterval: 0.9), 0.9)
        XCTAssertEqual(DoubleTapPolicy.gapWindow(doubleClickInterval: 5.0), kDoubleTapGapMax)
    }

    // MARK: - 3 回連続タップ

    /// 3 回連続で叩いても暴走しない（2 打目で成立 → 1 打目を使い切る → 3 打目は通常録音）。
    /// AppController は成立時に lastTap を nil にするので、その状態を再現して確かめる。
    func testTripleTapDoesNotChain() {
        let first = tap(at: 100.0, hold: 0.12)
        let second = DoubleTapPolicy.decide(
            first: first, slotId: 1, pressedAt: 100.3, gapWindow: window)
        XCTAssertEqual(second, .accepted)

        // 成立した 1 打目は消費済み（AppController が lastTap = nil にする）
        let third = DoubleTapPolicy.decide(
            first: nil, slotId: 1, pressedAt: 100.6, gapWindow: window)
        XCTAssertEqual(third, .noFirstTap)
    }

    /// 3 打目が「新しい 1 打目」として記録されたあとの 4 打目は、また普通に成立してよい
    /// （＝連続入力を殺さない。暴走しないこと＝連鎖しないこと であって、封じることではない）
    func testTapAfterConsumedPairCanFormNewPair() {
        let third = tap(at: 100.6, hold: 0.1)
        let fourth = DoubleTapPolicy.decide(
            first: third, slotId: 1, pressedAt: 100.9, gapWindow: window)
        XCTAssertEqual(fourth, .accepted)
    }

    // MARK: - 旧しきい値との対比（実際に取りこぼしていた入力が救われること）

    /// 旧実装（hold 0.4 / 離鍵→押下 0.4 の二段構え）では落ちていたが、新実装では成立するケース。
    /// 「しっかり押してから素早く 2 回叩いた」典型で、これが「たまに検出されない」の正体だった。
    func testRescuedByNewThresholds() {
        /// 旧実装の判定を再現（hold < 0.4 で記録し、離鍵→押下 < 0.4 で成立）
        func oldDecide(first: DoubleTapPolicy.Tap, secondPressedAt: TimeInterval) -> Bool {
            guard first.hold < 0.4 else { return false }  // 記録されない＝以後どう叩いても不成立
            return secondPressedAt - first.releasedAt < 0.4
        }

        // ケース A: 1 打目のホールドが 0.52 秒（人の「タップ」は 400ms を普通に超える）
        let caseA = tap(at: 100.0, hold: 0.52)
        let caseAsecond = 100.0 + 0.52 + 0.15
        XCTAssertFalse(oldDecide(first: caseA, secondPressedAt: caseAsecond), "旧実装では落ちる前提")
        XCTAssertEqual(
            DoubleTapPolicy.decide(
                first: caseA, slotId: 1, pressedAt: caseAsecond, gapWindow: window),
            .accepted, "取りこぼしていた入力が救われていない")

        // ケース B: ホールドは短いが 2 打目までが 0.45 秒（旧 0.4 秒窓をわずかに超える）
        let caseB = tap(at: 200.0, hold: 0.15)
        let caseBsecond = 200.0 + 0.15 + 0.45
        XCTAssertFalse(oldDecide(first: caseB, secondPressedAt: caseBsecond), "旧実装では落ちる前提")
        XCTAssertEqual(
            DoubleTapPolicy.decide(
                first: caseB, slotId: 1, pressedAt: caseBsecond, gapWindow: window),
            .accepted, "取りこぼしていた入力が救われていない")
    }

    /// 逆に、旧実装のガードの意図（長い口述の直後は Enter しない）は新実装でも維持されていること
    func testDictationGuardIsPreserved() {
        let dictation = tap(at: 100.0, hold: 5.0)
        XCTAssertEqual(
            DoubleTapPolicy.decide(
                first: dictation, slotId: 1, pressedAt: 105.2, gapWindow: window),
            .holdTooLong)
    }
}
