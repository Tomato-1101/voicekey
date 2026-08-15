//
//  DoubleTapPolicyTests.swift
//  ダブルタップ（auto_enter）判定の単体テスト
//
//  ユーザー指摘 2 件への対処を恒久の回帰テストにする。
//  (1)「ダブルタップでの入力がたまに検出されない」
//      旧実装は 0.4 秒の窓を「1 打目のホールド」と「離鍵→2 打目の押下」の 2 か所に効かせており、
//      人間のタップ（400ms を普通に超える）が簡単に窓から外れていた。
//      testRescuedByNewThresholds が、その取りこぼしが救われることを機械的に示す。
//  (2)「最初のタップの音声を消さず、開始タイミングを揃えてほしい」
//      1 打目の離鍵で録音を止めず保留に入り、2 打目では録音を作り直さない設計にした。
//      この判定器はその「保留（Pending）」を入力に取る。録音を止めない結線自体は
//      AppController 側の状態遷移なので、ここでは判定の境界だけを固定する。
//

import XCTest

@testable import voicekey

final class DoubleTapPolicyTests: XCTestCase {

    /// システム設定が既定（0.5 秒）のときの間隔窓
    private let window = DoubleTapPolicy.gapWindow(doubleClickInterval: 0.5)

    /// 保留中の 1 打目を組み立てる（押下 t0、hold 秒だけ押しっぱなし）
    private func pending(
        slotId: Int = 1, at t0: TimeInterval, hold: TimeInterval
    ) -> DoubleTapPolicy.Pending {
        DoubleTapPolicy.Pending(slotId: slotId, pressedAt: t0, releasedAt: t0 + hold)
    }

    // MARK: - 1 打目を「タップ」として保留に入れるか

    /// 短いタップは保留に入る（＝録音を止めずに 2 打目を待つ）
    func testShortHoldIsTapCandidate() {
        XCTAssertTrue(DoubleTapPolicy.isTapCandidate(hold: 0.12))
        XCTAssertTrue(DoubleTapPolicy.isTapCandidate(hold: 0.52), "人のタップは 400ms を普通に超える")
    }

    /// 口述の長さは保留に入れない（長く喋った直後に押し直しても Enter を撃たないガード）
    func testLongHoldIsNotTapCandidate() {
        XCTAssertFalse(DoubleTapPolicy.isTapCandidate(hold: 3.0))
        XCTAssertFalse(DoubleTapPolicy.isTapCandidate(hold: 5.0), "長い口述の直後に Enter しない")
    }

    /// 境界はちょうどで成立側に倒す（境界で「たまに落ちる」のを避ける）
    func testTapCandidateBoundary() {
        XCTAssertTrue(DoubleTapPolicy.isTapCandidate(hold: kTapHoldMax))
        XCTAssertFalse(DoubleTapPolicy.isTapCandidate(hold: kTapHoldMax + 0.01))
    }

    // MARK: - 2 打目の判定

    /// 素早い 2 回タップ → 成立
    func testQuickDoubleTapIsAccepted() {
        let first = pending(at: 100.0, hold: 0.12)
        let decision = DoubleTapPolicy.decide(
            pending: first, slotId: 1, pressedAt: first.releasedAt + 0.18, gapWindow: window)
        XCTAssertEqual(decision, .accepted)
        XCTAssertTrue(decision.isDoubleTap)
    }

    /// 間隔が空きすぎ → 不成立
    func testTooSlowSecondTapIsRejected() {
        let first = pending(at: 100.0, hold: 0.2)
        let decision = DoubleTapPolicy.decide(
            pending: first, slotId: 1, pressedAt: first.releasedAt + 2.0, gapWindow: window)
        XCTAssertEqual(decision, .tooSlow)
    }

    /// 別スロットのタップ → 不成立（スロット 1 の直後にスロット 2 を叩いても Enter しない）
    func testOtherSlotIsRejected() {
        let first = pending(slotId: 1, at: 100.0, hold: 0.12)
        let decision = DoubleTapPolicy.decide(
            pending: first, slotId: 2, pressedAt: first.releasedAt + 0.18, gapWindow: window)
        XCTAssertEqual(decision, .otherSlot)
    }

    /// 保留が無い（アプリ起動後の最初の押下など）→ 不成立＝通常の録音開始
    func testNoPendingIsRejected() {
        let decision = DoubleTapPolicy.decide(
            pending: nil, slotId: 1, pressedAt: 100.0, gapWindow: window)
        XCTAssertEqual(decision, .noFirstTap)
    }

    // MARK: - 境界値

    /// 離鍵→2 打目がちょうど窓ぴったり → 成立
    func testGapExactlyAtWindowIsAccepted() {
        let first = pending(at: 100.0, hold: 0.2)
        let decision = DoubleTapPolicy.decide(
            pending: first, slotId: 1, pressedAt: first.releasedAt + window, gapWindow: window)
        XCTAssertEqual(decision, .accepted)
    }

    /// わずかに超過 → 不成立
    func testGapSlightlyOverWindowIsRejected() {
        let first = pending(at: 100.0, hold: 0.2)
        let decision = DoubleTapPolicy.decide(
            pending: first, slotId: 1, pressedAt: first.releasedAt + window + 0.01,
            gapWindow: window)
        XCTAssertEqual(decision, .tooSlow)
    }

    /// ホールドが長くても、保留に入ってさえいれば 2 打目の判定はホールドを見ない
    /// （ホールドのガードは isTapCandidate の 1 か所だけ。二重にしたのが旧実装の失敗だった）
    func testDecideDoesNotReCheckHold() {
        let first = pending(at: 100.0, hold: 5.0)
        let decision = DoubleTapPolicy.decide(
            pending: first, slotId: 1, pressedAt: first.releasedAt + 0.1, gapWindow: window)
        XCTAssertEqual(decision, .accepted, "判定条件を 2 か所に散らさない")
    }

    // MARK: - 間隔窓のクランプ

    /// システム設定が既定より短くても下限まで（狭めない＝取りこぼしに戻さない）
    func testGapWindowClampsToMinimum() {
        XCTAssertEqual(DoubleTapPolicy.gapWindow(doubleClickInterval: 0.2), kDoubleTapGapMin)
    }

    /// システム設定を伸ばしていれば追従する（ただし上限まで）。
    /// この窓はそのまま「2 打目が来ない単発タップの確定がどれだけ遅れるか」の上限でもある
    func testGapWindowFollowsSystemSettingUpToMaximum() {
        XCTAssertEqual(DoubleTapPolicy.gapWindow(doubleClickInterval: 0.9), 0.9)
        XCTAssertEqual(DoubleTapPolicy.gapWindow(doubleClickInterval: 5.0), kDoubleTapGapMax)
    }

    // MARK: - 3 回連続タップ

    /// 3 回連続で叩いても暴走しない（2 打目で成立 → 保留を使い切る → 3 打目は通常録音）
    func testTripleTapDoesNotChain() {
        let first = pending(at: 100.0, hold: 0.12)
        XCTAssertEqual(
            DoubleTapPolicy.decide(
                pending: first, slotId: 1, pressedAt: first.releasedAt + 0.18, gapWindow: window),
            .accepted)

        // 成立時に AppController が保留を nil にするので、3 打目は 1 打目扱いに戻る
        XCTAssertEqual(
            DoubleTapPolicy.decide(pending: nil, slotId: 1, pressedAt: 100.6, gapWindow: window),
            .noFirstTap)
    }

    /// 3 打目が「新しい 1 打目」として保留されたあとの 4 打目は、また普通に成立してよい
    /// （＝連続入力を殺さない。暴走しないこと＝連鎖しないこと であって、封じることではない）
    func testTapAfterConsumedPairCanFormNewPair() {
        let third = pending(at: 100.6, hold: 0.1)
        XCTAssertEqual(
            DoubleTapPolicy.decide(
                pending: third, slotId: 1, pressedAt: third.releasedAt + 0.2, gapWindow: window),
            .accepted)
    }

    // MARK: - 旧しきい値との対比（実際に取りこぼしていた入力が救われること）

    /// 旧実装（hold 0.4 / 離鍵→押下 0.4 の二段構え）では落ちていたが、新実装では成立するケース。
    /// 「しっかり押してから素早く 2 回叩いた」典型で、これが「たまに検出されない」の正体だった。
    func testRescuedByNewThresholds() {
        /// 旧実装の判定を再現（hold < 0.4 で 1 打目として記録し、離鍵→押下 < 0.4 で成立）
        func oldAccepts(_ first: DoubleTapPolicy.Pending, secondPressedAt: TimeInterval) -> Bool {
            guard first.hold < 0.4 else { return false }  // 記録されない＝以後どう叩いても不成立
            return secondPressedAt - first.releasedAt < 0.4
        }
        /// 新実装の判定（保留に入るか → 2 打目が窓に入るか）
        func newAccepts(_ first: DoubleTapPolicy.Pending, secondPressedAt: TimeInterval) -> Bool {
            guard DoubleTapPolicy.isTapCandidate(hold: first.hold) else { return false }
            return DoubleTapPolicy.decide(
                pending: first, slotId: first.slotId, pressedAt: secondPressedAt,
                gapWindow: window
            ).isDoubleTap
        }

        // ケース A: 1 打目のホールドが 0.52 秒（人の「タップ」は 400ms を普通に超える）
        let caseA = pending(at: 100.0, hold: 0.52)
        let caseAsecond = caseA.releasedAt + 0.15
        XCTAssertFalse(oldAccepts(caseA, secondPressedAt: caseAsecond), "旧実装では落ちる前提")
        XCTAssertTrue(newAccepts(caseA, secondPressedAt: caseAsecond), "取りこぼしが救われていない")

        // ケース B: ホールドは短いが 2 打目までが 0.45 秒（旧 0.4 秒窓をわずかに超える）
        let caseB = pending(at: 200.0, hold: 0.15)
        let caseBsecond = caseB.releasedAt + 0.45
        XCTAssertFalse(oldAccepts(caseB, secondPressedAt: caseBsecond), "旧実装では落ちる前提")
        XCTAssertTrue(newAccepts(caseB, secondPressedAt: caseBsecond), "取りこぼしが救われていない")
    }

    /// 逆に、旧実装のガードの意図（長い口述の直後は Enter しない）は新実装でも維持されていること
    func testDictationGuardIsPreserved() {
        XCTAssertFalse(
            DoubleTapPolicy.isTapCandidate(hold: 5.0),
            "5 秒喋った直後に押し直しただけで auto_enter になってはいけない")
    }
}
