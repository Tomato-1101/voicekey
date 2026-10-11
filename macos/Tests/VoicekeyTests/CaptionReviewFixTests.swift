//
//  CaptionReviewFixTests.swift
//  ライブ字幕のレビュー指摘（2026-10-12）の回帰テスト
//
//  - 高-1: Apple 先出しで Apple が失敗しても、確定文はクラウドで必ず訳す
//  - 高-2: Soniox は無音が続いたら切り、有音で張り直す（失敗に数えない）
//  - 中-1/2: Soniox の訳待ちの時間切れは確定分だけ・訳は文ごとに原文と組にする
//  - 中-3: 短い確定行を直前の行へまとめる判定
//  - 中-4: 停止時に送る終端
//  - 中-5: 締め切り後に届いたクラウドの訳も差し替える
//  - 中-6: Soniox を諦めた理由の文言
//  - 低: 確定の前倒しが詰まっても止まらない
//  すべてオフライン（ネットワーク・鍵・音声なし。Soniox / Groq / Gemini には接続しない）。
//

import XCTest
import os

@testable import voicekey

/// 先頭に印を付けて返すだけの翻訳器（遅延を指定できる）
@available(macOS 26.0, *)
private struct EchoTranslator: Translator {
    let tag: String
    var delay: TimeInterval = 0

    func translate(_ text: String, context: [TranslationExchange]) async throws -> String {
        if delay > 0 { try await Task.sleep(nanoseconds: UInt64(delay * 1_000_000_000)) }
        return tag + text
    }
}

/// 必ず失敗する翻訳器（呼ばれた回数を数える）
@available(macOS 26.0, *)
private final class FailingTranslator: Translator, @unchecked Sendable {
    let calls = OSAllocatedUnfairLock(initialState: 0)

    func translate(_ text: String, context: [TranslationExchange]) async throws -> String {
        calls.withLock { $0 += 1 }
        throw TranslationError.translationUnavailable("テスト用の失敗")
    }
}

/// 最初の 1 回だけ長く待つ翻訳器（クラウドが詰まった状態の再現）
@available(macOS 26.0, *)
private final class SlowFirstTranslator: Translator, @unchecked Sendable {
    private let calls = OSAllocatedUnfairLock(initialState: 0)
    let stall: TimeInterval

    init(stall: TimeInterval) { self.stall = stall }

    func translate(_ text: String, context: [TranslationExchange]) async throws -> String {
        let first = calls.withLock { count -> Bool in
            count += 1
            return count == 1
        }
        if first { try await Task.sleep(nanoseconds: UInt64(stall * 1_000_000_000)) }
        return "CL:" + text
    }
}

final class CaptionReviewFixTests: XCTestCase {

    // MARK: - 高-1 Apple 先出しの失敗

    /// Apple 先出しで Apple が落ちても、確定文は全部クラウドで訳して出す（原文のまま流さない）。
    /// 以後は cloud→Apple 構成に戻り、差し替え（クラウドの二重呼び出し）もしない。
    func testAppleFirstFailureFallsBackToCloudForEveryLine() throws {
        guard #available(macOS 26.0, *) else { throw XCTSkip("ライブ字幕は macOS 26 以降") }
        let apple = FailingTranslator()
        let appleReady = OSAllocatedUnfairLock(initialState: true)
        let counter = OSAllocatedUnfairLock(initialState: 0)
        let translated = OSAllocatedUnfairLock(initialState: [String]())
        let failed = OSAllocatedUnfairLock(initialState: [String]())
        let revised = OSAllocatedUnfairLock(initialState: [String]())
        let coordinator = TranslationCoordinator(
            translatorProvider: {
                CaptionService.confirmedTranslator(
                    apple: apple, cloud: EchoTranslator(tag: "CL:"),
                    appleFirst: appleReady.withLock { $0 }
                ) { appleReady.withLock { $0 = false } }
            },
            sequenceProvider: { counter.withLock { $0 += 1; return $0 } },
            // 本物と同じく、Apple 先出しのときだけ差し替え先を返す
            refinerProvider: { appleReady.withLock { $0 } ? EchoTranslator(tag: "RF:") : nil }
        )
        coordinator.onTranslated = { _, japanese, _ in translated.withLock { $0.append(japanese) } }
        coordinator.onFailed = { source, _ in failed.withLock { $0.append(source) } }
        coordinator.onRevised = { _, japanese, _ in revised.withLock { $0.append(japanese) } }

        let lines = ["Good morning everyone.", "Let us begin.", "First item."]
        for line in lines { coordinator.submit(line) }
        wait(seconds: 0.8)

        XCTAssertEqual(translated.withLock { $0 }, lines.map { "CL:" + $0 }, "確定文がクラウドで訳されていない")
        XCTAssertTrue(failed.withLock { $0 }.isEmpty, "原文のまま流した行がある")
        XCTAssertTrue(revised.withLock { $0 }.isEmpty, "Apple の代わりにクラウドで訳した行を、もう一度クラウドへ送った")
        XCTAssertFalse(appleReady.withLock { $0 }, "Apple の失敗後も Apple 先出しのまま")
        XCTAssertEqual(apple.calls.withLock { $0 }, 1, "Apple 失敗後も毎回 Apple を先に試している")
    }

    /// cloud→Apple 構成（従来）はクラウドが落ちたら Apple で訳す
    func testCloudFirstFallsBackToApple() async throws {
        guard #available(macOS 26.0, *) else { throw XCTSkip("ライブ字幕は macOS 26 以降") }
        let hookCalled = OSAllocatedUnfairLock(initialState: false)
        let translator = CaptionService.confirmedTranslator(
            apple: EchoTranslator(tag: "AP:"), cloud: FailingTranslator(), appleFirst: false
        ) { hookCalled.withLock { $0 = true } }
        let result = try await translator.translate("Hello.", context: [])
        XCTAssertEqual(result, "AP:Hello.")
        XCTAssertFalse(hookCalled.withLock { $0 }, "クラウドの失敗で Apple 先出しの解除フックを呼んだ")
    }

    // MARK: - 高-2 Soniox の無音切断

    /// 無音が 30 秒続いたら切り、有音が来たら張り直す。何度切っても失敗には数えない
    func testSonioxIdleDisconnectAndReconnect() {
        XCTAssertEqual(SonioxIdleTracker.idleDisconnectInterval, 30)
        XCTAssertEqual(SonioxIdleTracker.voiceRMS, PauseFinalizeTrigger.silenceRMS)

        var tracker = SonioxIdleTracker()
        var reconnect = SonioxReconnectPolicy()
        tracker.noteConnected(now: 100)
        XCTAssertFalse(tracker.shouldDisconnect(now: 129), "30 秒経つ前に切った")
        // 途中で声が入ったら数え直す
        XCTAssertFalse(tracker.noteFrame(isVoiced: true, now: 120))
        XCTAssertFalse(tracker.shouldDisconnect(now: 149))
        // 無音のフレーム（RMS がしきい値未満）は声として数えない
        XCTAssertFalse(tracker.noteFrame(isVoiced: false, now: 140))
        XCTAssertTrue(tracker.shouldDisconnect(now: 150), "無音が 30 秒続いても切らない（無音に課金され続ける）")
        XCTAssertTrue(tracker.isIdle)
        XCTAssertFalse(tracker.shouldDisconnect(now: 300), "切った後にもう一度切ろうとした")

        // 切っている間の無音では張り直さない
        XCTAssertFalse(tracker.noteFrame(isVoiced: false, now: 301))
        XCTAssertTrue(tracker.isIdle)
        // 有音で張り直す
        XCTAssertTrue(tracker.noteFrame(isVoiced: true, now: 302), "音が戻っても張り直さない")
        XCTAssertFalse(tracker.isIdle)
        tracker.noteConnected(now: 302.3)
        XCTAssertFalse(tracker.shouldDisconnect(now: 320))

        // 2 周目も同じように切れて戻る（アイドルは失敗ではないので、再接続の回数は 1 つも進まない）
        XCTAssertTrue(tracker.shouldDisconnect(now: 333))
        XCTAssertTrue(tracker.noteFrame(isVoiced: true, now: 400))
        XCTAssertEqual(reconnect.failures, 0)
        XCTAssertEqual(reconnect.nextDelay(isFatal: false), SonioxReconnectPolicy.baseDelay,
                       "アイドル切断の後でも、本当の失敗は 1 回目の待ち時間から始まる")
    }

    /// 有音判定の RMS
    func testSonioxIdleRMS() {
        XCTAssertEqual(SonioxIdleTracker.rms([]), 0)
        XCTAssertLessThan(SonioxIdleTracker.rms([Float](repeating: 0.001, count: 160)), SonioxIdleTracker.voiceRMS)
        XCTAssertGreaterThanOrEqual(SonioxIdleTracker.rms([Float](repeating: 0.1, count: 160)), SonioxIdleTracker.voiceRMS)
    }

    // MARK: - 中-1 / 中-2 Soniox の組み立て

    /// 訳待ちの時間切れは確定分だけを行にし、未確定分は後で確定トークンとして 1 回だけ出る
    func testFlushConfirmedKeepsPendingTokens() {
        var assembler = SonioxCaptionAssembler(translates: true)
        _ = assembler.apply(response([src("Hello", final: true), endToken(), src(" wor", final: false)]))
        XCTAssertTrue(assembler.isWaitingForTranslation)
        XCTAssertEqual(assembler.flushConfirmed(), .confirmed(source: "Hello", translation: ""))
        XCTAssertNil(assembler.flushConfirmed(), "確定分を二度出した")

        // 未確定だった " wor" は確定トークン " world." として届く。二重には出ない
        _ = assembler.apply(response([src(" world.", final: true)]))
        XCTAssertEqual(assembler.flush(), .confirmed(source: "world.", translation: ""))
        XCTAssertNil(assembler.flush())
    }

    /// 原文 2 文が確定した後に訳がまとめて届いたら、文ごとに原文と組にして 2 行出す
    func testTranslatedLinesPairSentenceBySentence() {
        var assembler = SonioxCaptionAssembler(translates: true)
        _ = assembler.apply(response([src("Hello there. How are you? I", final: true)]))
        let events = assembler.apply(response([tr("やあ。元気？", final: true), src(" think", final: false)]))
        XCTAssertEqual(events, [
            .confirmed(source: "Hello there.", translation: "やあ。"),
            .confirmed(source: "How are you?", translation: "元気？"),
            .live(source: "I think", translation: ""),
        ])
        // 持ち越した文は次の訳と組になる（1 文ずつずれない）。未確定だった " think" は確定トークンとして届き直す
        let next = assembler.apply(response([src(" think so.", final: true), tr("そう思う。", final: true)]))
        XCTAssertEqual(next.first, .confirmed(source: "I think so.", translation: "そう思う。"))
    }

    /// 訳が 1 文ずつ遅れて届いても、2 文目の訳が次の原文と組にならない
    func testTranslatedLinesDoNotDrift() {
        var assembler = SonioxCaptionAssembler(translates: true)
        _ = assembler.apply(response([src("One. Two.", final: true)]))
        var events = assembler.apply(response([tr("一。", final: true)]))
        XCTAssertEqual(events.first, .confirmed(source: "One.", translation: "一。"))
        events = assembler.apply(response([src(" Three.", final: true), tr("二。", final: true)]))
        XCTAssertEqual(events.first, .confirmed(source: "Two.", translation: "二。"))
        events = assembler.apply(response([tr("三。", final: true)]))
        XCTAssertEqual(events.first, .confirmed(source: "Three.", translation: "三。"))
    }

    /// 停止時の終端の印（<fin>）は本文に混ぜない
    func testFinTokenIsNotText() {
        var assembler = SonioxCaptionAssembler(translates: false)
        let events = assembler.apply(response([
            src("Bye", final: true),
            .init(text: SonioxCaptionAssembler.finToken, isFinal: true, isTranslation: false),
        ]))
        XCTAssertEqual(events.first, .confirmed(source: "Bye", translation: ""))
    }

    // MARK: - 中-3 短い確定行のまとめ

    /// 古い行を読み終わっていない状態で上限に達したら、短い行は直前の行へまとめる
    func testShortLinesMergeInsteadOfEvicting() {
        let max = 2
        // 上限に達していて、古い行がまだ読まれていない → まとめる
        XCTAssertTrue(CaptionLineMergePolicy.shouldMerge(
            newText: "はい。", lastText: "ありがとうございます。", activeCount: 2, oldestHasMetMinimum: false, maxLines: max))
        // まだ上限に達していない／古い行は読み終わった → 普通に積む
        XCTAssertFalse(CaptionLineMergePolicy.shouldMerge(
            newText: "はい。", lastText: "ありがとう。", activeCount: 1, oldestHasMetMinimum: false, maxLines: max))
        XCTAssertFalse(CaptionLineMergePolicy.shouldMerge(
            newText: "はい。", lastText: "ありがとう。", activeCount: 2, oldestHasMetMinimum: true, maxLines: max))
        // どちらも長い／まとめると長すぎる → まとめない
        let long = String(repeating: "あ", count: 30)
        XCTAssertFalse(CaptionLineMergePolicy.shouldMerge(
            newText: long, lastText: long, activeCount: 2, oldestHasMetMinimum: false, maxLines: max))
        XCTAssertFalse(CaptionLineMergePolicy.shouldMerge(
            newText: "はい。", lastText: String(repeating: "あ", count: 47), activeCount: 3,
            oldestHasMetMinimum: false, maxLines: max))
        XCTAssertFalse(CaptionLineMergePolicy.shouldMerge(
            newText: "はい。", lastText: nil, activeCount: 2, oldestHasMetMinimum: false, maxLines: max))
    }

    /// 前の文の訳がまだ空のまま結合しても、前の文の原文が表示本文から消えない
    func testMergedTextKeepsUntranslatedPartAsSource() {
        // 前の文は訳待ち（原文だけ）、後の文は訳済み
        XCTAssertEqual(
            CaptionLineMergePolicy.composedText(parts: [("", "Thank you."), ("はい。", "Yes.")]),
            "Thank you. はい。")
        // 全部訳済みなら訳だけをつなぐ（従来どおり）
        XCTAssertEqual(
            CaptionLineMergePolicy.composedText(parts: [("ありがとう。", "Thank you."), ("はい。", "Yes.")]),
            "ありがとう。はい。")
        // 訳が後から届いたら、その部分だけ訳に置き換わる
        var parts: [(japanese: String, source: String)] = [("", "Thank you."), ("はい。", "Yes.")]
        parts[0].japanese = "ありがとう。"
        XCTAssertEqual(CaptionLineMergePolicy.composedText(parts: parts), "ありがとう。はい。")
        // 全部訳が空（原文だけの行）は空のまま＝原文側の表示を保つ
        XCTAssertEqual(CaptionLineMergePolicy.composedText(parts: [("", "a"), ("", "b")]), "")
        XCTAssertEqual(CaptionLineMergePolicy.composedText(parts: []), "")
    }

    /// つなぎ方（日本語はそのまま、英語は空白を挟む）
    func testMergeJoin() {
        XCTAssertEqual(CaptionLineMergePolicy.join("ありがとう。", "はい。"), "ありがとう。はい。")
        XCTAssertEqual(CaptionLineMergePolicy.join("Thanks.", "Yes."), "Thanks. Yes.")
        XCTAssertEqual(CaptionLineMergePolicy.join("", "Yes."), "Yes.")
        XCTAssertEqual(CaptionLineMergePolicy.join("Thanks.", ""), "Thanks.")
    }

    // MARK: - 中-4 停止時の終端

    /// 終端は音声入力の Soniox と同じ「finalize → 長さ 0 のフレーム」で、待つのは 1.5 秒まで
    func testSonioxEndOfStreamMessages() throws {
        guard #available(macOS 26.0, *) else { throw XCTSkip("ライブ字幕は macOS 26 以降") }
        XCTAssertEqual(SonioxCaptionStream.finishWaitLimit, 1.5)
        let messages = SonioxCaptionStream.endOfStreamMessages
        XCTAssertEqual(messages.count, 2)
        guard case let .string(first) = messages[0] else { return XCTFail("1 つ目が finalize の JSON ではない") }
        let object = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(first.utf8)) as? [String: String])
        XCTAssertEqual(object, ["type": "finalize"])
        guard case let .data(second) = messages[1] else { return XCTFail("2 つ目が長さ 0 のフレームではない") }
        XCTAssertTrue(second.isEmpty)
    }

    /// 接続していないストリームの停止は待たずに戻る（終端を送る相手がいない）
    func testSonioxStopWithoutConnectionReturnsImmediately() async throws {
        guard #available(macOS 26.0, *) else { throw XCTSkip("ライブ字幕は macOS 26 以降") }
        let stream = SonioxCaptionStream(translates: true, language: "en")
        let started = CFAbsoluteTimeGetCurrent()
        await stream.stop()
        await stream.stop()
        XCTAssertLessThan(CFAbsoluteTimeGetCurrent() - started, 0.5)
    }

    // MARK: - 中-5 遅れて届いたクラウドの訳

    /// 締め切り（1.5 秒）を過ぎて届いたクラウドの訳も、同じ行の差し替えとして渡す
    func testLateRefinementStillRevises() throws {
        guard #available(macOS 26.0, *) else { throw XCTSkip("ライブ字幕は macOS 26 以降") }
        let events = OSAllocatedUnfairLock(initialState: [String]())
        let revisedSequence = OSAllocatedUnfairLock(initialState: -1)
        let coordinator = TranslationCoordinator(
            translatorProvider: { EchoTranslator(tag: "AP:") },
            sequenceProvider: { 5 },
            refinerProvider: { EchoTranslator(tag: "CL:", delay: 2.5) }
        )
        coordinator.onTranslated = { _, japanese, _ in events.withLock { $0.append(japanese) } }
        coordinator.onRevised = { _, japanese, sequence in
            events.withLock { $0.append(japanese) }
            revisedSequence.withLock { $0 = sequence }
        }

        coordinator.submit("Thanks for coming.")
        wait(seconds: 2.0)
        XCTAssertEqual(events.withLock { $0 }, ["AP:Thanks for coming."])
        wait(seconds: 1.5)
        XCTAssertEqual(events.withLock { $0 }, ["AP:Thanks for coming.", "CL:Thanks for coming."],
                       "締め切り後に届いたクラウドの訳を捨てた")
        XCTAssertEqual(revisedSequence.withLock { $0 }, 5)
    }

    /// 詰まった差し替えは締め切りで待つのをやめ、次の行の差し替えを止めない
    func testStalledRefinementDoesNotBlockNextLine() throws {
        guard #available(macOS 26.0, *) else { throw XCTSkip("ライブ字幕は macOS 26 以降") }
        let revised = OSAllocatedUnfairLock(initialState: [String]())
        let refiner = SlowFirstTranslator(stall: 4)
        let counter = OSAllocatedUnfairLock(initialState: 0)
        let coordinator = TranslationCoordinator(
            translatorProvider: { EchoTranslator(tag: "AP:") },
            sequenceProvider: { counter.withLock { $0 += 1; return $0 } },
            refinerProvider: { refiner }
        )
        coordinator.onRevised = { _, japanese, _ in revised.withLock { $0.append(japanese) } }

        coordinator.submit("First line.")
        wait(seconds: 0.3)
        coordinator.submit("Second line.")
        // 1 行目の締め切り（1.5 秒）で 2 行目へ進む。2 行目の締め切り（出してから 1.5 秒）より前に届く
        wait(seconds: 1.6)
        XCTAssertEqual(revised.withLock { $0 }, ["CL:Second line."], "詰まった 1 行目に 2 行目が巻き込まれた")
    }

    /// クラウドが失敗したら（429 など）Apple の訳のまま
    func testRefineFailureKeepsAppleTranslation() throws {
        guard #available(macOS 26.0, *) else { throw XCTSkip("ライブ字幕は macOS 26 以降") }
        let events = OSAllocatedUnfairLock(initialState: [String]())
        let coordinator = TranslationCoordinator(
            translatorProvider: { EchoTranslator(tag: "AP:") },
            sequenceProvider: { 1 },
            refinerProvider: { FailingTranslator() }
        )
        coordinator.onTranslated = { _, japanese, _ in events.withLock { $0.append(japanese) } }
        coordinator.onRevised = { _, japanese, _ in events.withLock { $0.append(japanese) } }
        coordinator.submit("Rate limited.")
        wait(seconds: 0.5)
        XCTAssertEqual(events.withLock { $0 }, ["AP:Rate limited."])
    }

    // MARK: - 中-6 Soniox を諦めた理由

    func testSonioxGiveUpWording() {
        XCTAssertEqual(SonioxReconnectPolicy.giveUpReason(errorCode: 402), "Soniox の残高不足")
        XCTAssertEqual(SonioxReconnectPolicy.giveUpReason(errorCode: 401), "Soniox のキーが無効")
        XCTAssertEqual(SonioxReconnectPolicy.giveUpReason(errorCode: 403), "Soniox のキーが無効")
        XCTAssertEqual(SonioxReconnectPolicy.giveUpReason(errorCode: 503), "Soniox がエラーを返しました（503）")
        XCTAssertEqual(SonioxCaptionLinkState.fallbackMenuText(reason: "Soniox の残高不足"),
                       "Soniox → Apple に切替中（Soniox の残高不足）")
        XCTAssertEqual(SonioxCaptionLinkState.connected.menuText, "Soniox 接続中")
        XCTAssertEqual(SonioxCaptionLinkState.idle.menuText, "Soniox 待機中（無音のため切断）")
    }

    // MARK: - 低 確定の前倒しの詰まり

    /// 返ってこない前倒しは 2 秒で見捨てて次を出す。見捨てるのは 3 回まで、返事が来たら数え直す
    func testFinalizeGateRecoversFromStuckRequest() {
        var gate = FinalizeRequestGate()
        let first = gate.begin(now: 0)
        XCTAssertNotNil(first)
        XCTAssertNil(gate.begin(now: 1.0), "返事を待たずに重ねた")
        // 2 秒返ってこなければ見捨てて次を出す（以前はここで永久に止まっていた）
        let second = gate.begin(now: 2.0)
        XCTAssertNotNil(second)
        // 見捨てた古い依頼の返事は、新しい依頼の印を消さない
        gate.end(id: try! XCTUnwrap(first))
        XCTAssertNotNil(gate.inFlightID)
        XCTAssertNotNil(gate.begin(now: 4.0))
        XCTAssertNotNil(gate.begin(now: 6.0))
        XCTAssertEqual(gate.abandoned, FinalizeRequestGate.maxAbandoned)
        XCTAssertNil(gate.begin(now: 100), "固まった解析器に依頼を積み上げ続けた")
        // 返事が来たら元に戻る
        gate.end(id: try! XCTUnwrap(gate.inFlightID))
        XCTAssertEqual(gate.abandoned, 0)
        XCTAssertNotNil(gate.begin(now: 101))
    }

    // MARK: - 補助

    private func wait(seconds: TimeInterval) {
        let done = expectation(description: "wait")
        DispatchQueue.global().asyncAfter(deadline: .now() + seconds) { done.fulfill() }
        wait(for: [done], timeout: seconds + 2)
    }

    private func response(_ tokens: [SonioxCaptionAssembler.Token]) -> SonioxCaptionAssembler.Response {
        .init(tokens: tokens, finished: false, errorCode: nil, errorMessage: nil)
    }

    private func src(_ text: String, final: Bool) -> SonioxCaptionAssembler.Token {
        .init(text: text, isFinal: final, isTranslation: false, awaitsTranslation: true)
    }

    private func tr(_ text: String, final: Bool) -> SonioxCaptionAssembler.Token {
        .init(text: text, isFinal: final, isTranslation: true)
    }

    private func endToken() -> SonioxCaptionAssembler.Token {
        .init(text: SonioxCaptionAssembler.endToken, isFinal: true, isTranslation: false)
    }
}
