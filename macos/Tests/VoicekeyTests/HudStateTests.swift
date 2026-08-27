//
//  HudStateTests.swift
//  録音 HUD（ピル）の状態遷移の回帰テスト
//
//  ユーザー報告 2 件（2026-08-27）の再発防止:
//   1) 「録音中に通知が出ると、そのあと録音表示が戻らず消える」
//      → 通知の消灯は「変換中なら戻す・それ以外は隠す」だったため、録音中に出た通知
//        （モデル準備の進捗・無音通知など）で録音ピルが消えていた。
//   2) 「入力し終わってすぐ次を始めると、前回の入力の文字がピルに残る」
//      → ライブ字幕を「次の録音の開始時」に消していたため、直前の録音の遅れて届いた
//        確定（離鍵の 100ms 前後あと）が新しい録音のピルに書き込まれていた。
//        消すタイミングを「録音が終わった時」へ移してある。
//
//  パネル（NSPanel）は作らせずに（enabled = false）状態モデルだけを検証する。
//

import XCTest
@testable import voicekey

@MainActor
final class HudStateTests: XCTestCase {

    /// パネルを作らない HUD（状態モデルだけを見る）
    private func makeHud(alwaysVisible: Bool = true) -> HudController {
        let hud = HudController()
        hud.enabled = false
        hud.alwaysVisible = alwaysVisible
        return hud
    }

    // MARK: - 通知からの復帰

    /// 録音中に出た通知は、消えたあと録音ピルへ戻る（HUD ごと消えない）
    func testNoticeDuringRecordingRestoresRecordingPill() {
        let hud = makeHud()
        hud.update(for: .recording(autoEnter: false, handsFree: false))

        hud.notice("モデル準備中 0%")
        XCTAssertEqual(hud.model.mode, .notice("モデル準備中 0%"))

        hud.restoreAfterNotice()
        XCTAssertEqual(hud.model.mode, .recording(autoEnter: false, handsFree: false))
    }

    /// auto_enter（ダブルタップ確定）中の通知も、その状態のまま戻る
    func testNoticeRestoresAutoEnterRecording() {
        let hud = makeHud()
        hud.update(for: .recording(autoEnter: true, handsFree: false))
        hud.notice("何かの通知")

        hud.restoreAfterNotice()
        XCTAssertEqual(hud.model.mode, .recording(autoEnter: true, handsFree: false))
    }

    /// 変換中の通知は従来どおり「変換中…」へ戻る
    func testNoticeDuringTranscribingRestoresTranscribing() {
        let hud = makeHud()
        hud.update(for: .transcribing)
        hud.notice("何かの通知")

        hud.restoreAfterNotice()
        XCTAssertEqual(hud.model.mode, .transcribing)
    }

    /// 待機中の通知は、常時表示 ON なら待機ピルへ戻る
    func testNoticeDuringIdleRestoresIdlePill() {
        let hud = makeHud(alwaysVisible: true)
        hud.update(for: .idle)
        hud.notice("何かの通知")

        hud.restoreAfterNotice()
        XCTAssertEqual(hud.model.mode, .idlePill)
    }

    /// 常時表示 OFF なら待機では隠れる
    func testNoticeDuringIdleHidesWhenNotAlwaysVisible() {
        let hud = makeHud(alwaysVisible: false)
        hud.update(for: .idle)
        hud.notice("何かの通知")

        hud.restoreAfterNotice()
        XCTAssertEqual(hud.model.mode, .hidden)
    }

    /// 通知が出ている間に録音が始まったら、通知を待たずに録音表示へ切り替わる
    func testRecordingReplacesNoticeImmediately() {
        let hud = makeHud()
        hud.update(for: .idle)
        hud.notice("何かの通知")

        hud.update(for: .recording(autoEnter: false, handsFree: false))
        XCTAssertEqual(hud.model.mode, .recording(autoEnter: false, handsFree: false))
    }

    // MARK: - ライブ字幕の持ち越し

    /// 録音が終わった時点でライブ字幕を捨てる（次の録音へ持ち越さない）
    func testLiveTextIsClearedWhenRecordingEnds() {
        let hud = makeHud()
        hud.update(for: .recording(autoEnter: false, handsFree: false))
        hud.setLiveText("前回の入力です")
        XCTAssertEqual(hud.model.liveText, "前回の入力です")

        hud.update(for: .transcribing)
        XCTAssertEqual(hud.model.liveText, "")
    }

    /// 録音を挟まず待機へ落ちた場合もライブ字幕を捨てる
    func testLiveTextIsClearedWhenGoingIdle() {
        let hud = makeHud()
        hud.update(for: .recording(autoEnter: false, handsFree: false))
        hud.setLiveText("前回の入力です")

        hud.update(for: .idle)
        XCTAssertEqual(hud.model.liveText, "")
    }

    /// 前の録音の遅れて届いた更新は、録音していない間なら書き込まれない
    func testLiveTextIgnoredWhileNotRecording() {
        let hud = makeHud()
        hud.update(for: .transcribing)

        hud.setLiveText("遅れて届いた前回の確定")
        XCTAssertEqual(hud.model.liveText, "")
    }

    /// 新しい録音のピルは、直前の録音の文字を持ったまま始まらない
    /// （＝波形バーが出る条件 liveText.isEmpty を満たす）
    func testNextRecordingStartsWithEmptyLiveText() {
        let hud = makeHud()
        hud.update(for: .recording(autoEnter: false, handsFree: false))
        hud.setLiveText("前回の入力です")
        hud.update(for: .transcribing)

        hud.update(for: .recording(autoEnter: false, handsFree: false))
        XCTAssertEqual(hud.model.liveText, "")
    }

    /// auto_enter 昇格（ダブルタップ確定）では、喋っている途中のライブ字幕を消さない
    func testAutoEnterPromotionKeepsLiveText() {
        let hud = makeHud()
        hud.update(for: .recording(autoEnter: false, handsFree: false))
        hud.setLiveText("いま喋っている途中")

        hud.update(for: .recording(autoEnter: true, handsFree: false))
        XCTAssertEqual(hud.model.liveText, "いま喋っている途中")
    }
}
