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
}
