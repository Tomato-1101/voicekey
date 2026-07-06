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

    /// 各ステップ遷移で呼ばれた副作用（エンジン起動・整形オーバーライド・マイクモニタ・ホットキーテスト）を
    /// 記録するスパイ。すべて @MainActor 上でのみ触る。
    final class Spy {
        var engineStarts = 0
        var overrides: [Bool] = []
        var micStarts = 0
        var micStops = 0
        var hotkeyTests: [Bool] = []
    }

    /// 副作用を記録するテスト用モデルを作る。ConfigStore は実 UserDefaults を汚さないよう隔離 suite を注入。
    private func makeModel(startStep: OnboardingStep = .login) -> (OnboardingModel, Spy) {
        let suite = "voicekey.test.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suite)!
        addTeardownBlock { defaults.removePersistentDomain(forName: suite) }
        let spy = Spy()
        let model = OnboardingModel(
            startStep: startStep,
            config: ConfigStore(defaults: defaults),
            onFinish: {},
            onRestart: {},
            startEngineForPractice: { spy.engineStarts += 1 },
            setFormatOverride: { spy.overrides.append($0) },
            startMicMonitor: { _ in spy.micStarts += 1 },
            stopMicMonitor: { spy.micStops += 1 },
            setMicDevice: { _, _ in },
            setHotkeyTest: { on, _ in spy.hotkeyTests.append(on) },
            teardown: {}
        )
        return (model, spy)
    }

    // 動作確認→体験1 でエンジンが起動する（初回のみ）。以降の体験ステップでは再起動しない。
    func testEngineStartsOnceOnFirstPracticeStep() {
        let (model, spy) = makeModel(startStep: .hotkeyTest)
        XCTAssertEqual(spy.engineStarts, 0)  // 練習前は未起動

        model.goNext()  // → practiceBasic
        XCTAssertEqual(model.step, .practiceBasic)
        XCTAssertEqual(spy.engineStarts, 1)

        model.goNext()  // → practiceHandsfree
        model.goNext()  // → practiceFormat
        XCTAssertEqual(spy.engineStarts, 1)  // 2 回目以降は起動しない（冪等）
    }

    // 整形体験ステップに入っている間だけ整形オーバーライドが ON になる。
    func testFormatOverrideOnlyDuringFormatStep() {
        let (model, spy) = makeModel(startStep: .practiceBasic)

        model.goNext()  // → practiceHandsfree（OFF）
        model.goNext()  // → practiceFormat（ON）
        XCTAssertEqual(model.step, .practiceFormat)
        XCTAssertEqual(spy.overrides.last, true)

        model.goNext()  // → summary（OFF へ戻す）
        XCTAssertEqual(model.step, .summary)
        XCTAssertEqual(spy.overrides.last, false)
    }

    // スキップでまとめへ飛ぶと、整形オーバーライドは必ず OFF に戻る。
    func testSkipFromFormatStepClearsOverride() {
        let (model, spy) = makeModel(startStep: .practiceFormat)
        XCTAssertEqual(spy.overrides.last, true)  // 開始時点（practiceFormat）で ON

        model.skipToSummary()
        XCTAssertEqual(model.step, .summary)
        XCTAssertEqual(spy.overrides.last, false)
    }

    // マイクモニタはマイクテストステップに入ったときだけ開始し、離れると停止する。
    func testMicMonitorStartsOnlyOnMicTest() {
        let (model, spy) = makeModel(startStep: .checkIntro)
        XCTAssertEqual(spy.micStarts, 0)

        model.goNext()  // → micTest
        XCTAssertEqual(model.step, .micTest)
        XCTAssertEqual(spy.micStarts, 1)

        model.goNext()  // → hotkeyTest（モニタ停止）
        XCTAssertEqual(spy.micStarts, 1)          // 他ステップでは開始しない
        XCTAssertGreaterThan(spy.micStops, 0)      // 離れたら停止される
    }

    // ホットキーテストモードはホットキーテストステップに入っている間だけ ON になる。
    func testHotkeyTestActiveOnlyOnHotkeyStep() {
        let (model, spy) = makeModel(startStep: .micTest)

        model.goNext()  // → hotkeyTest（ON）
        XCTAssertEqual(model.step, .hotkeyTest)
        XCTAssertEqual(spy.hotkeyTests.last, true)

        model.goNext()  // → practiceBasic（OFF へ戻す）
        XCTAssertEqual(spy.hotkeyTests.last, false)
    }
}
