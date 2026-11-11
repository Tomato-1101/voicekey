//
//  RecordingLifecycleTests.swift
//  録音の世代突き合わせと、録音中の設定変更の扱いの単体テスト
//
//  (1) 古い録音の開始完了が遅れて届いても、次の録音の「開始の見張り」を解かない。
//  (2) 設定を変えたときは Transcriber を書き換えずに作り直す（同じ構成なら使い回す）ための判定。
//      書き換え方式だと、録音中・処理中の録音にまで新しいモデル・言語・プロンプトが効いていた。
//  (3) 打ち切った世代は、整形・翻訳の後でも結果を受け取らない（台帳の振る舞い）。
//

import XCTest

@testable import voicekey

final class RecordingLifecycleTests: XCTestCase {

    // 見張っている世代の完了なら解く
    func testStartCompletionDisarmsMatchingGeneration() {
        XCTAssertTrue(StallPolicy.startCompletionDisarms(pendingGeneration: 5, completedGeneration: 5))
    }

    // 古い世代の完了が遅れて届いても、次の世代の見張りは解かない
    func testLateCompletionDoesNotDisarmNextRecording() {
        XCTAssertFalse(StallPolicy.startCompletionDisarms(pendingGeneration: 6, completedGeneration: 5))
    }

    // 見張りが既に無い（打ち切り済み）ところへ届いた完了でも何も解かない
    func testCompletionWithoutPendingDoesNothing() {
        XCTAssertFalse(StallPolicy.startCompletionDisarms(pendingGeneration: nil, completedGeneration: 5))
    }

    // 同じ構成なら使い回す（接続を維持する）
    func testSameConfigIsReused() {
        let t = Transcriber(backend: .groq, model: "whisper-large-v3-turbo", language: "ja", prompt: "")
        XCTAssertTrue(t.hasSameConfig(backend: .groq, model: "whisper-large-v3-turbo", language: "ja", prompt: ""))
    }

    // モデル・言語・プロンプト・バックエンドのどれかが変われば作り直す（進行中の録音のインスタンスを書き換えない）
    func testAnyChangeRequiresNewInstance() {
        let t = Transcriber(backend: .groq, model: "m1", language: "ja", prompt: "p")
        XCTAssertFalse(t.hasSameConfig(backend: .groq, model: "m2", language: "ja", prompt: "p"))
        XCTAssertFalse(t.hasSameConfig(backend: .groq, model: "m1", language: "en", prompt: "p"))
        XCTAssertFalse(t.hasSameConfig(backend: .groq, model: "m1", language: "ja", prompt: "q"))
        XCTAssertFalse(t.hasSameConfig(backend: .openai, model: "m1", language: "ja", prompt: "p"))
    }

    // 打ち切った世代は、何度問い合わせても（整形の後・翻訳の後・Enter の前）捨てる対象のまま
    func testAbandonedGenerationStaysRejectedAcrossChecks() {
        var sessions = AbandonedSessions()
        sessions.abandon(3)
        XCTAssertTrue(sessions.contains(3))  // 確定直後
        XCTAssertTrue(sessions.contains(3))  // 整形・翻訳の後
        XCTAssertTrue(sessions.contains(3))  // Enter の前
        XCTAssertFalse(sessions.contains(4), "次の録音まで巻き込んではいけない")
    }

    // Soniox が途中で切れて前半だけ取れたときは録音全体を再送して後半も救う
    func testSonioxDisconnectWithPartialTextIsReplayed() {
        XCTAssertTrue(Transcriber.shouldReplayAfterLiveFailure(
            backend: .soniox, ending: .failed(.disconnect), streamedIsEmpty: false))
        XCTAssertTrue(Transcriber.shouldReplayAfterLiveFailure(
            backend: .soniox, ending: .failed(.error(code: "500", type: nil)), streamedIsEmpty: false))
    }

    // 正常終了・タイムアウト・空（従来の REST 救済が担当）・他プロバイダーは再送しない
    func testReplayIsLimitedToSonioxFailures() {
        XCTAssertFalse(Transcriber.shouldReplayAfterLiveFailure(
            backend: .soniox, ending: .completed, streamedIsEmpty: false))
        XCTAssertFalse(Transcriber.shouldReplayAfterLiveFailure(
            backend: .soniox, ending: .failed(.timeout), streamedIsEmpty: false))
        XCTAssertFalse(Transcriber.shouldReplayAfterLiveFailure(
            backend: .soniox, ending: .failed(.disconnect), streamedIsEmpty: true))
        XCTAssertFalse(Transcriber.shouldReplayAfterLiveFailure(
            backend: .openaiLive, ending: .failed(.disconnect), streamedIsEmpty: false))
    }

    // 再送が正常に終わったら再送分（全文）を使い、途中までの通知は出さない
    func testCompletedReplayReplacesLiveText() {
        let resolved = Transcriber.resolveReplay(live: "前半だけ", replayed: "全文", replayCompleted: true)
        XCTAssertEqual(resolved.text, "全文")
        XCTAssertTrue(resolved.isComplete)
    }

    // 再送もタイムアウト・失敗で終わったら、ライブ分より短い文で置き換えない（長い方を残して通知する）
    func testIncompleteReplayKeepsLongerText() {
        let shorter = Transcriber.resolveReplay(live: "ライブで取れた長い文", replayed: "短い", replayCompleted: false)
        XCTAssertEqual(shorter.text, "ライブで取れた長い文")
        XCTAssertFalse(shorter.isComplete)
        let longer = Transcriber.resolveReplay(live: "短い", replayed: "再送で取れた長い文", replayCompleted: false)
        XCTAssertEqual(longer.text, "再送で取れた長い文")
        XCTAssertFalse(longer.isComplete)
    }

    // 再送が投げた・空だったときはライブ分をそのまま使い、途中までと知らせる
    func testFailedOrEmptyReplayKeepsLiveText() {
        for replayed in [nil, ""] as [String?] {
            for completed in [true, false] {
                let resolved = Transcriber.resolveReplay(live: "ライブ分", replayed: replayed, replayCompleted: completed)
                XCTAssertEqual(resolved.text, "ライブ分")
                XCTAssertFalse(resolved.isComplete)
            }
        }
    }

    // 短いタップで離鍵後に届いた開始失敗は通知する。見張りが打ち切った世代は通知を重ねない
    func testLateStartFailureNotifiesUnlessTimedOut() {
        XCTAssertTrue(StallPolicy.lateStartFailureNotifies(timedOutGeneration: nil, failedGeneration: 5))
        XCTAssertTrue(StallPolicy.lateStartFailureNotifies(timedOutGeneration: 4, failedGeneration: 5))
        XCTAssertFalse(StallPolicy.lateStartFailureNotifies(timedOutGeneration: 5, failedGeneration: 5))
    }
}
