//
//  OnboardingDeciderTests.swift
//  初回起動オンボーディングの起動時分岐判定（Phase 5）の単体テスト
//
//  OnboardingDecider.launchAction は副作用のない純関数（UserDefaults に触れない）。
//  - 完全初回（フラグなし）→ 最初から表示
//  - 既存ユーザー（didSetupLaunchAtLogin=true）→ 補完のみで非表示
//  - 完了済み → スキップ
//  - 中断ステップあり → 何より優先してその位置から再開
//  という判定を固定する。Windows 版 tests/test_config_manager.py の初回起動判定と同じ観点。
//

import XCTest
@testable import voicekey

final class OnboardingDeciderTests: XCTestCase {

    // 完全な初回起動（新規インストール）: 最初から表示する
    func testFreshInstallShowsFromStart() {
        let action = OnboardingDecider.launchAction(
            didCompleteOnboarding: false,
            didSetupLaunchAtLogin: false,
            savedStep: nil
        )
        XCTAssertEqual(action, .show(fromStep: 0))
    }

    // 既存ユーザー（オンボーディング機能導入前から使用）: 補完のみで表示しない
    func testExistingUserAutoCompletes() {
        let action = OnboardingDecider.launchAction(
            didCompleteOnboarding: false,
            didSetupLaunchAtLogin: true,
            savedStep: nil
        )
        XCTAssertEqual(action, .autoComplete)
    }

    // 完了済み: 二度と出さない
    func testCompletedSkips() {
        let action = OnboardingDecider.launchAction(
            didCompleteOnboarding: true,
            didSetupLaunchAtLogin: true,
            savedStep: nil
        )
        XCTAssertEqual(action, .skip)
    }

    // 中断再開は最優先: 完了・既存フラグに関係なく、保存ステップから続きを出す
    func testSavedStepResumesTakesPrecedence() {
        let action = OnboardingDecider.launchAction(
            didCompleteOnboarding: false,
            didSetupLaunchAtLogin: true,   // 既存ユーザーでも
            savedStep: OnboardingStep.inputMonitoring.rawValue
        )
        XCTAssertEqual(action, .show(fromStep: OnboardingStep.inputMonitoring.rawValue))
    }

    // 中断ステップは完了済みよりも優先される（入力監視の再起動再開を確実にする）
    func testSavedStepBeatsCompleted() {
        let action = OnboardingDecider.launchAction(
            didCompleteOnboarding: true,
            didSetupLaunchAtLogin: true,
            savedStep: OnboardingStep.inputMonitoring.rawValue
        )
        XCTAssertEqual(action, .show(fromStep: OnboardingStep.inputMonitoring.rawValue))
    }

    // 負値の savedStep（不正値）は中断なしと同じ扱いにフォールバックする
    func testNegativeSavedStepIgnored() {
        let action = OnboardingDecider.launchAction(
            didCompleteOnboarding: false,
            didSetupLaunchAtLogin: false,
            savedStep: -1
        )
        XCTAssertEqual(action, .show(fromStep: 0))
    }

    // ステップ enum の順序（戻る/進むの前提）を固定する。
    // 権限・ログインの後ろに体験（練習）3 ステップ＋機能ツアーを足し、最後が完了。
    func testStepOrdering() {
        XCTAssertTrue(OnboardingStep.welcome < OnboardingStep.microphone)
        XCTAssertTrue(OnboardingStep.login < OnboardingStep.practiceBasic)
        XCTAssertTrue(OnboardingStep.practiceBasic < OnboardingStep.practiceHandsfree)
        XCTAssertTrue(OnboardingStep.practiceHandsfree < OnboardingStep.practiceFormat)
        XCTAssertTrue(OnboardingStep.practiceFormat < OnboardingStep.featureTour)
        XCTAssertTrue(OnboardingStep.featureTour < OnboardingStep.done)
        XCTAssertEqual(OnboardingStep.allCases.count, 10)
    }

    // 体験（練習）ステップの判定（エンジン起動・スキップ導線の前提）
    func testIsPracticeFlag() {
        XCTAssertTrue(OnboardingStep.practiceBasic.isPractice)
        XCTAssertTrue(OnboardingStep.practiceHandsfree.isPractice)
        XCTAssertTrue(OnboardingStep.practiceFormat.isPractice)
        XCTAssertFalse(OnboardingStep.featureTour.isPractice)
        XCTAssertFalse(OnboardingStep.login.isPractice)
        XCTAssertFalse(OnboardingStep.done.isPractice)
    }
}
