//
//  CaptionGroqParseTests.swift
//  クラウド翻訳（Groq / OpenAI 互換）の解析ロジックとストリーミング経路の回帰テスト
//
//  廃止した subglass の `tests/groq_parse/main.swift` からの移植（2026-08-15）。
//  ネットワークにも API キーにも触らないので課金ゼロ・承認ダイアログなしで回せる。
//  実キーが無い状態でもクラウド経路を壊していないことを確かめられる唯一の手段なので、
//  Caption/Translation まわりを触ったら必ず通す。
//
//  検証するもの:
//    1. SSE の差分抽出（`data:` 行・`[DONE]`・空行）
//    2. 非ストリーミング応答の本文取り出し（ベンチ経路）
//    3. HTTP ステータス → TranslationError の写像
//    4. chat/completions のリクエスト組み立て（文脈・認証・stream 指定）
//    5. ベンチ用の system プロンプト差し替え
//    6. TranslationCoordinator のストリーミング経路（途中経過は累積・確定訳は 1 回）
//    7. FallbackTranslator（クラウド失敗時に Apple 相当へ落ちる／両方失敗なら元の失敗を投げる）
//

import XCTest
import os

@testable import voicekey

/// 途中経過を 3 回配ってから全文を返すスタブ（コーディネータの流し込みだけを見る）
@available(macOS 26.0, *)
private struct FakeStreamingTranslator: StreamingTranslator {
    func translate(_ text: String, context: [TranslationExchange]) async throws -> String {
        "（非ストリーミング経路）"
    }

    func translateStreaming(
        _ text: String, context: [TranslationExchange], onDelta: @escaping @Sendable (String) -> Void
    ) async throws -> String {
        for piece in ["これは", "ストリーミング", "です。"] { onDelta(piece) }
        return "これはストリーミングです。"
    }
}

/// 必ず失敗する翻訳器（フォールバックの発火条件を作る）
@available(macOS 26.0, *)
private struct FailingTranslator: StreamingTranslator {
    let error: TranslationError

    func translate(_ text: String, context: [TranslationExchange]) async throws -> String { throw error }

    func translateStreaming(
        _ text: String, context: [TranslationExchange], onDelta: @escaping @Sendable (String) -> Void
    ) async throws -> String {
        // 途中まで配ってから落ちる形（実際の SSE 断でも起きる）を再現する
        onDelta("途中まで")
        throw error
    }
}

/// 常に同じ訳を返す落とし先（Apple 翻訳の代役）
@available(macOS 26.0, *)
private struct FixedTranslator: Translator {
    let value: String

    func translate(_ text: String, context: [TranslationExchange]) async throws -> String { value }
}

final class CaptionGroqParseTests: XCTestCase {

    // MARK: - 1. SSE の差分抽出

    /// 実際に Groq / OpenAI が返す形の SSE から本文だけが積み上がること
    func testSSEDeltasAccumulate() throws {
        guard #available(macOS 26.0, *) else { throw XCTSkip("ライブ字幕は macOS 26 以降") }

        let sseLines = [
            "data: {\"id\":\"chatcmpl-1\",\"choices\":[{\"index\":0,\"delta\":{\"role\":\"assistant\",\"content\":\"\"}}]}",
            "",
            "data: {\"id\":\"chatcmpl-1\",\"choices\":[{\"index\":0,\"delta\":{\"content\":\"ここに\"}}]}",
            "data: {\"id\":\"chatcmpl-1\",\"choices\":[{\"index\":0,\"delta\":{\"content\":\"入れて、\"}}]}",
            "data: {\"id\":\"chatcmpl-1\",\"choices\":[{\"index\":0,\"delta\":{\"content\":\"溶鉱炉を作ろう。\"}}]}",
            "data: {\"id\":\"chatcmpl-1\",\"choices\":[{\"index\":0,\"delta\":{},\"finish_reason\":\"stop\"}]}",
            "data: [DONE]",
        ]
        var accumulated = ""
        var deltaCount = 0
        for line in sseLines {
            guard let piece = GroqTranslator.deltaText(fromSSELine: line), !piece.isEmpty else { continue }
            accumulated += piece
            deltaCount += 1
        }

        XCTAssertEqual(accumulated, "ここに入れて、溶鉱炉を作ろう。", "SSE 差分の積み上げ")
        // 空文字の delta と finish_reason だけの行は本文として数えない
        XCTAssertEqual(deltaCount, 3, "SSE 差分の回数")
    }

    /// 終端マーカーと空行を本文にしないこと
    func testSSETerminatorAndBlankLineAreIgnored() throws {
        guard #available(macOS 26.0, *) else { throw XCTSkip("ライブ字幕は macOS 26 以降") }

        XCTAssertNil(GroqTranslator.deltaText(fromSSELine: "data: [DONE]"), "[DONE] を本文にしない")
        XCTAssertNil(GroqTranslator.deltaText(fromSSELine: ""), "空行を無視")
    }

    // MARK: - 2. 非ストリーミング応答の本文取り出し

    /// ベンチ経路で使う非ストリーミング応答から本文が取れて、前後の空白が落ちること
    func testExtractTextTrimsBody() throws {
        guard #available(macOS 26.0, *) else { throw XCTSkip("ライブ字幕は macOS 26 以降") }

        let body = """
            {"choices":[{"message":{"role":"assistant","content":" 溶鉱炉を作るためにここに入れて。 "}}],\
            "usage":{"prompt_tokens":123,"completion_tokens":45}}
            """
        XCTAssertEqual(
            try GroqTranslator.extractText(from: Data(body.utf8)),
            "溶鉱炉を作るためにここに入れて。",
            "応答本文の取り出しと trim"
        )
    }

    // MARK: - 3. エラー写像

    /// 429 は retry-after 秒つきの rateLimited になること
    func testMapErrorRateLimited() throws {
        guard #available(macOS 26.0, *) else { throw XCTSkip("ライブ字幕は macOS 26 以降") }

        let mapped = GroqTranslator.mapError(
            status: 429, http: makeResponse(429, headers: ["retry-after": "3"]), data: errorBody)
        guard case let .rateLimited(retryAfter) = mapped else {
            return XCTFail("429 → rateLimited ではない: \(mapped)")
        }
        XCTAssertEqual(retryAfter, 3.0, "429 → rateLimited(retry-after)")
    }

    /// 401 は unauthorized になること
    func testMapErrorUnauthorized() throws {
        guard #available(macOS 26.0, *) else { throw XCTSkip("ライブ字幕は macOS 26 以降") }

        let mapped = GroqTranslator.mapError(status: 401, http: makeResponse(401), data: errorBody)
        guard case .unauthorized = mapped else {
            return XCTFail("401 → unauthorized ではない: \(mapped)")
        }
    }

    /// 400 は badRequest になり、本文からメッセージが抜き出されること
    func testMapErrorBadRequestExtractsMessage() throws {
        guard #available(macOS 26.0, *) else { throw XCTSkip("ライブ字幕は macOS 26 以降") }

        let mapped = GroqTranslator.mapError(status: 400, http: makeResponse(400), data: errorBody)
        guard case let .badRequest(message) = mapped else {
            return XCTFail("400 → badRequest ではない: \(mapped)")
        }
        XCTAssertEqual(message, "Rate limit reached", "400 → badRequest(メッセージ抽出)")
    }

    // MARK: - 4. リクエスト組み立て

    /// 本番経路のリクエスト（文脈つき・ストリーミング指定・認証ヘッダ）が正しく組めること
    func testMakeChatRequestForProduction() throws {
        guard #available(macOS 26.0, *) else { throw XCTSkip("ライブ字幕は macOS 26 以降") }

        let request = try GroqTranslator.makeChatRequest(
            endpointBase: GroqTranslator.endpointBase,
            model: "llama-3.3-70b-versatile",
            key: "dummy-key",
            text: "You have cold, bro?",
            context: [
                TranslationExchange(
                    source: "Bro, there's no more ingots in there.",
                    translated: "兄弟、もうインゴットがないよ。")
            ],
            stream: true
        )

        XCTAssertEqual(
            request.url?.absoluteString,
            "https://api.groq.com/openai/v1/chat/completions",
            "エンドポイント")
        XCTAssertEqual(
            request.value(forHTTPHeaderField: "Authorization"), "Bearer dummy-key", "認証ヘッダ")

        let json = try body(of: request)
        let messages = try XCTUnwrap(json["messages"] as? [[String: String]])
        XCTAssertEqual(json["stream"] as? Bool, true, "stream=true")
        XCTAssertEqual(json["model"] as? String, "llama-3.3-70b-versatile", "モデル ID")
        XCTAssertEqual(messages.count, 4, "メッセージ数（system + 文脈2 + 本文）")
        // ベンチ用のプロンプトを本番に混ぜ込んでいないこと
        XCTAssertEqual(messages[0]["content"], GeminiTranslator.systemPrompt, "system は実運用のプロンプト")
        XCTAssertEqual(messages[1]["role"], "user", "文脈は user/assistant 交互（原文側）")
        XCTAssertEqual(messages[2]["role"], "assistant", "文脈は user/assistant 交互（訳文側）")
        XCTAssertEqual(messages[3]["content"], "You have cold, bro?", "最後が今回の原文")
    }

    // MARK: - 5. ベンチ用の差し替え

    /// system プロンプトの差し替えと、OpenAI 互換の別ホストが効くこと
    func testMakeChatRequestForBenchmark() throws {
        guard #available(macOS 26.0, *) else { throw XCTSkip("ライブ字幕は macOS 26 以降") }

        let request = try GroqTranslator.makeChatRequest(
            endpointBase: "https://api.openai.com/v1", model: "gpt-4.1-mini", key: "k",
            text: "hi", context: [], stream: false, systemPrompt: "BENCH-PROMPT"
        )

        let json = try body(of: request)
        let messages = try XCTUnwrap(json["messages"] as? [[String: String]])
        XCTAssertEqual(messages[0]["content"], "BENCH-PROMPT", "system の差し替え")
        // `stream: false` を送ると弾く実装があるため、キーごと出さないのが正
        XCTAssertNil(json["stream"], "stream 未指定時はキーごと出さない")
        XCTAssertEqual(
            request.url?.absoluteString,
            "https://api.openai.com/v1/chat/completions",
            "OpenAI 互換の別ホスト")
    }

    // MARK: - 6. コーディネータのストリーミング経路

    /// 途中経過は累積で届き、確定訳は 1 回だけ積まれること
    func testCoordinatorStreamsAccumulatedPartialsAndFinalizesOnce() throws {
        guard #available(macOS 26.0, *) else { throw XCTSkip("ライブ字幕は macOS 26 以降") }

        let deltas = OSAllocatedUnfairLock(initialState: [String]())
        let finals = OSAllocatedUnfairLock(initialState: [String]())
        let done = expectation(description: "確定訳")

        let coordinator = TranslationCoordinator(
            translatorProvider: { FakeStreamingTranslator() },
            sequenceProvider: { 1 }
        )
        coordinator.onStreamingDelta = { _, soFar, _ in deltas.withLock { $0.append(soFar) } }
        coordinator.onTranslated = { _, japanese, _ in
            finals.withLock { $0.append(japanese) }
            done.fulfill()
        }
        // 48 字以内かつ文末記号で終わるので 1 区切りのまま流れる（刻まれない長さを選んでいる）
        coordinator.submit("Put it in here, put the blast furniture.")
        wait(for: [done], timeout: 5)

        XCTAssertEqual(
            deltas.withLock { $0 },
            ["これは", "これはストリーミング", "これはストリーミングです。"],
            "途中経過が累積で届く")
        XCTAssertEqual(finals.withLock { $0 }, ["これはストリーミングです。"], "確定訳は 1 回だけ")
    }

    // MARK: - 7. フォールバック

    /// クラウドが 429 で落ちても、通常経路・ストリーミング経路の両方で落とし先の訳が返ること
    func testFallbackReturnsSecondaryTranslationOnRateLimit() async throws {
        guard #available(macOS 26.0, *) else { throw XCTSkip("ライブ字幕は macOS 26 以降") }

        let rateLimited = FallbackTranslator(
            primary: FailingTranslator(error: .rateLimited(retryAfter: 30)),
            fallback: FixedTranslator(value: "代替訳")
        )
        let viaTranslate = try await rateLimited.translate("x", context: [])
        let viaStreaming = try await rateLimited.translateStreaming("x", context: [], onDelta: { _ in })

        XCTAssertEqual([viaTranslate, viaStreaming], ["代替訳", "代替訳"], "429 で落とし先の訳を返す")
    }

    /// 落とし先も失敗したら、原因が消えないよう元の失敗を投げ直すこと
    func testFallbackRethrowsOriginalErrorWhenBothFail() async throws {
        guard #available(macOS 26.0, *) else { throw XCTSkip("ライブ字幕は macOS 26 以降") }

        let bothFail = FallbackTranslator(
            primary: FailingTranslator(error: .unauthorized),
            fallback: FailingTranslator(error: .network("落とし先も不通"))
        )
        do {
            _ = try await bothFail.translate("x", context: [])
            XCTFail("両方失敗なら投げるはず")
        } catch let error as TranslationError {
            guard case .unauthorized = error else {
                return XCTFail("元の失敗（unauthorized）ではない: \(error)")
            }
        }
    }

    // MARK: - 補助

    /// エラー写像で使う典型的なエラー応答
    private let errorBody = Data(#"{"error":{"message":"Rate limit reached"}}"#.utf8)

    /// 指定ステータスの HTTP レスポンスを作る
    private func makeResponse(_ status: Int, headers: [String: String] = [:]) -> HTTPURLResponse {
        HTTPURLResponse(
            url: URL(string: "https://api.groq.com/openai/v1/chat/completions")!,
            statusCode: status,
            httpVersion: nil,
            headerFields: headers
        )!
    }

    /// リクエストボディを JSON として読む
    private func body(of request: URLRequest) throws -> [String: Any] {
        let data = try XCTUnwrap(request.httpBody)
        return try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
    }
}
