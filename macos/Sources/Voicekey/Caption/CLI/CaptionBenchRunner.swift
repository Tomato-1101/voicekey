/// `--bench-translators` / `--groq-model-test` モード（翻訳エンジンの一括ベンチ）
///
/// 実運用と同じ文（今夜の実ログから採った固定 10 文。聞き間違いを含む）を各エンジンへ
/// **1 文につき 1 回だけ**投げ、訳文・遅延・トークン・概算費用を並べて出す。
/// どのエンジンを常用するかを、印象ではなく実測で決めるための道具。
///
/// **消費の上限（厳守。2026-08-10 ユーザー指示）**
///   - 各エンジン各文 1 回。自動リトライは 1 回まで。ループしない。
///   - 総リクエスト 80 件まで。うち Gemini 系は 35 件まで。
///   - 手動実行のみ（cron・launchd に入れない）。
///   - キーが無いエンジンは SKIP と明示して、後から同じスクリプトで追試できるようにする。
///
/// 価格は 2026-08-10 に一次資料（console.groq.com/docs/models・ai.google.dev の pricing・
/// developers.openai.com の pricing）で確認した USD / 100万トークン。
/// 円換算は 1 ドル = 158 円（同日の実勢 157.7 円を丸めたもの）。
import Foundation

/// 翻訳エンジンのベンチ
@available(macOS 26.0, *)
enum CaptionBenchRunner {

    // MARK: - 設定

    /// 円換算レート（概算。実勢 157.7 円 / ドル・2026-08-10）
    static let usdToJPY = 158.0

    /// 総リクエスト上限
    private static let totalRequestLimit = 80
    /// Gemini 系のリクエスト上限
    private static let geminiRequestLimit = 35

    /// ベンチに使う固定 10 文
    ///
    /// 今夜の実ログ（`log show --predicate 'subsystem == "com.voicekey.app"'` の「英文（確定）」）から採った実物。
    /// 音声認識の聞き間違い（blast furnace → "blast furniture" / coal → "cold" /
    /// iron ingots → "iron engines" "iron innings" / chat → "Chez"）を 7 文、
    /// 素直な文を 3 文入れてある。**質の判定はこの聞き間違いを直せるかで行う**。
    static let sentences: [String] = [
        "Put it in here, put the blast furniture, to make a blast furniture.",
        "You have cold, bro?",
        "No, put the put the coal in this cobblestone on the in the furnished cobblestone.",
        "I have I have more than I have more iron engines than you can even imagine.",
        "More iron innings than you could even imagine.",
        "And it's hard to find cold in the deep parts of the cave.",
        "Chez, should I make diamond boots or should I start making diamond, uh, a pickaxe?",
        "Bro, there's no more ingots in there.",
        "All right, Kyle, I'll make my way towards you, okay?",
        "Brother, well, my coordinates are -231, -31, -one.",
    ]

    /// モデル選定の小テストに使う 3 文（聞き間違いだけ）
    private static let modelTestSentences: [String] = Array(sentences.prefix(3))

    /// ベンチ用のシステムプロンプト
    ///
    /// 実運用のプロンプトに「何の配信か」を足す。字幕の質は文脈で決まるので、
    /// 素の翻訳力ではなく**実際の使い方に近い条件**で比べる。
    static let benchSystemPrompt = GeminiTranslator.systemPrompt + """

        この音声は Minecraft（マインクラフト）のマルチプレイ実況配信です。\
        ゲーム用語（blast furnace＝溶鉱炉、furnace＝かまど、coal＝石炭、iron ingot＝鉄インゴット、\
        cobblestone＝丸石、pickaxe＝ツルハシ、chat＝視聴者への呼びかけ）を踏まえ、\
        聞き間違いと思われる語（furniture / cold / engines / innings など）は文脈から正しい語に直して訳してください。
        """

    /// 比較するエンジン
    ///
    /// Groq のモデルは設定値（既定 `llama-3.3-70b-versatile`）。
    /// OpenAI は「安価・低遅延」で `gpt-4.1-mini` を選んだ（gpt-5 系は推論トークンが乗って
    /// 遅延が読めないため、字幕用途の比較には非推論モデルを使う）。
    private static func engines() -> [Engine] {
        [
            Engine(label: "apple", kind: .apple, modelID: "(オンデバイス)", priceIn: 0, priceOut: 0),
            Engine(
                label: "groq",
                kind: .openAICompatible(base: GroqTranslator.endpointBase, provider: .groq),
                modelID: CaptionSettings.groqModelID,
                priceIn: 0.59, priceOut: 0.79
            ),
            Engine(label: "gemini-lite", kind: .gemini, modelID: "gemini-3.5-flash-lite", priceIn: 0.30, priceOut: 2.50),
            Engine(label: "gemini-flash", kind: .gemini, modelID: "gemini-3.5-flash", priceIn: 1.50, priceOut: 9.00),
            Engine(label: "gemini-3.6", kind: .gemini, modelID: "gemini-3.6-flash", priceIn: 1.50, priceOut: 7.50),
            Engine(
                label: "openai",
                kind: .openAICompatible(base: "https://api.openai.com/v1", provider: .openai),
                modelID: "gpt-4.1-mini",
                priceIn: 0.40, priceOut: 1.60
            ),
        ]
    }

    // MARK: - 実行

    /// 一括ベンチを実行する
    ///
    /// - Parameter logFilePath: ログの追加出力先（nil なら標準出力のみ）
    /// - Returns: プロセスの終了コード（0=正常 / 1=1 件も実行できなかった）
    static func run(logFilePath: String?) async -> Int32 {
        let writer = CaptionTestLogWriter(path: logFilePath)
        defer { writer.close() }

        let budget = RequestBudget(total: totalRequestLimit, gemini: geminiRequestLimit)
        writer.write("[BENCH] 開始 sentences=\(sentences.count) 上限=総\(totalRequestLimit)件/Gemini\(geminiRequestLimit)件 rate=\(usdToJPY)円/ドル")

        var summaries: [EngineSummary] = []
        var outputs: [String: [String]] = [:]

        for engine in engines() {
            // キーが無いエンジンは 1 リクエストも使わずに飛ばす
            if let provider = engine.provider, APIKeyStore.load(provider) == nil {
                writer.write("[ENGINE] \(engine.label) status=skip reason=\(provider.rawValue) が未設定 model=\(engine.modelID)")
                summaries.append(EngineSummary(engine: engine, skipped: "APIキー未設定(\(provider.rawValue))"))
                continue
            }
            // Apple は言語モデルが導入済みのときだけ（未導入だとダウンロード承認 UI で止まる）
            var appleTranslator: AppleTranslator?
            if case .apple = engine.kind {
                let translator = AppleTranslator()
                guard await translator.prepareIfInstalled() else {
                    writer.write("[ENGINE] apple status=skip reason=英→日の言語モデルが未導入")
                    summaries.append(EngineSummary(engine: engine, skipped: "言語モデル未導入"))
                    continue
                }
                appleTranslator = translator
            }

            writer.write("[ENGINE] \(engine.label) status=run model=\(engine.modelID)")
            var summary = EngineSummary(engine: engine, skipped: nil)
            var texts: [String] = []

            for (index, sentence) in sentences.enumerated() {
                guard !engine.isMetered || budget.canSpend(isGemini: engine.isGemini) else {
                    writer.write("[LIMIT] リクエスト上限に達したため打ち切りました engine=\(engine.label) 使用=\(budget.used)")
                    break
                }
                let started = CFAbsoluteTimeGetCurrent()
                do {
                    let result = try await translateOnce(
                        engine: engine, apple: appleTranslator, text: sentence, budget: budget
                    )
                    let elapsed = (CFAbsoluteTimeGetCurrent() - started) * 1000
                    summary.latencies.append(elapsed)
                    summary.inputTokens += result.usage.input
                    summary.outputTokens += result.usage.output
                    texts.append(result.text)
                    writer.write(
                        String(format: "[RESULT] engine=%@ idx=%02d ms=%.0f in=%d out=%d ja=%@",
                               engine.label, index + 1, elapsed, result.usage.input, result.usage.output, result.text)
                    )
                } catch {
                    summary.failures += 1
                    texts.append("(失敗)")
                    let detail = (error as? TranslationError)?.description ?? String(describing: error)
                    writer.write("[RESULT] engine=\(engine.label) idx=\(index + 1) status=fail reason=\(detail)")
                }
            }

            outputs[engine.label] = texts
            summaries.append(summary)
            writer.write(summary.line())
        }

        // 文ごとに全エンジンの訳を並べる（質の比較はここを目で見て判断する）
        writer.write("[COMPARE] 文ごとの訳文比較")
        for (index, sentence) in sentences.enumerated() {
            writer.write(String(format: "[SRC] %02d %@", index + 1, sentence))
            for summary in summaries where summary.skipped == nil {
                let text = outputs[summary.engine.label]?.indices.contains(index) == true
                    ? outputs[summary.engine.label]![index] : "(未実行)"
                writer.write(String(format: "[JA ] %02d %-12@ %@", index + 1, summary.engine.label, text))
            }
        }

        let totalJPY = summaries.reduce(0.0) { $0 + $1.costJPY }
        writer.write(String(format: "[COST] APIリクエスト=%d/%d（うちGemini=%d/%d） 概算費用=%.2f円",
                            budget.used, totalRequestLimit, budget.geminiUsed, geminiRequestLimit, totalJPY))
        for summary in summaries {
            writer.write(summary.line())
        }
        let executed = summaries.contains { $0.skipped == nil && !$0.latencies.isEmpty }
        writer.write("[DONE] status=\(executed ? "ok" : "no-engine")")
        return executed ? 0 : 1
    }

    /// Groq のモデル選定テスト（`GET /models` ＋ 3 文の小テスト）
    ///
    /// 候補は production ラインナップのうち日本語の質が見込めるもの。
    /// 消費は「候補数 × 3 文」だけ（一覧取得はトークンを使わない）。
    ///
    /// - Parameter logFilePath: ログの追加出力先
    /// - Returns: 終了コード（0=正常 / 1=キー未設定などで実行できず）
    static func runGroqModelTest(logFilePath: String?) async -> Int32 {
        let writer = CaptionTestLogWriter(path: logFilePath)
        defer { writer.close() }

        guard let found = APIKeyStore.load(.groq) else {
            writer.write("[GROQ-MODELS] status=skip reason=GROQ_API_KEY が未設定")
            writer.write("[DONE] status=skip")
            return 1
        }
        writer.write("[GROQ-MODELS] キー取得元=\(found.source.displayName)")

        let available: [String]
        do {
            available = try await GroqTranslator.listModels(key: found.key)
        } catch {
            writer.write("[GROQ-MODELS] status=error reason=\(String(describing: error))")
            writer.write("[DONE] status=error")
            return 1
        }
        writer.write("[GROQ-MODELS] 一覧=\(available.joined(separator: ", "))")

        // 候補（存在するものだけ試す）。1 候補につき 3 文なので最大 9 リクエスト。
        let candidates = ["llama-3.3-70b-versatile", "openai/gpt-oss-120b", "openai/gpt-oss-20b"]
            .filter { available.contains($0) }
        writer.write("[GROQ-MODELS] 候補=\(candidates.joined(separator: ", "))")

        let budget = RequestBudget(total: candidates.count * modelTestSentences.count, gemini: 0)
        for model in candidates {
            let engine = Engine(
                label: "groq:\(model)",
                kind: .openAICompatible(base: GroqTranslator.endpointBase, provider: .groq),
                modelID: model, priceIn: 0, priceOut: 0
            )
            for (index, sentence) in modelTestSentences.enumerated() {
                guard budget.canSpend(isGemini: false) else { break }
                let started = CFAbsoluteTimeGetCurrent()
                do {
                    let result = try await translateOnce(engine: engine, apple: nil, text: sentence, budget: budget)
                    let elapsed = (CFAbsoluteTimeGetCurrent() - started) * 1000
                    writer.write(String(format: "[GROQ-TRY] model=%@ idx=%d ms=%.0f ja=%@", model, index + 1, elapsed, result.text))
                } catch {
                    writer.write("[GROQ-TRY] model=\(model) idx=\(index + 1) status=fail reason=\(String(describing: error))")
                }
            }
        }
        writer.write("[GROQ-MODELS] 使用リクエスト=\(budget.used)")
        writer.write("[DONE] status=ok")
        return 0
    }

    // MARK: - 1 回分の翻訳

    /// 1 文を 1 エンジンで訳す（失敗したら 1 回だけ再試行する）
    private static func translateOnce(
        engine: Engine, apple: AppleTranslator?, text: String, budget: RequestBudget
    ) async throws -> (text: String, usage: TokenUsage) {
        do {
            // 上限を数えるのは課金対象（クラウド）だけ。Apple はオンデバイスで無料なので数えない。
            if engine.isMetered { budget.spend(isGemini: engine.isGemini) }
            return try await perform(engine: engine, apple: apple, text: text)
        } catch {
            // 再試行は 1 回だけ（レート制限・瞬断・サーバエラーのときのみ）
            guard shouldRetry(error), !engine.isMetered || budget.canSpend(isGemini: engine.isGemini) else { throw error }
            try? await Task.sleep(for: .seconds(1))
            if engine.isMetered { budget.spend(isGemini: engine.isGemini) }
            return try await perform(engine: engine, apple: apple, text: text)
        }
    }

    /// 一時的な失敗か（恒久的な失敗では再試行しない）
    private static func shouldRetry(_ error: Error) -> Bool {
        guard let error = error as? TranslationError else { return false }
        switch error {
        case .network, .rateLimited, .serverError: return true
        default: return false
        }
    }

    /// エンジン別の実処理
    private static func perform(
        engine: Engine, apple: AppleTranslator?, text: String
    ) async throws -> (text: String, usage: TokenUsage) {
        switch engine.kind {
        case .apple:
            guard let apple else { throw TranslationError.translationUnavailable("翻訳器が用意されていません") }
            // Apple の API は文脈もプロンプトも取れない（＝素の 1 文翻訳）。これも比較対象の性質。
            return (try await apple.translate(text, context: []), TokenUsage())
        case let .openAICompatible(base, provider):
            guard let key = APIKeyStore.load(provider)?.key else { throw TranslationError.missingAPIKey }
            return try await callOpenAICompatible(base: base, model: engine.modelID, key: key, text: text)
        case .gemini:
            guard let key = APIKeyStore.load(.gemini)?.key else { throw TranslationError.missingAPIKey }
            return try await callGemini(model: engine.modelID, key: key, text: text)
        }
    }

    /// OpenAI 互換 API（Groq / OpenAI）を 1 回叩く（非ストリーミング。usage を取るため）
    private static func callOpenAICompatible(
        base: String, model: String, key: String, text: String
    ) async throws -> (text: String, usage: TokenUsage) {
        let request = try GroqTranslator.makeChatRequest(
            endpointBase: base, model: model, key: key, text: text,
            context: [], stream: false, systemPrompt: benchSystemPrompt
        )
        let (data, http) = try await send(request)
        guard http.statusCode == 200 else {
            throw GroqTranslator.mapError(status: http.statusCode, http: http, data: data)
        }
        let translated = try GroqTranslator.extractText(from: data)
        var usage = TokenUsage()
        if let root = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
           let block = root["usage"] as? [String: Any] {
            usage.input = block["prompt_tokens"] as? Int ?? 0
            usage.output = block["completion_tokens"] as? Int ?? 0
        }
        return (translated, usage)
    }

    /// Gemini API を 1 回叩く（usageMetadata から実トークンを取る）
    private static func callGemini(
        model: String, key: String, text: String
    ) async throws -> (text: String, usage: TokenUsage) {
        guard let url = URL(string: "https://generativelanguage.googleapis.com/v1beta/models/\(model):generateContent") else {
            throw TranslationError.badRequest(message: "モデル ID が不正です: \(model)")
        }
        let body: [String: Any] = [
            "systemInstruction": ["parts": [["text": benchSystemPrompt]]],
            "contents": [["role": "user", "parts": [["text": text]]]],
            "generationConfig": ["maxOutputTokens": 512],
        ]
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue(key, forHTTPHeaderField: "x-goog-api-key")
        request.setValue("application/json", forHTTPHeaderField: "content-type")
        request.httpBody = try JSONSerialization.data(withJSONObject: body)

        let (data, http) = try await send(request)
        guard http.statusCode == 200 else {
            throw GroqTranslator.mapError(status: http.statusCode, http: http, data: data)
        }
        guard let root = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let candidates = root["candidates"] as? [[String: Any]],
              let first = candidates.first else {
            throw TranslationError.badResponse("candidates がありません")
        }
        let parts = (first["content"] as? [String: Any])?["parts"] as? [[String: Any]] ?? []
        let translated = parts.compactMap { $0["text"] as? String }.joined()
            .trimmingCharacters(in: .whitespacesAndNewlines)
        guard !translated.isEmpty else {
            throw TranslationError.badResponse("テキストが空です(finishReason=\(first["finishReason"] as? String ?? "不明"))")
        }
        var usage = TokenUsage()
        if let block = root["usageMetadata"] as? [String: Any] {
            let prompt = block["promptTokenCount"] as? Int ?? 0
            let total = block["totalTokenCount"] as? Int ?? 0
            usage.input = prompt
            // thinking 分も課金対象なので、出力は「合計 − 入力」で取る（candidatesTokenCount だと漏れる）
            usage.output = max(total - prompt, block["candidatesTokenCount"] as? Int ?? 0)
        }
        return (translated, usage)
    }

    /// HTTP を 1 回投げる（ベンチなので待ち時間は長めに取る）
    private static func send(_ request: URLRequest) async throws -> (Data, HTTPURLResponse) {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.timeoutIntervalForRequest = 30
        configuration.waitsForConnectivity = false
        let session = URLSession(configuration: configuration)
        do {
            let (data, response) = try await session.data(for: request)
            guard let http = response as? HTTPURLResponse else {
                throw TranslationError.badResponse("HTTP レスポンスではありません")
            }
            return (data, http)
        } catch let error as TranslationError {
            throw error
        } catch {
            throw TranslationError.network((error as NSError).localizedDescription)
        }
    }

    // MARK: - 補助の型

    /// 使ったトークン
    struct TokenUsage {
        var input = 0
        var output = 0
    }

    /// 比較対象のエンジン 1 つ
    private struct Engine {
        enum Kind {
            case apple
            /// OpenAI 互換（Groq / OpenAI）
            case openAICompatible(base: String, provider: APIProvider)
            case gemini
        }

        let label: String
        let kind: Kind
        let modelID: String
        /// 入力 USD / 100万トークン
        let priceIn: Double
        /// 出力 USD / 100万トークン
        let priceOut: Double

        /// このエンジンが要る API キー（Apple は nil）
        var provider: APIProvider? {
            switch kind {
            case .apple: return nil
            case let .openAICompatible(_, provider): return provider
            case .gemini: return .gemini
            }
        }

        /// Gemini 系か（別枠の上限を数えるため）
        var isGemini: Bool {
            if case .gemini = kind { return true }
            return false
        }

        /// リクエスト上限に数える（＝課金される外部 API）か
        var isMetered: Bool { provider != nil }
    }

    /// エンジンごとの集計
    private struct EngineSummary {
        let engine: Engine
        /// SKIP した理由（実行した場合は nil）
        let skipped: String?
        var latencies: [Double] = []
        var inputTokens = 0
        var outputTokens = 0
        var failures = 0

        /// 遅延の中央値（ms）
        var medianMS: Double {
            guard !latencies.isEmpty else { return 0 }
            let sorted = latencies.sorted()
            let middle = sorted.count / 2
            return sorted.count % 2 == 0 ? (sorted[middle - 1] + sorted[middle]) / 2 : sorted[middle]
        }

        /// 概算費用（円）
        var costJPY: Double {
            let usd = Double(inputTokens) / 1_000_000 * engine.priceIn
                + Double(outputTokens) / 1_000_000 * engine.priceOut
            return usd * usdToJPY
        }

        /// 1 行サマリ
        func line() -> String {
            if let skipped {
                return "[SUMMARY] engine=\(engine.label) status=skip reason=\(skipped)"
            }
            return String(
                format: "[SUMMARY] engine=%@ model=%@ ok=%d fail=%d medianMs=%.0f maxMs=%.0f in=%d out=%d 費用=%.3f円",
                engine.label, engine.modelID, latencies.count, failures,
                medianMS, latencies.max() ?? 0, inputTokens, outputTokens, costJPY
            )
        }
    }

    /// リクエスト数の予算管理（上限を超えたら投げない）
    private final class RequestBudget {
        private let total: Int
        private let geminiLimit: Int
        private(set) var used = 0
        private(set) var geminiUsed = 0

        init(total: Int, gemini: Int) {
            self.total = total
            self.geminiLimit = gemini
        }

        /// あと 1 回投げてよいか
        func canSpend(isGemini: Bool) -> Bool {
            guard used < total else { return false }
            return isGemini ? geminiUsed < geminiLimit : true
        }

        /// 1 回分を計上する
        func spend(isGemini: Bool) {
            used += 1
            if isGemini { geminiUsed += 1 }
        }
    }
}
