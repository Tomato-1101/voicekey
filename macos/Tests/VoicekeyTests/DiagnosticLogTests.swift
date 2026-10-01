//
//  DiagnosticLogTests.swift
//  詳細ログの整形（[計測] / [ライブ] サマリ・設定差分・ホットキー時刻換算）の単体テスト
//
//  すべて純関数の検証。ログファイル・ネットワーク・Keychain には一切触れない。
//

import XCTest
@testable import voicekey

final class DiagnosticLogTests: XCTestCase {

    // MARK: - DiagnosticText

    func testClipReplacesNewlinesAndTruncates() {
        XCTAssertEqual(DiagnosticText.clip("a\nb"), "a b")
        XCTAssertEqual(DiagnosticText.clip("abcdef", 3), "abc…")
        XCTAssertEqual(DiagnosticText.clip("abc", 3), "abc")
    }

    func testMsAndGenFormatting() {
        XCTAssertEqual(DiagnosticText.ms(from: 1.0, to: 1.25), "250ms")
        XCTAssertEqual(DiagnosticText.ms(from: nil, to: 1.0), "-")
        XCTAssertEqual(DiagnosticText.ms(from: 1.0, to: nil), "-")
        XCTAssertEqual(DiagnosticText.ms(42), "42ms")
        XCTAssertEqual(DiagnosticText.ms(nil), "-")
        XCTAssertEqual(DiagnosticText.gen(0), "-")
        XCTAssertEqual(DiagnosticText.gen(7), "7")
    }

    // MARK: - [計測] サマリ

    func testSummaryLineWithAllFields() {
        var t = DictationTimeline(generation: 12, slot: 1, backend: "soniox", model: "stt-rt", pressedAt: 100.0)
        t.route = "streaming"
        t.ending = "completed"
        t.outcome = DictationTimeline.Outcome.pasted
        t.recordStartedAt = 100.030
        t.firstAudioAt = 100.080
        t.recordingSec = 2.5
        t.releasedAt = 102.6
        t.stoppedAt = 102.65
        t.finalizedAt = 102.75
        t.sttMs = 150
        t.formatMs = 300
        t.translateMs = 90
        t.pasteMs = 60
        t.pastedAt = 103.2
        t.characters = 18
        t.targetBundleID = "com.apple.TextEdit"
        t.autoEnter = true

        XCTAssertEqual(
            t.summaryLine(),
            "[計測] gen=12 slot=1 backend=soniox model=stt-rt 経路=streaming 終わり方=completed 結果=貼付 "
                + "押下→録音開始=30ms 押下→最初の音声=80ms 録音=2.50s 離鍵→確定=150ms 離鍵→停止=50ms 停止→確定=100ms STT=150ms VAD=- "
                + "整形=300ms 翻訳=90ms 貼付=60ms 離鍵→貼付完了=600ms 押下→貼付完了=3200ms 文字数=18 "
                + "貼付先=com.apple.TextEdit auto_enter=true"
        )
    }

    func testSummaryLineMissingValuesAreDash() {
        var t = DictationTimeline(generation: 3, slot: 2, backend: "groq", model: "", pressedAt: 10)
        t.outcome = DictationTimeline.Outcome.tooShort
        let line = t.summaryLine()
        XCTAssertTrue(line.hasPrefix("[計測] gen=3 slot=2 backend=groq model=- 経路=- 終わり方=- 結果=短すぎ "))
        for key in ["押下→録音開始", "押下→最初の音声", "録音", "離鍵→確定", "離鍵→停止", "停止→確定", "STT", "VAD", "整形", "翻訳",
                    "貼付", "離鍵→貼付完了", "押下→貼付完了", "文字数", "貼付先"] {
            XCTAssertTrue(line.contains(" \(key)=- "), "\(key) が - になっていない: \(line)")
        }
        XCTAssertTrue(line.hasSuffix("auto_enter=false"))
    }

    func testUnfinishedIsDefaultOutcome() {
        let t = DictationTimeline(generation: 1, slot: 1, backend: "groq", model: "m", pressedAt: 0)
        XCTAssertTrue(t.summaryLine().contains("結果=未完了"))
    }

    func testRouteClassification() {
        XCTAssertEqual(DictationTimeline.route(backend: .soniox, hadStreamer: true, streamedText: true), "streaming")
        XCTAssertEqual(DictationTimeline.route(backend: .appleLocal, hadStreamer: true, streamedText: true), "local")
        XCTAssertEqual(DictationTimeline.route(backend: .soniox, hadStreamer: true, streamedText: false), "replay")
        XCTAssertEqual(DictationTimeline.route(backend: .deepgram, hadStreamer: true, streamedText: false), "fallback")
        XCTAssertEqual(DictationTimeline.route(backend: .groq, hadStreamer: false, streamedText: false), "rest")
        XCTAssertEqual(DictationTimeline.route(backend: .soniox, hadStreamer: false, streamedText: false), "replay")
        XCTAssertEqual(DictationTimeline.route(backend: .appleLocal, hadStreamer: false, streamedText: false), "local")
    }

    // MARK: - [ライブ] サマリ

    func testLiveSummaryLine() {
        let line = LiveSessionLog.summaryLine(
            generation: 5, provider: "deepgram", createdAt: 50,
            connectedAt: 50.2, firstAudioSentAt: 50.21, firstResultAt: 50.9,
            endRequestedAt: 52.0, resolvedAt: 52.12, ending: "completed", sendErrors: 0)
        XCTAssertEqual(
            line,
            "[ライブ] gen=5 provider=deepgram 接続確立=200ms 最初の音声送信=210ms 最初の結果=900ms "
                + "終了要求→確定=120ms 終わり方=completed 送信エラー=0"
        )
    }

    func testLiveSummaryLineWithoutEndRequest() {
        // 終了要求の前に壊れた（受信エラー等）ときは「終了要求→確定」を出さない
        let line = LiveSessionLog.summaryLine(
            generation: 0, provider: "openai-live", createdAt: 1,
            connectedAt: nil, firstAudioSentAt: nil, firstResultAt: nil,
            endRequestedAt: nil, resolvedAt: 3, ending: "failed:error", sendErrors: 4)
        XCTAssertEqual(
            line,
            "[ライブ] gen=- provider=openai-live 接続確立=- 最初の音声送信=- 最初の結果=- "
                + "終了要求→確定=- 終わり方=failed:error 送信エラー=4"
        )
    }

    func testEndingLabelForReason() {
        XCTAssertEqual(LiveSessionLog.endingLabel(forReason: "metadata"), "completed")
        XCTAssertEqual(LiveSessionLog.endingLabel(forReason: "completed"), "completed")
        XCTAssertEqual(LiveSessionLog.endingLabel(forReason: "timeout"), "failed:timeout")
        XCTAssertEqual(LiveSessionLog.endingLabel(forReason: "disconnect"), "failed:disconnect")
        XCTAssertEqual(LiveSessionLog.endingLabel(forReason: "error"), "failed:error")
        XCTAssertEqual(LiveSessionLog.endingLabel(forReason: "cancel"), "cancel")
    }

    // MARK: - 設定差分

    func testSettingsDiff() {
        let old = ["hudEnabled": "true", "language": "ja", "slot1.prompt": "長さ10"]
        let new = ["hudEnabled": "false", "language": "ja", "slot1.prompt": "長さ12", "repasteKey": "cmd+v"]
        XCTAssertEqual(
            SettingsChangeLog.diff(old: old, new: new),
            "hudEnabled=false repasteKey=cmd+v slot1.prompt=長さ12"
        )
        XCTAssertNil(SettingsChangeLog.diff(old: old, new: old))
        XCTAssertEqual(SettingsChangeLog.diff(old: ["a": "1"], new: [:]), "a=(削除)")
    }

    // MARK: - ホットキー時刻の換算

    func testHotkeyEventClockNanoseconds() {
        // 1 tick = 1ns の Mac（Intel）では ns としてそのまま読む
        let uptime = HotkeyEventClock.uptime(
            timestamp: 100_000_000_000, handledAt: 100.004, numer: 1, denom: 1)
        XCTAssertEqual(uptime ?? -1, 100.0, accuracy: 1e-9)
    }

    func testHotkeyEventClockMachTicks() {
        // Apple Silicon（24MHz: numer=125 denom=3）で生の tick が入ってきた場合
        let ticks: UInt64 = 2_400_000_000  // = 100 秒
        let uptime = HotkeyEventClock.uptime(timestamp: ticks, handledAt: 100.01, numer: 125, denom: 3)
        XCTAssertEqual(uptime ?? -1, 100.0, accuracy: 1e-6)
    }

    func testHotkeyEventClockImplausibleIsNil() {
        // どちらの解釈でも処理時刻と 10 秒以上ずれる値は採らない（誤った遅延を出さない）
        XCTAssertNil(HotkeyEventClock.uptime(timestamp: 5_000_000_000, handledAt: 1000, numer: 125, denom: 3))
        XCTAssertNil(HotkeyEventClock.uptime(timestamp: 0, handledAt: 1000, numer: 1, denom: 1))
    }
}
