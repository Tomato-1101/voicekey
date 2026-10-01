//
//  ApiUsageTests.swift
//  API 単価の計算と、日次集計の保存→読み込みの往復を確かめる（通信なし・課金なし）
//

import XCTest
@testable import voicekey

final class ApiUsageTests: XCTestCase {

    private func makeTempDir() -> URL {
        let dir = FileManager.default.temporaryDirectory
            .appendingPathComponent("ApiUsageTests-\(UUID().uuidString)", isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir
    }

    // MARK: - 単価の計算

    /// 音声の時間課金: 1 時間 = 表の単価そのもの、30 分 = 半額
    func testAudioSecondsToUSD() throws {
        let hour = try XCTUnwrap(ApiPricing.cost(
            provider: .soniox, model: "stt-rt-v5", billedAudioSeconds: 3600, inputTokens: 0, outputTokens: 0))
        XCTAssertEqual(hour, 0.12, accuracy: 1e-9)
        // gpt-transcribe は $0.0045/分 → 30 分で $0.135
        let half = try XCTUnwrap(ApiPricing.cost(
            provider: .openai, model: "gpt-transcribe", billedAudioSeconds: 1800, inputTokens: 0, outputTokens: 0))
        XCTAssertEqual(half, 0.135, accuracy: 1e-9)
    }

    /// Groq の音声は 1 リクエスト最低 10 秒で課金される
    func testGroqMinimumBilledSeconds() {
        XCTAssertEqual(ApiPricing.billedSeconds(provider: .groq, model: "whisper-large-v3-turbo", audioSeconds: 3), 10)
        XCTAssertEqual(ApiPricing.billedSeconds(provider: .groq, model: "whisper-large-v3-turbo", audioSeconds: 25), 25)
        // 最低課金の無いプロバイダーは実秒数のまま
        XCTAssertEqual(ApiPricing.billedSeconds(provider: .soniox, model: "stt-rt-v5", audioSeconds: 3), 3)
    }

    /// LLM のトークン課金: 入力・出力それぞれ 100 万トークンあたりの単価
    func testTokensToUSD() throws {
        let usd = try XCTUnwrap(ApiPricing.cost(
            provider: .groq, model: "openai/gpt-oss-20b", billedAudioSeconds: 0,
            inputTokens: 1_000_000, outputTokens: 500_000))
        XCTAssertEqual(usd, 0.075 + 0.15, accuracy: 1e-9)
        // Gemini（有料枠）: 入力 2,000・出力 1,000 → 0.0006 + 0.0025
        let gemini = try XCTUnwrap(ApiPricing.cost(
            provider: .gemini, model: "gemini-3.5-flash-lite", billedAudioSeconds: 0,
            inputTokens: 2_000, outputTokens: 1_000))
        XCTAssertEqual(gemini, 0.0031, accuracy: 1e-9)
    }

    /// 単価未確認は nil（0 円に見せない）、Apple は無料
    func testUnconfirmedAndFreePrices() {
        XCTAssertNil(ApiPricing.price(provider: .groq, model: "llama-3.1-8b-instant"))
        XCTAssertNil(ApiPricing.cost(
            provider: .elevenlabs, model: "scribe_v1", billedAudioSeconds: 60, inputTokens: 0, outputTokens: 0))
        XCTAssertEqual(ApiPricing.cost(
            provider: .apple, model: "オンデバイス音声認識", billedAudioSeconds: 600, inputTokens: 0, outputTokens: 0), 0)
        XCTAssertEqual(ApiProvider(backend: .openaiLive), .openai)
        XCTAssertEqual(ApiProvider(backend: .azureMAI), .microsoft)
    }

    // MARK: - 集計

    /// 同じ日・同じ API は 1 行にまとまり、未確認単価は合計に「含む」として立つ
    func testAggregationAndUnpricedFlag() {
        let store = ApiUsageStore(directory: makeTempDir())
        store.recordAudio(provider: .soniox, model: "stt-rt-v5", seconds: 1800)
        store.recordAudio(provider: .soniox, model: "stt-rt-v5", seconds: 1800)
        store.recordTokens(provider: .groq, model: "llama-3.1-8b-instant", purpose: .formatting,
                           inputTokens: 100, outputTokens: 50)

        let rows = store.breakdown(mode: .list) { _ in true }
        XCTAssertEqual(rows.count, 2)
        let soniox = rows.first { $0.provider == "soniox" }
        XCTAssertEqual(soniox?.requests, 2)
        XCTAssertEqual(soniox?.audioSeconds ?? 0, 3600, accuracy: 1e-9)

        let today = store.todaySummary(mode: .list)
        XCTAssertEqual(today.usd, 0.12, accuracy: 1e-9)
        XCTAssertTrue(today.hasUnpriced)
        XCTAssertEqual(today.requests, 3)
        XCTAssertEqual(store.dailySeries(30, mode: .list).count, 30)
        XCTAssertEqual(store.dailySeries(30, mode: .list).last?.summary.usd ?? 0, 0.12, accuracy: 1e-9)
    }

    /// 過去日の記録は今日には入らず、累計には入る
    func testPastDayCountsOnlyInAllTime() throws {
        let store = ApiUsageStore(directory: makeTempDir())
        let past = try XCTUnwrap(Calendar.current.date(byAdding: .day, value: -40, to: Date()))
        store.recordAudio(provider: .soniox, model: "stt-rt-v5", seconds: 3600, date: past)
        XCTAssertEqual(store.todaySummary(mode: .list).usd, 0)
        XCTAssertEqual(store.allTimeSummary(mode: .list).usd, 0.12, accuracy: 1e-9)
    }

    // MARK: - 表示モード（定価／実際に払った分）

    /// 実払いモードは OpenAI・Soniox・Microsoft だけを計上し、無料枠のプロバイダーは 0 円（行は残る）
    func testPaidModeCountsOnlyPaidProviders() {
        let store = ApiUsageStore(directory: makeTempDir())
        store.recordAudio(provider: .soniox, model: "stt-rt-v5", seconds: 3600)             // $0.12・有料
        store.recordAudio(provider: .openai, model: "gpt-transcribe", seconds: 3600)         // $0.27・有料
        store.recordAudio(provider: .groq, model: "whisper-large-v3-turbo", seconds: 3600)   // $0.04・無料枠
        store.recordAudio(provider: .elevenlabs, model: "scribe_v1", seconds: 60)            // 単価未確認・無料枠
        store.recordTokens(provider: .gemini, model: "gemini-3.5-flash-lite", purpose: .captionTranslation,
                           inputTokens: 1_000_000, outputTokens: 0)                          // $0.30・無料枠

        let list = store.todaySummary(mode: .list)
        XCTAssertEqual(list.usd, 0.12 + 0.27 + 0.04 + 0.30, accuracy: 1e-9)
        XCTAssertTrue(list.hasUnpriced, "定価では単価未確認が残る")

        let paid = store.todaySummary(mode: .paid)
        XCTAssertEqual(paid.usd, 0.12 + 0.27, accuracy: 1e-9)
        XCTAssertFalse(paid.hasUnpriced, "無料枠は未確認でも 0 円として確定する")
        XCTAssertEqual(paid.requests, 5, "回数はモードで変えない")
        // 無料枠の行も内訳に残る（0 円）
        let rows = store.breakdown(mode: .paid) { _ in true }
        XCTAssertEqual(rows.count, 5)
        XCTAssertEqual(rows.first { $0.provider == "groq" }?.costUSD(mode: .paid), 0)
        XCTAssertTrue(ApiCostMode.paid.isFreeTier(.groq))
        XCTAssertFalse(ApiCostMode.paid.isFreeTier(.openai))
        XCTAssertFalse(ApiCostMode.list.isFreeTier(.groq))
        XCTAssertEqual(store.dailySeries(30, mode: .paid).last?.summary.usd ?? 0, 0.39, accuracy: 1e-9)
    }

    /// 推定行は実測と別行で数え、合計の「うち推定」に出る。古い JSON（estimated 無し）は実測として読める
    func testEstimatedRowsAndLegacyDecode() throws {
        var est = ApiUsageEntry(provider: "soniox", model: "stt-rt-v5", purpose: "transcription")
        est.requests = 2; est.audioSeconds = 1800; est.billedAudioSeconds = 1800; est.estimated = true
        var real = est
        real.estimated = false
        XCTAssertFalse(est.sameKey(real))
        let dir = makeTempDir()
        let raw = try JSONEncoder().encode(ApiUsageData(days: ["2026-09-20": [est, real]], backfillVersion: 1))
        try raw.write(to: dir.appendingPathComponent("api-usage.json"))
        let store = ApiUsageStore(directory: dir)
        let s = store.allTimeSummary(mode: .list)
        XCTAssertEqual(s.usd, 0.12, accuracy: 1e-9)           // 0.06 + 0.06
        XCTAssertEqual(s.estimatedUSD, 0.06, accuracy: 1e-9)
        XCTAssertEqual(store.breakdown(mode: .list) { _ in true }.count, 2)

        let legacy = Data(#"{"days":{"2026-09-20":[{"provider":"groq","model":"m","purpose":"transcription","requests":1}]}}"#.utf8)
        let decoded = try JSONDecoder().decode(ApiUsageData.self, from: legacy)
        XCTAssertEqual(decoded.backfillVersion, 0)
        XCTAssertEqual(decoded.days["2026-09-20"]?.first?.estimated, false)
    }

    // MARK: - 保存 → 読み込みの往復

    func testSaveLoadRoundTrip() {
        let dir = makeTempDir()
        let store = ApiUsageStore(directory: dir)
        store.recordAudio(provider: .groq, model: "whisper-large-v3-turbo", seconds: 3)
        store.recordTokens(provider: .gemini, model: "gemini-3.5-flash-lite", purpose: .captionTranslation,
                           inputTokens: 2_000, outputTokens: 1_000)
        store.flush()

        let reloaded = ApiUsageStore(directory: dir)
        XCTAssertEqual(reloaded.snapshot, store.snapshot)
        let groq = reloaded.breakdown(mode: .list) { _ in true }.first { $0.provider == "groq" }
        XCTAssertEqual(groq?.billedAudioSeconds, 10, "最低課金秒数を適用した値が保存される")
        XCTAssertEqual(reloaded.allTimeSummary(mode: .list).usd, store.allTimeSummary(mode: .list).usd, accuracy: 1e-12)
    }

    /// Groq の通常応答とストリーミング最終チャンク（x_groq.usage）の両方から usage を読める。本文は記録しない
    func testChatUsageParsing() {
        let store = ApiUsageStore(directory: makeTempDir())
        let plain = Data(#"{"choices":[{"message":{"content":"秘密の本文"}}],"usage":{"prompt_tokens":120,"completion_tokens":30}}"#.utf8)
        XCTAssertTrue(store.recordChatUsage(fromJSON: plain, provider: .groq, model: "openai/gpt-oss-20b", purpose: .formatting))
        let sse = Data(#"{"choices":[],"x_groq":{"usage":{"prompt_tokens":10,"completion_tokens":5}}}"#.utf8)
        XCTAssertTrue(store.recordChatUsage(fromJSON: sse, provider: .groq, model: "openai/gpt-oss-20b", purpose: .formatting))
        let delta = Data(#"{"choices":[{"delta":{"content":"x"}}]}"#.utf8)
        XCTAssertFalse(store.recordChatUsage(fromJSON: delta, provider: .groq, model: "openai/gpt-oss-20b", purpose: .formatting))

        let row = store.breakdown(mode: .list) { _ in true }.first
        XCTAssertEqual(row?.inputTokens, 130)
        XCTAssertEqual(row?.outputTokens, 35)
        XCTAssertEqual(row?.requests, 2)
        store.flush()
        let json = String(data: (try? JSONEncoder().encode(store.snapshot)) ?? Data(), encoding: .utf8) ?? ""
        XCTAssertFalse(json.contains("秘密の本文"), "本文は保存しない")
    }
}
