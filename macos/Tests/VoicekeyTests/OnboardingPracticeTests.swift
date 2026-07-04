//
//  OnboardingPracticeTests.swift
//  体験型セットアップガイドの「成功判定」と「ステップ遷移の副作用」の単体テスト
//
//  - OnboardingPractice.hasInput: 練習欄のテキスト変化から成功を判定する純ロジック
//  - OnboardingModel: 体験ステップに入ると録音エンジンを 1 度だけ起動し、
//    整形体験ステップの間だけ整形オーバーライドを ON にする配線
//  を固定する。UI（SwiftUI）には触れず、モデルの副作用だけを検証する。
//  Windows 版 tests/test_onboarding_window.py の体験ステップ遷移テストと同じ観点。
//

import XCTest
@testable import voicekey

@MainActor
final class OnboardingPracticeTests: XCTestCase {

    // MARK: - 成功判定（純ロジック）

    // 空・空白のみは未入力。1 文字でも実体があれば入力あり（＝成功）。
    func testHasInputDetectsNonEmpty() {
        XCTAssertFalse(OnboardingPractice.hasInput(""))
        XCTAssertFalse(OnboardingPractice.hasInput("   "))
        XCTAssertFalse(OnboardingPractice.hasInput("\n \t"))
        XCTAssertTrue(OnboardingPractice.hasInput("あ"))
        XCTAssertTrue(OnboardingPractice.hasInput("  明日の打ち合わせ  "))
    }

    // MARK: - ステップ遷移の副作用

    /// 副作用（エンジン起動・整形オーバーライド）を記録するテスト用モデルを作る。
    /// ConfigStore は実 UserDefaults を汚さないよう隔離 suite を注入する。
    private func makeModel(startStep: OnboardingStep = .login)
        -> (OnboardingModel, () -> Int, () -> [Bool]) {
        let suite = "voicekey.test.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suite)!
        addTeardownBlock { defaults.removePersistentDomain(forName: suite) }
        var engineStartCount = 0
        var overrideHistory: [Bool] = []
        let model = OnboardingModel(
            startStep: startStep,
            config: ConfigStore(defaults: defaults),
            onFinish: {},
            onRestart: {},
            startEngine: { engineStartCount += 1 },
            setFormatOverride: { overrideHistory.append($0) }
        )
        return (model, { engineStartCount }, { overrideHistory })
    }

    // ログイン→体験1 でエンジンが起動する（初回のみ）。以降の体験ステップでは再起動しない。
    func testEngineStartsOnceOnFirstPracticeStep() {
        let (model, engineStartCount, _) = makeModel(startStep: .login)
        XCTAssertEqual(engineStartCount(), 0)  // ログイン段階では未起動

        model.goNext()  // → practiceBasic
        XCTAssertEqual(model.step, .practiceBasic)
        XCTAssertEqual(engineStartCount(), 1)

        model.goNext()  // → practiceHandsfree
        model.goNext()  // → practiceFormat
        XCTAssertEqual(engineStartCount(), 1)  // 2 回目以降は起動しない（冪等）
    }

    // 整形体験ステップに入っている間だけ整形オーバーライドが ON になる。
    func testFormatOverrideOnlyDuringFormatStep() {
        let (model, _, overrideHistory) = makeModel(startStep: .login)

        model.goNext()  // → practiceBasic（OFF）
        model.goNext()  // → practiceHandsfree（OFF）
        model.goNext()  // → practiceFormat（ON）
        XCTAssertEqual(model.step, .practiceFormat)
        XCTAssertEqual(overrideHistory().last, true)

        model.goNext()  // → featureTour（OFF へ戻す）
        XCTAssertEqual(overrideHistory().last, false)
    }

    // スキップで完了へ飛ぶと、整形オーバーライドは必ず OFF に戻る。
    func testSkipFromFormatStepClearsOverride() {
        let (model, _, overrideHistory) = makeModel(startStep: .practiceFormat)
        // 開始時点（practiceFormat）で ON になっている
        XCTAssertEqual(overrideHistory().last, true)

        model.skipToDone()
        XCTAssertEqual(model.step, .done)
        XCTAssertEqual(overrideHistory().last, false)
    }

    // 練習欄への入力で該当ステップの成功フラグが立つ。
    func testMarkPracticeSucceededSetsStepFlag() {
        let (model, _, _) = makeModel(startStep: .login)

        model.goNext()  // → practiceBasic
        model.markPracticeSucceeded()
        XCTAssertTrue(model.basicPracticeDone)
        XCTAssertFalse(model.handsfreePracticeDone)

        model.goNext()  // → practiceHandsfree
        model.markPracticeSucceeded()
        XCTAssertTrue(model.handsfreePracticeDone)

        model.goNext()  // → practiceFormat
        model.markPracticeSucceeded()
        XCTAssertTrue(model.formatPracticeDone)
    }
}
