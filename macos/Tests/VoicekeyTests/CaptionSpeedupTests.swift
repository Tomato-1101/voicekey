//
//  CaptionSpeedupTests.swift
//  ライブ字幕の「速さ重視」改修（2026-10-12）の回帰テスト
//
//  - 切れ目で確定を前倒しする判定（PauseFinalizeTrigger）
//  - 停止・張り直しの後に届いた結果を捨てる世代（CaptionGenerationGate）
//  - 確定後に古い部分訳を出さない（PartialTranslationDriver の epoch）
//  - あふれても確定文を落とさない／Apple 先出し → クラウド差し替え → 時間切れ（TranslationCoordinator）
//  - ライブ行の巻き戻り（CaptionLiveLinePolicy）
//  すべてオフライン（ネットワーク・鍵・音声なし）。
//

import XCTest
import os

@testable import voicekey

/// 先頭に印を付けて返すだけの翻訳器（遅延を指定できる）
@available(macOS 26.0, *)
private struct TaggedTranslator: Translator {
    let tag: String
    var delay: TimeInterval = 0

    func translate(_ text: String, context: [TranslationExchange]) async throws -> String {
        if delay > 0 { try await Task.sleep(nanoseconds: UInt64(delay * 1_000_000_000)) }
        return tag + text
    }
}

/// 最初の 1 回だけ長く待つ翻訳器（クラウドが詰まった状態の再現）
@available(macOS 26.0, *)
private final class StallOnceTranslator: Translator, @unchecked Sendable {
    private let calls = OSAllocatedUnfairLock(initialState: 0)
    let stall: TimeInterval

    init(stall: TimeInterval) { self.stall = stall }

    func translate(_ text: String, context: [TranslationExchange]) async throws -> String {
        let first = calls.withLock { count -> Bool in
            count += 1
            return count == 1
        }
        if first { try await Task.sleep(nanoseconds: UInt64(stall * 1_000_000_000)) }
        return "主:" + text
    }
}

final class CaptionSpeedupTests: XCTestCase {

    // MARK: - PauseFinalizeTrigger

    /// 話し声のあと 0.3 秒の無音で 1 回だけ確定を頼む
    func testPauseTriggerFiresOnceAfterSilence() {
        var trigger = PauseFinalizeTrigger()
        XCTAssertFalse(trigger.observeAudio(rms: 0.05, duration: 0.1, now: 0.0))
        XCTAssertFalse(trigger.observeAudio(rms: 0.001, duration: 0.1, now: 0.1))
        XCTAssertFalse(trigger.observeAudio(rms: 0.001, duration: 0.1, now: 0.2))
        XCTAssertTrue(trigger.observeAudio(rms: 0.001, duration: 0.1, now: 0.3), "0.3 秒の無音で確定を頼んでいない")
        // 同じ切れ目で何度も頼まない（確定させる中身が無い）
        XCTAssertFalse(trigger.observeAudio(rms: 0.001, duration: 0.5, now: 0.8))
        XCTAssertFalse(trigger.observeTick(now: 5.0))
    }

    /// 無音だけが続いても頼まない（話し声が無ければ確定させる中身が無い）
    func testPauseTriggerIgnoresSilenceWithoutSpeech() {
        var trigger = PauseFinalizeTrigger()
        for step in 0..<20 {
            XCTAssertFalse(trigger.observeAudio(rms: 0.0005, duration: 0.1, now: Double(step) * 0.1))
        }
        XCTAssertFalse(trigger.observeTick(now: 10))
    }

    /// タップがフレームを配らなくなった（対象が黙った）ときも、経過時間で頼む
    func testPauseTriggerFiresOnFrameGap() {
        var trigger = PauseFinalizeTrigger()
        XCTAssertFalse(trigger.observeAudio(rms: 0.05, duration: 0.1, now: 1.0))
        XCTAssertFalse(trigger.observeTick(now: 1.2))
        XCTAssertTrue(trigger.observeTick(now: 1.31), "フレーム途絶で確定を頼んでいない")
        XCTAssertFalse(trigger.observeTick(now: 2.0))
        // 再び話し始めたら、次の切れ目でまた頼める
        XCTAssertFalse(trigger.observeAudio(rms: 0.05, duration: 0.1, now: 3.0))
        XCTAssertTrue(trigger.observeTick(now: 3.4))
    }

    // MARK: - 世代

    /// 停止・張り直しの後は、前の世代の結果を受け付けない
    func testGenerationGateRejectsStaleResults() throws {
        guard #available(macOS 26.0, *) else { throw XCTSkip("ライブ字幕は macOS 26 以降") }
        let gate = CaptionGenerationGate()
        let first = gate.begin()
        XCTAssertTrue(gate.isCurrent(first))

        // 停止
        let stopped = gate.invalidate()
        XCTAssertFalse(gate.isCurrent(first), "停止後に古いセッションの結果が出てしまう")
        XCTAssertTrue(gate.isCurrent(stopped), "停止後に新しいセッションが始まっていないのに後片付けが走らない")

        // 準備中に停止 → 再開。準備が終わった古い起動は復活しない
        let second = gate.begin()
        XCTAssertFalse(gate.isCurrent(first))
        XCTAssertFalse(gate.isCurrent(stopped), "再開済みなのに古い後片付けが議事録を閉じてしまう")
        XCTAssertTrue(gate.isCurrent(second))
        XCTAssertEqual(gate.current, second)
    }

    // MARK: - 部分訳

    /// 訳している間に確定が来たら、その部分訳は出さない（確定訳の後に古い文が再表示されない）
    func testPartialResultDroppedAfterFinal() throws {
        guard #available(macOS 26.0, *) else { throw XCTSkip("ライブ字幕は macOS 26 以降") }
        let received = OSAllocatedUnfairLock(initialState: [String]())
        let driver = PartialTranslationDriver(
            translatorProvider: { TaggedTranslator(tag: "部:", delay: 0.2) },
            sequenceProvider: { 1 }
        )
        driver.onPartial = { _, japanese, _ in received.withLock { $0.append(japanese) } }

        driver.submit("Hello there")
        wait(seconds: 0.05)
        // 確定が来た
        driver.reset()
        wait(seconds: 0.4)
        XCTAssertEqual(received.withLock { $0 }, [], "確定後に古い部分訳が出た")

        // 確定が来なければ普通に出る（判定が厳しすぎないこと）
        driver.submit("How are you")
        wait(seconds: 0.4)
        XCTAssertEqual(received.withLock { $0 }, ["部:How are you"])
    }

    /// 速さ重視の調整値（意図せず戻されないように値を固定する）
    func testSpeedTuningValues() throws {
        guard #available(macOS 26.0, *) else { throw XCTSkip("ライブ字幕は macOS 26 以降") }
        XCTAssertEqual(PartialTranslationDriver.throttleInterval, 0.18, accuracy: 0.001)
        XCTAssertEqual(PartialTranslationDriver.minimumCharacters, 3)
        XCTAssertEqual(TranslationCoordinator.maxBufferedCharacters, 40)
        XCTAssertEqual(TranslationCoordinator.clauseBreakMinimumCharacters, 24)
        XCTAssertEqual(TranslationCoordinator.idleFlushInterval, 0.25, accuracy: 0.001)
        XCTAssertEqual(TranslationCoordinator.refineTimeout, 1.5, accuracy: 0.001)
        XCTAssertEqual(PauseFinalizeTrigger.pauseSeconds, 0.3, accuracy: 0.001)
    }

    // MARK: - TranslationCoordinator

    /// 翻訳キューが溢れても確定文を捨てない（溢れた分は Apple で訳す）
    func testOverflowDoesNotDropLines() throws {
        guard #available(macOS 26.0, *) else { throw XCTSkip("ライブ字幕は macOS 26 以降") }
        let translated = OSAllocatedUnfairLock(initialState: [String: String]())
        let counter = OSAllocatedUnfairLock(initialState: 0)
        let primary = StallOnceTranslator(stall: 0.6)
        let coordinator = TranslationCoordinator(
            translatorProvider: { primary },
            sequenceProvider: { counter.withLock { $0 += 1; return $0 } },
            overflowTranslatorProvider: { TaggedTranslator(tag: "AP:") }
        )
        coordinator.onTranslated = { source, japanese, _ in
            translated.withLock { $0[source] = japanese }
        }

        let lines = (1...14).map { "This is sentence number \($0)." }
        for line in lines { coordinator.submit(line) }
        wait(seconds: 1.5)

        let result = translated.withLock { $0 }
        XCTAssertEqual(Set(result.keys), Set(lines), "溢れた確定文が落ちた: \(Set(lines).subtracting(result.keys))")
        XCTAssertTrue(result.values.contains { $0.hasPrefix("AP:") }, "溢れた分を Apple で訳していない")
    }

    /// クラウド選択時: Apple の訳を先に出し、締め切り内に届いたクラウドの訳で同じ行を差し替える
    func testAppleFirstThenRefined() throws {
        guard #available(macOS 26.0, *) else { throw XCTSkip("ライブ字幕は macOS 26 以降") }
        let events = OSAllocatedUnfairLock(initialState: [String]())
        let shownSequence = OSAllocatedUnfairLock(initialState: 0)
        let revisedSequence = OSAllocatedUnfairLock(initialState: -1)
        let coordinator = TranslationCoordinator(
            translatorProvider: { TaggedTranslator(tag: "AP:") },
            sequenceProvider: { 7 },
            refinerProvider: { TaggedTranslator(tag: "CL:", delay: 0.2) }
        )
        coordinator.onTranslated = { _, japanese, sequence in
            events.withLock { $0.append(japanese) }
            shownSequence.withLock { $0 = sequence }
        }
        coordinator.onRevised = { _, japanese, sequence in
            events.withLock { $0.append(japanese) }
            revisedSequence.withLock { $0 = sequence }
        }

        coordinator.submit("Good morning everyone.")
        wait(seconds: 0.6)

        XCTAssertEqual(events.withLock { $0 }, ["AP:Good morning everyone.", "CL:Good morning everyone."])
        XCTAssertEqual(revisedSequence.withLock { $0 }, shownSequence.withLock { $0 }, "差し替え先の行が違う")
    }

    /// クラウドの訳が締め切り（1.5 秒）までに来なければ Apple の訳のまま確定する
    func testRefineTimesOutKeepingAppleTranslation() throws {
        guard #available(macOS 26.0, *) else { throw XCTSkip("ライブ字幕は macOS 26 以降") }
        let events = OSAllocatedUnfairLock(initialState: [String]())
        let coordinator = TranslationCoordinator(
            translatorProvider: { TaggedTranslator(tag: "AP:") },
            sequenceProvider: { 1 },
            refinerProvider: { TaggedTranslator(tag: "CL:", delay: 3) }
        )
        coordinator.onTranslated = { _, japanese, _ in events.withLock { $0.append(japanese) } }
        coordinator.onRevised = { _, japanese, _ in events.withLock { $0.append(japanese) } }

        coordinator.submit("Thanks for coming.")
        wait(seconds: 2.0)

        XCTAssertEqual(events.withLock { $0 }, ["AP:Thanks for coming."], "時間切れのクラウド訳で差し替えた")
    }

    /// 締め切り付き翻訳の単体（訳が先なら訳、時間切れが先なら nil）
    func testTranslateWithTimeout() async throws {
        guard #available(macOS 26.0, *) else { throw XCTSkip("ライブ字幕は macOS 26 以降") }
        let fast = await TranslationCoordinator.translate(
            "a", context: [], with: TaggedTranslator(tag: "x", delay: 0.05), timeout: 1)
        XCTAssertEqual(fast, "xa")
        let slow = await TranslationCoordinator.translate(
            "a", context: [], with: TaggedTranslator(tag: "x", delay: 1), timeout: 0.1)
        XCTAssertNil(slow)
        let expired = await TranslationCoordinator.translate(
            "a", context: [], with: TaggedTranslator(tag: "x"), timeout: 0)
        XCTAssertNil(expired, "締め切りを過ぎた行をクラウドへ送った")
    }

    // MARK: - CaptionLiveLinePolicy

    /// 翻訳待ちの古い原文で、新しい話を出しているライブ行を上書きしない（巻き戻り）
    func testLiveLineDoesNotRewindToOlderSentence() {
        let current = CaptionLiveLineText(japanese: "次の話の途中", source: "And then we")
        XCTAssertFalse(CaptionLiveLinePolicy.accepts(
            .pendingSource("The previous sentence."), current: current, lastConfirmed: nil))
        // ライブ行が空なら穴埋めに出す
        XCTAssertTrue(CaptionLiveLinePolicy.accepts(
            .pendingSource("The previous sentence."), current: nil, lastConfirmed: nil))
        XCTAssertTrue(CaptionLiveLinePolicy.accepts(
            .pendingSource("The previous sentence."),
            current: CaptionLiveLineText(japanese: "", source: ""), lastConfirmed: nil))
    }

    /// 確定したばかりの文をライブ行に二重に出さない
    func testLiveLineRejectsJustConfirmedText() {
        let last = CaptionLiveLineText(japanese: "こんにちは", source: "Hello")
        XCTAssertFalse(CaptionLiveLinePolicy.accepts(
            .partial(source: "Hello", japanese: "こんにちは"), current: nil, lastConfirmed: last))
        XCTAssertFalse(CaptionLiveLinePolicy.accepts(.listening("Hello"), current: nil, lastConfirmed: last))
        XCTAssertTrue(CaptionLiveLinePolicy.accepts(
            .partial(source: "Next", japanese: "次"), current: nil, lastConfirmed: last))
    }

    /// 暫定訳が出ている間は生の原文で上書きしない（訳→原文→訳 のちらつき）
    func testListeningDoesNotOverwritePartialTranslation() {
        let current = CaptionLiveLineText(japanese: "途中の訳", source: "Partial")
        XCTAssertFalse(CaptionLiveLinePolicy.accepts(.listening("Partial text"), current: current, lastConfirmed: nil))
        XCTAssertTrue(CaptionLiveLinePolicy.accepts(
            .partial(source: "Partial text", japanese: "途中の訳の続き"), current: current, lastConfirmed: nil))
    }

    /// 表示は最大 3 段（確定 2 行＋ライブ 1 行）
    func testAtMostThreeLines() {
        XCTAssertTrue(CaptionLiveLinePolicy.showsLiveLine(confirmedCount: 2))
        XCTAssertFalse(CaptionLiveLinePolicy.showsLiveLine(confirmedCount: 3))
    }

    // MARK: - 補助

    private func wait(seconds: TimeInterval) {
        let done = expectation(description: "wait")
        DispatchQueue.global().asyncAfter(deadline: .now() + seconds) { done.fulfill() }
        wait(for: [done], timeout: seconds + 2)
    }
}
