//
//  LLMModelCatalogTests.swift
//  LLM モデルカタログ（一覧の解析・保存値の移行・モデル別パラメータ・廃止エラー判定・差し替え）の回帰テスト
//
//  すべてオフライン。生成 API・一覧 API とも呼ばない（応答 JSON は手書きの見本）。
//  2026-10-11 に Groq の llama 廃止で整形・翻訳が全滅した件の再発防止。
//

import XCTest
@testable import voicekey

final class LLMModelCatalogTests: XCTestCase {

    private func json(_ object: Any) -> Data {
        try! JSONSerialization.data(withJSONObject: object)
    }

    // MARK: - 一覧の解析と除外

    func testParseGroqModelsExcludesNonChatAndInactive() {
        let data = json(["data": [
            ["id": "qwen/qwen3.8-27b", "active": true],
            ["id": "openai/gpt-oss-120b", "active": true],
            ["id": "whisper-large-v3", "active": true],
            ["id": "canopylabs/orpheus-v1-english", "active": true],
            ["id": "meta-llama/llama-prompt-guard-2-86m", "active": true],
            ["id": "openai/gpt-oss-safeguard-20b", "active": true],
            ["id": "old-model", "active": false],
            ["id": "allam-2-7b"],  // active が無いものは使える扱い
        ]])
        XCTAssertEqual(LLMModelCatalog.parseGroqModels(data),
                       ["allam-2-7b", "openai/gpt-oss-120b", "qwen/qwen3.8-27b"])
    }

    func testParseGroqModelsRejectsBrokenJSON() {
        XCTAssertNil(LLMModelCatalog.parseGroqModels(Data("not json".utf8)))
        XCTAssertNil(LLMModelCatalog.parseGroqModels(json(["models": []])))
    }

    func testParseGeminiModelsKeepsOnlyFlashGenerateContent() {
        let gen = ["generateContent", "countTokens"]
        let data = json(["models": [
            ["name": "models/gemini-3.5-flash-lite", "supportedGenerationMethods": gen],
            ["name": "models/gemini-3.8-flash", "supportedGenerationMethods": gen],
            ["name": "models/gemini-3.5-flash-preview-tts", "supportedGenerationMethods": gen],
            ["name": "models/gemini-3.5-flash-image", "supportedGenerationMethods": gen],
            ["name": "models/gemini-3.5-pro", "supportedGenerationMethods": gen],
            ["name": "models/gemini-embedding-001", "supportedGenerationMethods": ["embedContent"]],
            ["name": "models/gemini-3.5-flash-live", "supportedGenerationMethods": ["bidiGenerateContent"]],
            ["name": "models/gemma-3-27b-it", "supportedGenerationMethods": gen],
        ]])
        XCTAssertEqual(LLMModelCatalog.parseGeminiModels(data),
                       ["gemini-3.5-flash-lite", "gemini-3.8-flash"])
    }

    // MARK: - 保存値の移行

    func testMigratedModelReplacesRetiredGroqModels() {
        let rec = LLMModelCatalog.recommendedGroq
        XCTAssertEqual(LLMModelCatalog.migratedModel("llama-3.3-70b-versatile", provider: .groq, available: nil), rec)
        XCTAssertEqual(LLMModelCatalog.migratedModel("meta-llama/llama-4-scout-17b-16e-instruct", provider: .groq, available: nil), rec)
        XCTAssertEqual(LLMModelCatalog.migratedModel("moonshotai/kimi-k2-instruct", provider: .groq, available: nil), rec)
        // 一覧が無いときは、廃止既知でなければ触らない
        XCTAssertNil(LLMModelCatalog.migratedModel("openai/gpt-oss-20b", provider: .groq, available: nil))
        // 一覧があってそこに無ければ差し替える
        XCTAssertEqual(LLMModelCatalog.migratedModel("some/gone-model", provider: .groq, available: [rec]), rec)
        XCTAssertNil(LLMModelCatalog.migratedModel("openai/gpt-oss-20b", provider: .groq,
                                                   available: [rec, "openai/gpt-oss-20b"]))
        // 空・おすすめ自身・Gemini は対象外
        XCTAssertNil(LLMModelCatalog.migratedModel("", provider: .groq, available: nil))
        XCTAssertNil(LLMModelCatalog.migratedModel(rec, provider: .groq, available: []))
        XCTAssertNil(LLMModelCatalog.migratedModel("gemini-1.5-flash", provider: .gemini, available: ["gemini-3.8-flash"]))
    }

    func testMigrateStoredModelsRunsOnlyOnce() {
        let suite = testDefaultsSuite()
        let defaults = UserDefaults(suiteName: suite)!
        defer { removeTestDefaults(defaults, suite) }

        defaults.set("llama-3.3-70b-versatile", forKey: LLMModelSetting.formatting.defaultsKey)
        defaults.set("llama-3.1-8b-instant", forKey: LLMModelSetting.captionGroq.defaultsKey)
        defaults.set("openai/gpt-oss-120b", forKey: LLMModelSetting.dictationGroq.defaultsKey)
        defaults.set("gemini-2.5-flash", forKey: LLMModelSetting.captionGemini.defaultsKey)

        LLMModelCatalog.migrateStoredModelsIfNeeded(defaults: defaults)

        let rec = LLMModelCatalog.recommendedGroq
        XCTAssertEqual(defaults.string(forKey: LLMModelSetting.formatting.defaultsKey), rec)
        XCTAssertEqual(defaults.string(forKey: LLMModelSetting.captionGroq.defaultsKey), rec)
        XCTAssertEqual(defaults.string(forKey: LLMModelSetting.dictationGroq.defaultsKey), "openai/gpt-oss-120b")
        XCTAssertEqual(defaults.string(forKey: LLMModelSetting.captionGemini.defaultsKey), "gemini-2.5-flash")

        // 2 回目は動かない（フラグで一度きり）
        defaults.set("llama-3.3-70b-versatile", forKey: LLMModelSetting.formatting.defaultsKey)
        LLMModelCatalog.migrateStoredModelsIfNeeded(defaults: defaults)
        XCTAssertEqual(defaults.string(forKey: LLMModelSetting.formatting.defaultsKey), "llama-3.3-70b-versatile")
    }

    func testMigrateStoredModelsUsesCachedList() {
        let suite = testDefaultsSuite()
        let defaults = UserDefaults(suiteName: suite)!
        defer { removeTestDefaults(defaults, suite) }

        let rec = LLMModelCatalog.recommendedGroq
        defaults.set([rec, "openai/gpt-oss-20b"], forKey: "llmModelCache.groq.ids")
        defaults.set("openai/gpt-oss-120b", forKey: LLMModelSetting.dictationGroq.defaultsKey)
        defaults.set("openai/gpt-oss-20b", forKey: LLMModelSetting.formatting.defaultsKey)

        LLMModelCatalog.migrateStoredModelsIfNeeded(defaults: defaults)

        XCTAssertEqual(defaults.string(forKey: LLMModelSetting.dictationGroq.defaultsKey), rec)
        XCTAssertEqual(defaults.string(forKey: LLMModelSetting.formatting.defaultsKey), "openai/gpt-oss-20b")
    }

    // MARK: - モデル別パラメータ（実際に送る JSON で確認）

    func testTextFormatterRequestBodyCarriesModelParameters() throws {
        func encoded(_ model: String) throws -> [String: Any] {
            let body = TextFormatter.requestBody(model: model, prompt: "", text: "テスト")
            let data = try JSONSerialization.data(withJSONObject: body)
            return try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        }
        let oss = try encoded("openai/gpt-oss-20b")
        XCTAssertEqual(oss["reasoning_effort"] as? String, "low")
        XCTAssertEqual(oss["include_reasoning"] as? Bool, false)

        let qwen = try encoded("qwen/qwen3.8-27b")
        XCTAssertEqual(qwen["reasoning_effort"] as? String, "none")
        XCTAssertNil(qwen["include_reasoning"])

        let allam = try encoded("allam-2-7b")
        XCTAssertNil(allam["reasoning_effort"])
        XCTAssertNil(allam["include_reasoning"])
        XCTAssertEqual(allam["model"] as? String, "allam-2-7b")
    }

    func testGroqTranslatorRequestCarriesModelParameters() throws {
        guard #available(macOS 26.0, *) else { throw XCTSkip("GroqTranslator は macOS 26 以降") }
        func body(_ model: String) throws -> [String: Any] {
            let request = try GroqTranslator.makeChatRequest(
                endpointBase: "https://example.invalid/v1", model: model, key: "dummy",
                text: "hello", context: [], stream: true)
            return try XCTUnwrap(JSONSerialization.jsonObject(with: XCTUnwrap(request.httpBody)) as? [String: Any])
        }
        let oss = try body("openai/gpt-oss-120b")
        XCTAssertEqual(oss["reasoning_effort"] as? String, "low")
        XCTAssertEqual(oss["include_reasoning"] as? Bool, false)
        XCTAssertEqual(oss["stream"] as? Bool, true)

        let qwen = try body("qwen/qwen3.8-27b")
        XCTAssertEqual(qwen["reasoning_effort"] as? String, "none")

        let allam = try body("allam-2-7b")
        XCTAssertNil(allam["reasoning_effort"])
    }

    // MARK: - 廃止エラーの判定

    func testIsModelUnavailable() {
        let groqDecommissioned = json(["error": [
            "message": "The model `llama-3.3-70b-versatile` has been decommissioned and is no longer supported.",
            "type": "invalid_request_error", "code": "model_decommissioned"]])
        XCTAssertTrue(LLMModelCatalog.isModelUnavailable(status: 400, data: groqDecommissioned))

        let groqNotFound = json(["error": [
            "message": "The model `x` does not exist or you do not have access to it.",
            "type": "invalid_request_error", "code": "model_not_found"]])
        XCTAssertTrue(LLMModelCatalog.isModelUnavailable(status: 404, data: groqNotFound))

        let geminiNotFound = json(["error": [
            "code": 404, "message": "models/gemini-1.0-pro is not found for API version v1beta",
            "status": "NOT_FOUND"]])
        XCTAssertTrue(LLMModelCatalog.isModelUnavailable(status: 404, data: geminiNotFound))

        // 一般の 400・レート制限・キー不正は差し替えても直らない
        let badRequest = json(["error": ["message": "messages must not be empty",
                                         "type": "invalid_request_error", "code": "invalid_value"]])
        XCTAssertFalse(LLMModelCatalog.isModelUnavailable(status: 400, data: badRequest))
        XCTAssertFalse(LLMModelCatalog.isModelUnavailable(status: 429, data: groqDecommissioned))
        XCTAssertFalse(LLMModelCatalog.isModelUnavailable(status: 401, data: groqNotFound))
        XCTAssertFalse(LLMModelCatalog.isModelUnavailable(status: 404, data: Data("<html>".utf8)))
    }

    // MARK: - 設定 UI の選択肢

    func testOptionsPutRecommendedFirstAndMarkUnavailable() {
        let rec = LLMModelCatalog.recommendedGroq
        let noList = LLMModelCatalog.options(.groq, current: rec, available: nil)
        XCTAssertEqual(noList.first?.id, rec)
        XCTAssertTrue(noList.first?.label.hasSuffix("（おすすめ）") ?? false)
        XCTAssertEqual(noList.map(\.id), LLMModelCatalog.groqModels.map(\.id))

        // 一覧に無い保存値は「（利用不可）」付きで末尾に残す（選択が消えて見えないように）
        let retired = LLMModelCatalog.options(.groq, current: "llama-3.3-70b-versatile", available: nil)
        XCTAssertEqual(retired.last, LLMModelOption(id: "llama-3.3-70b-versatile",
                                                    label: "llama-3.3-70b-versatile（利用不可）"))

        // 一覧があるときは、一覧に無い既知モデルを出さず、一覧にだけあるモデルを足す
        let listed = LLMModelCatalog.options(.groq, current: rec, available: [rec, "openai/gpt-oss-20b", "new/model"])
        XCTAssertEqual(listed.map(\.id), [rec, "openai/gpt-oss-20b", "new/model"])

        // 未知だが一覧にも無い保存値は利用不可、一覧が無いなら判断しない
        let unknown = LLMModelCatalog.options(.groq, current: "custom/x", available: [rec])
        XCTAssertEqual(unknown.last?.label, "custom/x（利用不可）")
        let unknownNoList = LLMModelCatalog.options(.groq, current: "custom/x", available: nil)
        XCTAssertEqual(unknownNoList.last?.label, "custom/x")
    }

    // MARK: - 実行時の差し替え

    func testReplaceUnavailableWritesRecommendedAndNotifiesOnce() {
        let suite = testDefaultsSuite()
        let defaults = UserDefaults(suiteName: suite)!
        let previousHandler = LLMModelCatalog.onModelReplaced
        defer {
            LLMModelCatalog.onModelReplaced = previousHandler
            removeTestDefaults(defaults, suite)
        }

        var received: [LLMModelReplacement] = []
        LLMModelCatalog.onModelReplaced = { received.append($0) }

        // 通知済み集合はプロセス全体で共有なので、テストごとに一意なモデル名を使う
        let failed = "test/retired-\(UUID().uuidString)"
        let first = LLMModelCatalog.replaceUnavailable(.formatting, failedModel: failed, defaults: defaults)
        let second = LLMModelCatalog.replaceUnavailable(.formatting, failedModel: failed, defaults: defaults)

        XCTAssertEqual(first, LLMModelCatalog.recommendedGroq)
        XCTAssertEqual(second, LLMModelCatalog.recommendedGroq)
        XCTAssertEqual(defaults.string(forKey: "formatModel"), LLMModelCatalog.recommendedGroq)
        XCTAssertEqual(received.count, 1)
        XCTAssertEqual(received.first?.noticeText,
                       "モデル \(failed) は提供終了のため \(LLMModelCatalog.recommendedGroq) に切り替えました")

        // Gemini の設定は Gemini のおすすめへ
        let gemini = LLMModelCatalog.replaceUnavailable(.captionGemini, failedModel: failed, defaults: defaults)
        XCTAssertEqual(gemini, LLMModelCatalog.recommendedGemini)
        XCTAssertEqual(defaults.string(forKey: LLMModelSetting.captionGemini.defaultsKey), LLMModelCatalog.recommendedGemini)

        // おすすめ自身が失敗したら差し替え先は無い
        XCTAssertNil(LLMModelCatalog.replaceUnavailable(.captionGroq, failedModel: LLMModelCatalog.recommendedGroq,
                                                        defaults: defaults))
    }

    // MARK: - 利用不可の判定（取得一覧より静的な廃止リストを優先しない）

    // Gemini の取得一覧は flash 系に絞っているので、一覧に無い pro 系でも利用不可にしない
    func testGeminiNotInFetchedListIsStillAvailable() {
        XCTAssertFalse(LLMModelCatalog.isUnavailable("gemini-3.8-pro", provider: .gemini, available: ["gemini-3.8-flash"]))
        XCTAssertFalse(LLMModelCatalog.isUnavailable("gemini-3.8-pro", provider: .gemini, available: nil))
    }

    // Groq は取得一覧にあれば、静的な廃止リスト（llama 系）に当たっても利用可
    func testGroqInFetchedListIsAvailableEvenIfKnownRetired() {
        let llama = "llama-3.3-70b-versatile"
        XCTAssertFalse(LLMModelCatalog.isUnavailable(llama, provider: .groq, available: [llama]))
        XCTAssertNil(LLMModelCatalog.migratedModel(llama, provider: .groq, available: [llama]))
        // 一覧が無い・一覧に無いときは従来どおり廃止扱い
        XCTAssertTrue(LLMModelCatalog.isUnavailable(llama, provider: .groq, available: nil))
        XCTAssertTrue(LLMModelCatalog.isUnavailable(llama, provider: .groq, available: ["openai/gpt-oss-20b"]))
        XCTAssertTrue(LLMModelCatalog.isUnavailable("some/gone-model", provider: .groq, available: ["openai/gpt-oss-20b"]))
    }

    // MARK: - 出力の長さ

    // 入力が長いほど出力上限を広げ、天井で止める
    func testMaxCompletionTokensScalesWithInput() {
        XCTAssertEqual(LLMModelCatalog.maxCompletionTokens(inputCharacters: 0), 512)
        XCTAssertEqual(LLMModelCatalog.maxCompletionTokens(inputCharacters: 100), 812)
        XCTAssertEqual(LLMModelCatalog.maxCompletionTokens(inputCharacters: 100_000), LLMModelCatalog.maxCompletionTokensCeiling)
    }

    // 整形のリクエストも入力長に応じた上限を送る
    func testTextFormatterRequestBodyCarriesMaxTokens() {
        let text = String(repeating: "あ", count: 1000)
        let body = TextFormatter.requestBody(model: "openai/gpt-oss-20b", prompt: "", text: text)
        XCTAssertEqual(body["max_completion_tokens"] as? Int, 3512)
    }

    // finish_reason が length なら途中で切れた扱い
    func testIsTruncated() {
        XCTAssertTrue(LLMModelCatalog.isTruncated(responseJSON: json(
            ["choices": [["message": ["content": "途中"], "finish_reason": "length"]]])))
        XCTAssertFalse(LLMModelCatalog.isTruncated(responseJSON: json(
            ["choices": [["message": ["content": "全部"], "finish_reason": "stop"]]])))
        XCTAssertFalse(LLMModelCatalog.isTruncated(responseJSON: Data("not json".utf8)))
    }

    // 音声入力の翻訳（Groq）も入力長に応じた上限を送り、切れた訳は失敗扱い（＝原文を入力）にする
    func testDictationGroqTranslationBodyAndTruncation() throws {
        guard #available(macOS 26.0, *) else { throw XCTSkip("翻訳は macOS 26 以降") }
        let text = String(repeating: "あ", count: 200)
        let body = DictationTranslator.groqRequestBody(model: "openai/gpt-oss-20b", systemPrompt: "訳して", text: text)
        XCTAssertEqual(body["max_completion_tokens"] as? Int, 1112)
        XCTAssertEqual(body["reasoning_effort"] as? String, "low")
        XCTAssertEqual((body["messages"] as? [[String: String]])?.last?["content"], text)

        let truncated = json(["choices": [["message": ["content": "Half"], "finish_reason": "length"]]])
        XCTAssertThrowsError(try DictationTranslator.groqTranslatedText(from: truncated))
        let complete = json(["choices": [["message": ["content": " Done "], "finish_reason": "stop"]]])
        XCTAssertEqual(try DictationTranslator.groqTranslatedText(from: complete), "Done")
    }
}
