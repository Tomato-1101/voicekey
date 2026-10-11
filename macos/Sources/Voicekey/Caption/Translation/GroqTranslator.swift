/// Groq API による英→日翻訳（OpenAI 互換・SSE ストリーミング）
///
/// エンドポイントは OpenAI 互換の `POST /openai/v1/chat/completions` なので、
/// このファイルの組み立て・SSE 解析は `endpointBase` と API キーを差し替えるだけで
/// OpenAI にもそのまま使える（ベンチ側もこの形を共有している）。
///
/// クラウド共通ポリシー（CLAUDE.md の恒久要件）:
///   - 部分訳では呼ばない（確定文のみ）。高頻度呼び出しをしない。
///   - 失敗・429 のときは Apple 翻訳へ落とす（`FallbackTranslator` が担当し notice を残す）。
///   - 既定エンジンは Apple のまま変えない。
import Foundation

/// Groq（OpenAI 互換 API）の翻訳器
@available(macOS 26.0, *)
struct GroqTranslator: StreamingTranslator {

    /// OpenAI 互換エンドポイントの組み立て元
    static let endpointBase = "https://api.groq.com/openai/v1"

    private let session: URLSession
    /// キーは毎回取り直す（設定画面で保存された直後から新しいキーが効くように）
    private let keyProvider: @Sendable () -> String?
    /// モデル ID も毎回取り直す（設定変更を再起動なしで反映するため）
    private let modelProvider: @Sendable () -> String
    /// システムプロンプト（既定は字幕の英→日。音声入力の「翻訳して入力」が向きを差し替える）
    private let systemPromptProvider: @Sendable () -> String
    /// API 使用量の内訳に出す用途（字幕の翻訳 / 音声入力の「翻訳して入力」）
    private let usagePurpose: ApiUsagePurpose
    /// モデルが提供終了だったときに書き換える設定（nil なら差し替えずに失敗を返す）
    private let modelSetting: LLMModelSetting?

    /// - Parameters:
    ///   - keyProvider: API キーを返すクロージャ（未設定なら nil）
    ///   - modelProvider: 使用するモデル ID を返すクロージャ
    ///   - systemPromptProvider: システムプロンプトを返すクロージャ（既定は字幕の英→日）
    ///   - usagePurpose: 使用量を記録するときの用途（既定は字幕の翻訳）
    ///   - modelSetting: モデルが提供終了だったときにおすすめへ書き換える設定（既定は字幕の Groq モデル）
    init(
        keyProvider: @escaping @Sendable () -> String? = { APIKeyStore.load(.groq)?.key },
        modelProvider: @escaping @Sendable () -> String = { CaptionSettings.groqModelID },
        systemPromptProvider: @escaping @Sendable () -> String = { GeminiTranslator.systemPrompt },
        usagePurpose: ApiUsagePurpose = .captionTranslation,
        modelSetting: LLMModelSetting? = .captionGroq
    ) {
        self.keyProvider = keyProvider
        self.modelProvider = modelProvider
        self.systemPromptProvider = systemPromptProvider
        self.usagePurpose = usagePurpose
        self.modelSetting = modelSetting

        let configuration = URLSessionConfiguration.ephemeral
        // 字幕が数十秒固まるより、早く諦めて Apple 翻訳へ落ちる方がよい
        configuration.timeoutIntervalForRequest = 20
        configuration.timeoutIntervalForResource = 30
        configuration.waitsForConnectivity = false
        self.session = URLSession(configuration: configuration)
    }

    // MARK: - Translator / StreamingTranslator

    func translate(_ text: String, context: [TranslationExchange]) async throws -> String {
        try await translateStreaming(text, context: context, onDelta: { _ in })
    }

    /// ストリーミングで訳す
    ///
    /// SSE の `choices[0].delta.content` が届くたびに `onDelta` を呼び、最後に全文を返す。
    /// 再試行はここではしない（失敗は `FallbackTranslator` が Apple へ落とす。
    /// 字幕は鮮度が命なので、待ち直すより落とした方が速い）。
    func translateStreaming(
        _ text: String,
        context: [TranslationExchange],
        onDelta: @escaping @Sendable (String) -> Void
    ) async throws -> String {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return "" }
        guard let key = keyProvider(), !key.isEmpty else { throw TranslationError.missingAPIKey }

        // 使用量の記録に同じモデル名を使うため 1 回だけ取り出す
        var model = modelProvider()
        let systemPrompt = systemPromptProvider()

        let stream: URLSession.AsyncBytes
        switch try await openStream(model: model, key: key, text: trimmed, context: context, systemPrompt: systemPrompt) {
        case .ok(let opened):
            stream = opened
        case let .failed(http, body):
            // モデルの提供終了だけは、その 1 回をおすすめで取り直して設定も書き換える
            // （放置すると以後ずっと同じ理由で失敗し続ける。2026-10 の llama 廃止で実際に起きた）
            guard LLMModelCatalog.isModelUnavailable(status: http.statusCode, data: body),
                  let setting = modelSetting,
                  let replacement = LLMModelCatalog.replaceUnavailable(setting, failedModel: model) else {
                throw Self.mapError(status: http.statusCode, http: http, data: body)
            }
            model = replacement
            switch try await openStream(model: model, key: key, text: trimmed, context: context, systemPrompt: systemPrompt) {
            case .ok(let opened):
                stream = opened
            case let .failed(http, body):
                throw Self.mapError(status: http.statusCode, http: http, data: body)
            }
        }

        var accumulated = ""
        var usageRecorded = false
        var finishReason: String?
        var interrupted = false
        do {
            for try await line in stream.lines {
                // 最終チャンクの usage（include_usage の `usage` か Groq 独自の `x_groq.usage`）を 1 回だけ記録する。
                // usage を含む行だけを読む（差分の行ごとに余計な JSON 解析をしない）
                if !usageRecorded, line.contains("\"usage\""), line.hasPrefix("data:"),
                   let json = line.dropFirst("data:".count)
                       .trimmingCharacters(in: .whitespaces).data(using: .utf8) {
                    usageRecorded = ApiUsageStore.shared.recordChatUsage(
                        fromJSON: json, provider: .groq, model: model, purpose: usagePurpose)
                }
                if let reason = Self.finishReason(fromSSELine: line) { finishReason = reason }
                guard let piece = Self.deltaText(fromSSELine: line) else { continue }
                if piece.isEmpty { continue }
                accumulated += piece
                onDelta(piece)
            }
        } catch {
            guard !accumulated.isEmpty else {
                throw TranslationError.network((error as NSError).localizedDescription)
            }
            interrupted = true
        }
        // 途中で切れた訳を成功扱いにしない（以前は受け取った分だけで確定させていたため、
        // 文の後半が欠けた訳がそのまま字幕・貼り付けに出ていた）。失敗にすれば呼び出し側の
        // フォールバック（字幕は Apple の訳）が効く。
        if let error = Self.completionError(finishReason: finishReason, interrupted: interrupted) {
            throw error
        }

        let result = accumulated.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !result.isEmpty else { throw TranslationError.badResponse("訳文が空でした") }
        return result
    }

    /// ストリームを開いた結果（失敗時は本文まで読み切って返す）
    private enum OpenResult {
        case ok(URLSession.AsyncBytes)
        case failed(HTTPURLResponse, Data)
    }

    /// 1 回分のリクエストを送ってストリームを開く
    private func openStream(
        model: String, key: String, text: String, context: [TranslationExchange], systemPrompt: String
    ) async throws -> OpenResult {
        let request = try Self.makeChatRequest(
            endpointBase: Self.endpointBase,
            model: model,
            key: key,
            text: text,
            context: context,
            stream: true,
            systemPrompt: systemPrompt
        )
        let stream: URLSession.AsyncBytes
        let response: URLResponse
        do {
            (stream, response) = try await session.bytes(for: request)
        } catch {
            throw TranslationError.network((error as NSError).localizedDescription)
        }
        guard let http = response as? HTTPURLResponse else {
            throw TranslationError.badResponse("HTTP レスポンスではありません")
        }
        guard http.statusCode == 200 else {
            // 本文はエラーのときだけ読む（成功時に読むと本編のストリームを食ってしまう）
            var body = Data()
            for try await byte in stream { body.append(byte) }
            return .failed(http, body)
        }
        return .ok(stream)
    }

    // MARK: - OpenAI 互換の共通処理（ベンチからも使う）

    /// chat/completions のリクエストを組み立てる
    ///
    /// 文脈は user / assistant の交互メッセージで渡す（OpenAI 互換 API の作法）。
    ///
    /// - Parameters:
    ///   - endpointBase: `/chat/completions` の 1 つ上まで（例 `https://api.groq.com/openai/v1`）
    ///   - model: モデル ID
    ///   - key: API キー（Bearer で送る）
    ///   - text: 原文
    ///   - context: 直近の確定ペア
    ///   - stream: SSE で受け取るか
    ///   - systemPrompt: システムプロンプト（既定は実運用のもの。ベンチだけ差し替える）
    static func makeChatRequest(
        endpointBase: String,
        model: String,
        key: String,
        text: String,
        context: [TranslationExchange],
        stream: Bool,
        systemPrompt: String = GeminiTranslator.systemPrompt
    ) throws -> URLRequest {
        guard let url = URL(string: "\(endpointBase)/chat/completions") else {
            throw TranslationError.badRequest(message: "エンドポイントが不正です: \(endpointBase)")
        }
        var messages: [[String: String]] = [["role": "system", "content": systemPrompt]]
        for exchange in context {
            messages.append(["role": "user", "content": exchange.source])
            messages.append(["role": "assistant", "content": exchange.translated])
        }
        messages.append(["role": "user", "content": text])

        var body: [String: Any] = [
            "model": model,
            "messages": messages,
            // 固定 512 だと推論モデル（gpt-oss 等）は思考で使い切って途中で切れるので、音声入力と同じ式で確保する
            "max_completion_tokens": LLMModelCatalog.maxCompletionTokens(inputCharacters: text.count),
        ]
        // 推論モデルの思考を抑える等、モデル別の指定を足す（整形・翻訳とも同じ表を使う）
        body.merge(LLMModelCatalog.extraParameters(model: model)) { _, new in new }
        if stream {
            body["stream"] = true
            // 最終チャンクにトークン数（usage）を載せてもらう＝ API 使用量の記録に使う（OpenAI 互換の指定）
            body["stream_options"] = ["include_usage": true]
        }

        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("Bearer \(key)", forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: body)
        return request
    }

    /// SSE の 1 行から差分テキストを取り出す（`data: [DONE]` や空行は nil）
    static func deltaText(fromSSELine line: String) -> String? {
        guard line.hasPrefix("data:") else { return nil }
        let payload = line.dropFirst("data:".count).trimmingCharacters(in: .whitespaces)
        guard payload != "[DONE]", let data = payload.data(using: .utf8) else { return nil }
        guard let root = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let choices = root["choices"] as? [[String: Any]],
              let delta = choices.first?["delta"] as? [String: Any] else { return nil }
        return delta["content"] as? String
    }

    /// SSE の 1 行から `choices[0].finish_reason` を取り出す（無い行は nil）
    static func finishReason(fromSSELine line: String) -> String? {
        guard line.hasPrefix("data:") else { return nil }
        let payload = line.dropFirst("data:".count).trimmingCharacters(in: .whitespaces)
        guard payload != "[DONE]", let data = payload.data(using: .utf8),
              let root = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let choices = root["choices"] as? [[String: Any]] else { return nil }
        return choices.first?["finish_reason"] as? String
    }

    /// ストリームの終わり方から、訳が最後まで届いたかを判定する
    ///
    /// - Parameters:
    ///   - finishReason: 最後に届いた `finish_reason`（届かなければ nil）
    ///   - interrupted: 受信の途中で通信が切れたか
    /// - Returns: 最後まで届いていなければそのエラー。届いていれば nil
    static func completionError(finishReason: String?, interrupted: Bool) -> TranslationError? {
        if interrupted { return .badResponse("訳が途中で切れました（通信の切断）") }
        switch finishReason {
        case "stop": return nil
        // "length" は出力上限で打ち切られた＝文の後半が無い
        case let reason?: return .badResponse("訳が途中で切れました（finish_reason=\(reason)）")
        // OpenAI 互換の SSE は最後のチャンクに必ず finish_reason を付ける。無いのは途中で終わった印
        case nil: return .badResponse("訳が途中で切れました（finish_reason なし）")
        }
    }

    /// 非ストリーミング応答から訳文を取り出す（ベンチ用）
    static func extractText(from data: Data) throws -> String {
        guard let root = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            throw TranslationError.badResponse("JSON として読めません")
        }
        guard let choices = root["choices"] as? [[String: Any]],
              let message = choices.first?["message"] as? [String: Any],
              let content = message["content"] as? String else {
            throw TranslationError.badResponse("choices がありません")
        }
        let text = content.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { throw TranslationError.badResponse("テキストが空です") }
        return text
    }

    /// HTTP ステータスを翻訳エラーへ写す
    static func mapError(status: Int, http: HTTPURLResponse, data: Data) -> TranslationError {
        let message = errorMessage(from: data)
        switch status {
        case 401, 403:
            return .unauthorized
        case 429:
            let header = http.value(forHTTPHeaderField: "retry-after").flatMap(TimeInterval.init)
            return .rateLimited(retryAfter: header)
        case 400...499:
            return .badRequest(message: message)
        default:
            return .serverError(status: status, message: message)
        }
    }

    /// エラー応答から人間が読めるメッセージを取り出す
    static func errorMessage(from data: Data) -> String {
        if let root = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
           let error = root["error"] as? [String: Any],
           let message = error["message"] as? String {
            return message
        }
        return String(data: data.prefix(200), encoding: .utf8) ?? "(詳細なし)"
    }

    /// 使えるモデル ID の一覧を取る（モデル選定用。`GET /models`）
    ///
    /// - Parameter key: API キー
    /// - Returns: モデル ID の一覧（昇順）
    static func listModels(key: String) async throws -> [String] {
        guard let url = URL(string: "\(endpointBase)/models") else { return [] }
        var request = URLRequest(url: url)
        request.setValue("Bearer \(key)", forHTTPHeaderField: "Authorization")

        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await URLSession(configuration: .ephemeral).data(for: request)
        } catch {
            throw TranslationError.network((error as NSError).localizedDescription)
        }
        guard let http = response as? HTTPURLResponse else {
            throw TranslationError.badResponse("HTTP レスポンスではありません")
        }
        guard http.statusCode == 200 else {
            throw mapError(status: http.statusCode, http: http, data: data)
        }
        guard let root = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let list = root["data"] as? [[String: Any]] else {
            throw TranslationError.badResponse("models の形式が想定と違います")
        }
        return list.compactMap { $0["id"] as? String }.sorted()
    }
}
