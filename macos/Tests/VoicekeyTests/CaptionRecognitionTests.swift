//
//  CaptionRecognitionTests.swift
//  ライブ字幕・Meet ボットの「文字起こしエンジンを選ぶ」（2026-11-20 追加）の単体テスト
//
//  実 API には接続しない。保存値の解決と移行・発話の区切り・失敗時に Apple へ落とす判断・
//  「（おすすめ）」が一覧ごとに 1 つだけであることを、純ロジックと偽の認識係で確かめる。
//

import AVFoundation
import XCTest

@testable import voicekey

final class CaptionRecognitionTests: XCTestCase {

    // MARK: - 保存値の解決と移行

    /// 何も保存していなければ Apple（今までの挙動のまま）
    func testDefaultRecognitionIsApple() {
        let choice = CaptionRecognitionPolicy.resolve(storedBackend: nil, storedModel: nil, translationRaw: nil)
        XCTAssertEqual(choice.backend, .appleLocal)
        XCTAssertEqual(choice, CaptionRecognitionPolicy.defaultChoice)
        // 翻訳が Apple / Groq / Gemini のままでも認識は Apple
        for raw in ["apple", "groq", "gemini"] {
            XCTAssertEqual(
                CaptionRecognitionPolicy.resolve(storedBackend: nil, storedModel: nil, translationRaw: raw).backend,
                .appleLocal
            )
        }
    }

    /// 以前の「翻訳エンジン = Soniox」は、認識 Soniox ＋ 翻訳 Soniox として読む（同じ動きを保つ）
    func testLegacySonioxTranslationMigratesToSonioxRecognition() {
        let choice = CaptionRecognitionPolicy.resolve(storedBackend: nil, storedModel: nil, translationRaw: "soniox")
        XCTAssertEqual(choice, EngineModelChoice(backend: .soniox, model: Backend.soniox.defaultModel))
        XCTAssertEqual(CaptionRecognitionPolicy.effectiveTranslation(chosen: .soniox, recognition: choice.backend), .soniox)
    }

    /// 認識を保存した後は、翻訳の保存値に引きずられない
    func testStoredRecognitionWinsOverLegacyTranslation() {
        let choice = CaptionRecognitionPolicy.resolve(
            storedBackend: Backend.groq.rawValue, storedModel: "whisper-large-v3-turbo", translationRaw: "soniox"
        )
        XCTAssertEqual(choice, EngineModelChoice(backend: .groq, model: "whisper-large-v3-turbo"))
    }

    /// 壊れた保存値・選べなくなったエンジンは Apple へ倒す（課金エンジンへは倒さない）
    func testBrokenStoredRecognitionFallsBackToApple() {
        XCTAssertEqual(
            CaptionRecognitionPolicy.resolve(storedBackend: "nope", storedModel: "x", translationRaw: nil).backend,
            .appleLocal
        )
        XCTAssertEqual(
            CaptionRecognitionPolicy.resolve(
                storedBackend: Backend.deepgram.rawValue, storedModel: "nova-3", translationRaw: nil
            ).backend,
            .appleLocal
        )
    }

    /// モデルが空で保存されていたら、そのエンジンの既定モデルを使う
    func testEmptyStoredModelUsesDefaultModel() {
        let choice = CaptionRecognitionPolicy.resolve(
            storedBackend: Backend.elevenlabs.rawValue, storedModel: "", translationRaw: nil
        )
        XCTAssertEqual(choice, EngineModelChoice(backend: .elevenlabs, model: Backend.elevenlabs.defaultModel))
    }

    // MARK: - 翻訳エンジンとの組み合わせ

    /// 「Soniox（認識と一緒に訳す）」は認識が Soniox のときだけ。それ以外は課金の無い Apple で訳す
    func testSonioxTranslationNeedsSonioxRecognition() {
        XCTAssertEqual(CaptionRecognitionPolicy.effectiveTranslation(chosen: .soniox, recognition: .appleLocal), .apple)
        XCTAssertEqual(CaptionRecognitionPolicy.effectiveTranslation(chosen: .soniox, recognition: .openai), .apple)
        // ほかの翻訳エンジンは認識に関係なくそのまま
        XCTAssertEqual(CaptionRecognitionPolicy.effectiveTranslation(chosen: .groq, recognition: .soniox), .groq)
        XCTAssertEqual(CaptionRecognitionPolicy.effectiveTranslation(chosen: .gemini, recognition: .appleLocal), .gemini)
    }

    /// 翻訳の一覧に Soniox が出るのは、認識が Soniox のときだけ
    func testTranslationChoicesHideSonioxUnlessRecognitionIsSoniox() {
        XCTAssertTrue(CaptionRecognitionPolicy.translationChoices(recognition: .soniox).contains(.soniox))
        for backend in Backend.selectableCases where backend != .soniox {
            let choices = CaptionRecognitionPolicy.translationChoices(recognition: backend)
            XCTAssertFalse(choices.contains(.soniox), backend.rawValue)
            XCTAssertEqual(Set(choices), [.apple, .gemini, .groq])
        }
    }

    // MARK: - おすすめは一覧ごとに 1 つ

    /// 文字起こしエンジンの一覧（録音キー・字幕・Meet 録音で共用）のおすすめは Soniox の 1 つだけ
    func testEngineListHasExactlyOneRecommendation() {
        let groups = EngineModelCatalog.groups(current: CaptionRecognitionPolicy.defaultChoice)
        let marked = groups.flatMap(\.options).filter { $0.label.contains("（おすすめ）") }
        XCTAssertEqual(marked.map(\.id), [EngineModelCatalog.recommended])
        XCTAssertEqual(marked.first?.label, "Soniox · stt-rt-v5（おすすめ）")
        // 旧表記は残さない
        XCTAssertFalse(groups.flatMap(\.options).contains { $0.label.contains("推奨") })
    }

    /// 翻訳エンジンの一覧のおすすめは、認識が Soniox なら Soniox、それ以外は Groq の 1 つだけ
    func testTranslationListHasExactlyOneRecommendation() {
        for backend in Backend.selectableCases {
            let labels = translationLabels(recognition: backend)
            let marked = labels.filter { $0.contains("（おすすめ）") }
            XCTAssertEqual(marked.count, 1, "\(backend.rawValue): \(labels)")
            if backend == .soniox {
                XCTAssertEqual(marked.first, "Soniox（認識と一緒に訳す）（おすすめ）")
            } else {
                XCTAssertEqual(CaptionRecognitionPolicy.recommendedTranslation(recognition: backend), .groq)
                XCTAssertTrue(marked.first?.hasPrefix("groq:") ?? false, "\(marked)")
            }
        }
    }

    /// 設定画面と同じ組み立てで、翻訳一覧のラベルを作る（Groq / Gemini は見分けられるよう接頭辞を付ける）
    private func translationLabels(recognition: Backend) -> [String] {
        CaptionRecognitionPolicy.translationChoices(recognition: recognition).flatMap { engine -> [String] in
            switch engine {
            case .apple, .soniox:
                return [CaptionRecognitionPolicy.markedTranslationLabel(
                    engine.displayName, engine: engine, recognition: recognition
                )]
            case .gemini, .groq:
                let provider: LLMProvider = engine == .groq ? .groq : .gemini
                return LLMModelCatalog.options(provider, current: LLMModelCatalog.recommended(provider), available: nil)
                    .map {
                        "\(engine.rawValue):" + CaptionRecognitionPolicy.markedTranslationLabel(
                            $0.label, engine: engine, recognition: recognition
                        )
                    }
            }
        }
    }

    /// 表示名の揺れをそろえた（Apple 翻訳は「端末内・無料」、Meet の取り方のおすすめは字幕）
    func testDisplayNames() {
        XCTAssertEqual(TranslationEngine.apple.displayName, "Apple 翻訳（端末内・無料）")
        XCTAssertEqual(MeetTranscriptSource.meetCaptions.displayName, "Meet の字幕を読む（おすすめ）")
        XCTAssertEqual(MeetTranscriptSource.localSpeech.displayName, "自分で録音して文字起こし")
        XCTAssertEqual(
            MeetTranscriptSource.allCases.filter { $0.displayName.contains("（おすすめ）") }, [.meetCaptions]
        )
    }

    /// メニューの状態行は提供元の名前で出す（Soniox の従来の文言は変えない）
    func testLinkStateTextUsesProviderName() {
        XCTAssertEqual(SonioxCaptionLinkState.connected.menuText, "Soniox 接続中")
        XCTAssertEqual(
            SonioxCaptionLinkState.fallbackMenuText(provider: "OpenAI", reason: "APIキー未設定"),
            "OpenAI → Apple に切替中（APIキー未設定）"
        )
    }

    // MARK: - 発話の区切り

    private let rate = Int(UtteranceSegmenter.sampleRate)

    private func voice(_ seconds: Double) -> [Float] { [Float](repeating: 0.1, count: Int(seconds * Double(rate))) }
    private func silence(_ seconds: Double) -> [Float] { [Float](repeating: 0, count: Int(seconds * Double(rate))) }

    /// 無音は送らない。声が出たら直前の少しの音を付けて始める
    func testSilenceIsNotSentAndPreRollIsKept() {
        var segmenter = UtteranceSegmenter()
        XCTAssertEqual(segmenter.append(silence(1.0), now: 0), [])
        XCTAssertFalse(segmenter.isOpen)
        let events = segmenter.append(voice(0.1), now: 1)
        XCTAssertEqual(events.first, .begin)
        guard case let .audio(head)? = events.last else { return XCTFail("音声が出ること") }
        // 直前 0.2 秒ぶんだけを付ける（1 秒の無音を丸ごとは送らない）
        XCTAssertEqual(head.count, Int((UtteranceSegmenter.preRoll + 0.1) * Double(rate)))
    }

    /// 無音が 0.6 秒続いたら発話を終える
    func testEndsAfterSilence() {
        var segmenter = UtteranceSegmenter()
        _ = segmenter.append(voice(1.0), now: 0)
        XCTAssertFalse(segmenter.append(silence(0.3), now: 1).contains(.end(isSpeech: true)))
        XCTAssertTrue(segmenter.isOpen)
        XCTAssertEqual(segmenter.append(silence(0.3), now: 1.3).last, .end(isSpeech: true))
        XCTAssertFalse(segmenter.isOpen)
    }

    /// 切れ目なく話し続けても 15 秒で一度区切り、次の声からまた始める
    func testCutsAtMaxLength() {
        var segmenter = UtteranceSegmenter()
        var ends = 0
        var begins = 0
        for index in 0..<32 {
            for event in segmenter.append(voice(1.0), now: Double(index)) {
                if event == .begin { begins += 1 }
                if event == .end(isSpeech: true) { ends += 1 }
            }
        }
        XCTAssertEqual(ends, 2)
        XCTAssertEqual(begins, 3)
    }

    /// 物音 1 発のような短い音は文字起こしに回さない
    func testShortNoiseIsNotSpeech() {
        var segmenter = UtteranceSegmenter()
        _ = segmenter.append(voice(0.1), now: 0)
        XCTAssertEqual(segmenter.append(silence(0.7), now: 0.1).last, .end(isSpeech: false))
    }

    /// タップは黙っている間フレームを配らないので、フレームが途絶えた時間でも終わりを判定する
    func testTickEndsWhenFramesStop() {
        var segmenter = UtteranceSegmenter()
        _ = segmenter.append(voice(1.0), now: 10)
        XCTAssertNil(segmenter.tick(now: 10.3))
        XCTAssertEqual(segmenter.tick(now: 10.7), .end(isSpeech: true))
        XCTAssertNil(segmenter.tick(now: 11.5))
    }

    /// 停止のときは途中の発話も終わらせる（最後の 1 文を落とさない）
    func testFlushClosesOpenUtterance() {
        var segmenter = UtteranceSegmenter()
        XCTAssertNil(segmenter.flush())
        _ = segmenter.append(voice(0.5), now: 0)
        XCTAssertEqual(segmenter.flush(), .end(isSpeech: true))
        XCTAssertNil(segmenter.flush())
    }

    // MARK: - 失敗したときに Apple へ落とす判断

    /// 一時的な失敗は 3 回続くまで諦めない。成功したら数え直す
    func testFailurePolicyGivesUpAfterThreeInARow() {
        var policy = UtteranceFailurePolicy()
        XCTAssertFalse(policy.noteFailure(isFatal: false))
        XCTAssertFalse(policy.noteFailure(isFatal: false))
        policy.noteSuccess()
        XCTAssertFalse(policy.noteFailure(isFatal: false))
        XCTAssertFalse(policy.noteFailure(isFatal: false))
        XCTAssertTrue(policy.noteFailure(isFatal: false))
    }

    /// キー無し・無効・レート制限は 1 回で諦める（続けても直らない）
    func testFailurePolicyGivesUpImmediatelyOnFatal() {
        var policy = UtteranceFailurePolicy()
        XCTAssertTrue(policy.noteFailure(isFatal: true))
    }

    /// REST の失敗の分類: キー無し・429 は致命的、通信失敗は一時的
    func testRestRecognizerClassifiesFailures() async {
        func outcome(_ error: Error) async -> UtteranceOutcome {
            let recognizer = RestUtteranceRecognizer(providerName: "Groq") { _ in throw error }
            recognizer.feed([0.1])
            return await recognizer.finish()
        }
        guard case let .failed(_, keyFatal) = await outcome(TranscriptionError(message: "x", needsApiKey: true)),
              case let .failed(_, rateFatal) = await outcome(TranscriptionError(message: "x", isRateLimited: true)),
              case let .failed(_, netFatal) = await outcome(URLError(.timedOut))
        else { return XCTFail("失敗として返ること") }
        XCTAssertTrue(keyFatal)
        XCTAssertTrue(rateFatal)
        XCTAssertFalse(netFatal)
    }

    // MARK: - 発話単位のストリーム（偽の認識係）

    /// 偽の認識係（決めた結果を、決めた待ち時間の後に返す）
    private final class FakeRecognizer: UtteranceRecognizing, @unchecked Sendable {
        var onInterim: ((String) -> Void)?
        let outcome: UtteranceOutcome
        let delay: TimeInterval
        init(_ outcome: UtteranceOutcome, delay: TimeInterval = 0) {
            self.outcome = outcome
            self.delay = delay
        }
        func begin() -> Bool { true }
        func feed(_ samples: [Float]) {}
        func finish() async -> UtteranceOutcome {
            if delay > 0 { try? await Task.sleep(for: .seconds(delay)) }
            return outcome
        }
        func cancel() {}
    }

    /// 決めた順に認識係を渡す
    private final class RecognizerQueue: @unchecked Sendable {
        private let lock = NSLock()
        private var items: [FakeRecognizer]
        init(_ items: [FakeRecognizer]) { self.items = items }
        func next() -> FakeRecognizer {
            lock.lock()
            defer { lock.unlock() }
            return items.isEmpty ? FakeRecognizer(.text("")) : items.removeFirst()
        }
    }

    /// 結果を集める
    private final class Collector: @unchecked Sendable {
        private let lock = NSLock()
        private var _confirmed: [String] = []
        private var _giveUps: [String] = []
        func confirm(_ text: String) { lock.lock(); _confirmed.append(text); lock.unlock() }
        func giveUp(_ reason: String) { lock.lock(); _giveUps.append(reason); lock.unlock() }
        var confirmed: [String] { lock.lock(); defer { lock.unlock() }; return _confirmed }
        var giveUps: [String] { lock.lock(); defer { lock.unlock() }; return _giveUps }
    }

    private func buffer(_ samples: [Float]) -> AVAudioPCMBuffer {
        let format = AVAudioFormat(standardFormatWithSampleRate: UtteranceSegmenter.sampleRate, channels: 1)!
        let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(samples.count))!
        buffer.frameLength = AVAudioFrameCount(samples.count)
        samples.withUnsafeBufferPointer { source in
            buffer.floatChannelData![0].update(from: source.baseAddress!, count: samples.count)
        }
        return buffer
    }

    @available(macOS 26.0, *)
    private func makeStream(_ recognizers: [FakeRecognizer], canStart: Bool = true) -> (UtteranceCaptionStream, Collector) {
        let queue = RecognizerQueue(recognizers)
        let collector = Collector()
        let stream = UtteranceCaptionStream(providerName: "Fake", canStart: { canStart }, makeRecognizer: { queue.next() })
        stream.onConfirmed = { text, _ in collector.confirm(text) }
        stream.onGiveUp = { collector.giveUp($0) }
        return (stream, collector)
    }

    /// キーが無ければ開始しない（呼び出し側が Apple で始める）
    func testStreamDoesNotStartWithoutKey() throws {
        guard #available(macOS 26.0, *) else { throw XCTSkip("macOS 26 以降") }
        let (stream, _) = makeStream([], canStart: false)
        XCTAssertFalse(stream.start())
    }

    /// 先の発話の返事が遅くても、確定文は話した順に出る。停止時に話し途中だった文も落とさない
    func testStreamKeepsOrderAndFlushesOnStop() async throws {
        guard #available(macOS 26.0, *) else { throw XCTSkip("macOS 26 以降") }
        let (stream, collector) = makeStream([
            FakeRecognizer(.text("一つ目"), delay: 0.3), FakeRecognizer(.text("二つ目")),
        ])
        XCTAssertTrue(stream.start())
        stream.send(buffer(voice(0.5)))
        stream.send(buffer(silence(0.7)))
        stream.send(buffer(voice(0.5)))
        await stream.stop()
        XCTAssertEqual(collector.confirmed, ["一つ目", "二つ目"])
        XCTAssertEqual(collector.giveUps, [])
    }

    /// キー無効・レート制限は 1 回で諦めて知らせる（知らせるのは 1 回だけ）
    func testStreamGivesUpOnceOnFatalFailure() async throws {
        guard #available(macOS 26.0, *) else { throw XCTSkip("macOS 26 以降") }
        let (stream, collector) = makeStream([
            FakeRecognizer(.failed(reason: "Fake のレート制限", isFatal: true)),
            FakeRecognizer(.failed(reason: "二回目", isFatal: true)),
        ])
        XCTAssertTrue(stream.start())
        stream.send(buffer(voice(0.5)))
        stream.send(buffer(silence(0.7)))
        stream.send(buffer(voice(0.5)))
        stream.send(buffer(silence(0.7)))
        try await Task.sleep(for: .seconds(0.3))
        await stream.stop()
        XCTAssertEqual(collector.giveUps, ["Fake のレート制限"])
        XCTAssertEqual(collector.confirmed, [])
    }

    /// 一時的な失敗 1 回では諦めず、次の発話はそのまま確定する
    func testStreamSurvivesSingleTransientFailure() async throws {
        guard #available(macOS 26.0, *) else { throw XCTSkip("macOS 26 以降") }
        let (stream, collector) = makeStream([
            FakeRecognizer(.failed(reason: "通信失敗", isFatal: false)), FakeRecognizer(.text("続き")),
        ])
        XCTAssertTrue(stream.start())
        stream.send(buffer(voice(0.5)))
        stream.send(buffer(silence(0.7)))
        stream.send(buffer(voice(0.5)))
        await stream.stop()
        XCTAssertEqual(collector.confirmed, ["続き"])
        XCTAssertEqual(collector.giveUps, [])
    }
}
