/// Gemini API による英→日翻訳（任意エンジン）
///
/// Swift の公式 SDK が無いため URLSession で raw HTTP を叩く。
/// リアルタイム字幕なのでストリーミングは使わず、1 まとまりずつ短い応答を取る。
///
/// 既定モデル `gemini-3.5-flash-lite` は無料枠の対象で、thinking の既定が minimal のため
/// `thinkingConfig` を明示しなくても字幕向けの低レイテンシで返る（余計なフィールドを
/// 送って 400 になる方が事故が大きいので、既定に任せている）。
import Foundation
import OSLog

/// Gemini API を叩く翻訳器
@available(macOS 26.0, *)
final class GeminiTranslator: Translator {

    /// generateContent エンドポイントの組み立て元
    private static let endpointBase = "https://generativelanguage.googleapis.com/v1beta/models"

    /// 翻訳の指示。字幕は 1 文ずつ短く送るため、文脈補完の方針をここで固定する。
    static let systemPrompt = """
        あなたはライブ配信音声の同時字幕翻訳者です。入力は英語音声の音声認識テキストで、\
        誤認識・句読点の欠落・語の欠落が含まれることがあります。前後の文脈から自然に補い、\
        話し言葉として自然な日本語字幕に翻訳してください。
        出力は日本語訳のみ。前置き・注釈・原文の再掲・引用符は付けないでください。
        固有名詞や専門用語は無理に訳さず、一般的な表記があればそれを使ってください。
        """

    private let logger = makeCaptionLogger("GeminiTranslator")
    private let session: URLSession
    /// キーは毎回取り直す（設定画面で保存された直後から新しいキーが効くように）
    private let keyProvider: @Sendable () -> String?
    /// モデル ID も毎回取り直す（設定変更を再起動なしで反映するため）
    private let modelProvider: @Sendable () -> String

    /// - Parameters:
    ///   - keyProvider: API キーを返すクロージャ（未設定なら nil）
    ///   - modelProvider: 使用するモデル ID を返すクロージャ
    init(
        keyProvider: @escaping @Sendable () -> String? = { APIKeyStore.load()?.key },
        modelProvider: @escaping @Sendable () -> String = { CaptionSettings.geminiModelID }
    ) {
        self.keyProvider = keyProvider
        self.modelProvider = modelProvider

        let configuration = URLSessionConfiguration.ephemeral
        // 字幕が数十秒固まるより早く諦めて原文表示に落ちる方がよい
        configuration.timeoutIntervalForRequest = 20
        configuration.timeoutIntervalForResource = 30
        configuration.waitsForConnectivity = false
        self.session = URLSession(configuration: configuration)
    }

    func translate(_ text: String, context: [TranslationExchange]) async throws -> String {
        guard let key = keyProvider(), !key.isEmpty else { throw TranslationError.missingAPIKey }
        let body = requestBody(text: text, context: context, maxOutputTokens: 512)
        return try await send(body: body, model: modelProvider(), key: key, allowRetry: true)
    }

    /// キーが有効かどうかを最小のリクエストで確かめる
    ///
    /// 設定画面の「保存して確認」から呼ぶ。無言で保存して後で失敗するより、
    /// その場で有効／無効を返した方が詰まらない。
    ///
    /// - Parameter key: 検証したいキー
    /// - Throws: 無効・接続不可などの `TranslationError`
    func verify(key: String) async throws {
        let body: [String: Any] = [
            "contents": [["role": "user", "parts": [["text": "ping"]]]],
            "generationConfig": ["maxOutputTokens": 8],
        ]
        // 検証はユーザーを待たせるので再試行しない（結果をすぐ返す）
        _ = try await send(body: body, model: modelProvider(), key: key, allowRetry: false)
    }

    // MARK: - 内部処理

    /// リクエスト JSON を組み立てる
    ///
    /// 直近の確定ペアを user/model 交互で並べ、最後に今回の原文を置く。
    /// サンプリング系パラメータ（temperature 等）は付けない。
    private func requestBody(
        text: String,
        context: [TranslationExchange],
        maxOutputTokens: Int
    ) -> [String: Any] {
        var contents: [[String: Any]] = []
        for exchange in context {
            contents.append(["role": "user", "parts": [["text": exchange.source]]])
            contents.append(["role": "model", "parts": [["text": exchange.translated]]])
        }
        contents.append(["role": "user", "parts": [["text": text]]])

        return [
            "systemInstruction": ["parts": [["text": Self.systemPrompt]]],
            "contents": contents,
            "generationConfig": ["maxOutputTokens": maxOutputTokens],
        ]
    }

    /// リクエストを送り、テキストを取り出す
    ///
    /// - Parameters:
    ///   - body: リクエスト JSON
    ///   - model: モデル ID
    ///   - key: API キー
    ///   - allowRetry: 一時的な失敗で 1 回だけ再試行するか
    private func send(body: [String: Any], model: String, key: String, allowRetry: Bool) async throws -> String {
        do {
            return try await performOnce(body: body, model: model, key: key)
        } catch let error as TranslationError {
            guard allowRetry, let delay = retryDelay(for: error) else { throw error }
            logger.notice("翻訳を再試行します 理由=\(error.description, privacy: .public) 待機=\(delay, format: .fixed(precision: 2), privacy: .public)s")
            try? await Task.sleep(for: .seconds(delay))
            return try await performOnce(body: body, model: model, key: key)
        }
    }

    /// 一時的な失敗かどうかを判定し、待機秒数を返す（恒久的な失敗なら nil）
    private func retryDelay(for error: TranslationError) -> TimeInterval? {
        switch error {
        case .network:
            return 0.5
        case let .rateLimited(retryAfter):
            let delay = retryAfter ?? 1.0
            // 字幕は鮮度が命なので、長い待機を指示されたら諦めて原文表示に落とす
            return delay <= 5.0 ? delay : nil
        case .serverError:
            return 1.0
        case .missingAPIKey, .unauthorized, .badRequest, .badResponse, .translationUnavailable:
            return nil
        }
    }

    /// 実際に 1 回だけ HTTP を投げる
    private func performOnce(body: [String: Any], model: String, key: String) async throws -> String {
        guard let url = URL(string: "\(Self.endpointBase)/\(model):generateContent") else {
            throw TranslationError.badRequest(message: "モデル ID が不正です: \(model)")
        }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        // キーはヘッダで渡す。URL クエリに載せると各種ログへ残るため使わない。
        request.setValue(key, forHTTPHeaderField: "x-goog-api-key")
        request.setValue("application/json", forHTTPHeaderField: "content-type")
        request.httpBody = try JSONSerialization.data(withJSONObject: body)

        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await session.data(for: request)
        } catch {
            throw TranslationError.network((error as NSError).localizedDescription)
        }

        guard let http = response as? HTTPURLResponse else {
            throw TranslationError.badResponse("HTTP レスポンスではありません")
        }

        switch http.statusCode {
        case 200:
            return try extractText(from: data)
        case 401, 403:
            throw TranslationError.unauthorized
        case 429:
            throw TranslationError.rateLimited(retryAfter: retryAfterSeconds(http: http, data: data))
        case 400...499:
            throw TranslationError.badRequest(message: errorMessage(from: data))
        default:
            throw TranslationError.serverError(status: http.statusCode, message: errorMessage(from: data))
        }
    }

    /// 再試行までの待機秒数を取り出す
    ///
    /// Gemini は `Retry-After` ヘッダを返さないことがあり、その場合は
    /// エラー詳細の `RetryInfo.retryDelay`（"12s" 形式）に入っている。
    private func retryAfterSeconds(http: HTTPURLResponse, data: Data) -> TimeInterval? {
        if let header = http.value(forHTTPHeaderField: "Retry-After"), let seconds = TimeInterval(header) {
            return seconds
        }
        guard let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let error = object["error"] as? [String: Any],
              let details = error["details"] as? [[String: Any]] else { return nil }
        for detail in details {
            guard let delay = detail["retryDelay"] as? String else { continue }
            return TimeInterval(delay.replacingOccurrences(of: "s", with: ""))
        }
        return nil
    }

    /// 成功応答から訳文を取り出す
    private func extractText(from data: Data) throws -> String {
        guard let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            throw TranslationError.badResponse("JSON として読めません")
        }
        // 安全性フィルタで入力ごと弾かれた場合は candidates が空になるので先に見る
        if let feedback = object["promptFeedback"] as? [String: Any],
           let reason = feedback["blockReason"] as? String {
            throw TranslationError.badResponse("入力がブロックされました(\(reason))")
        }
        guard let candidates = object["candidates"] as? [[String: Any]],
              let first = candidates.first else {
            throw TranslationError.badResponse("candidates がありません")
        }
        let parts = (first["content"] as? [String: Any])?["parts"] as? [[String: Any]] ?? []
        let text = parts
            .compactMap { $0["text"] as? String }
            .joined()
            .trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else {
            // finishReason を添えないと「なぜ空なのか」が追えない（SAFETY / MAX_TOKENS 等）
            let reason = first["finishReason"] as? String ?? "不明"
            throw TranslationError.badResponse("テキストが空です(finishReason=\(reason))")
        }
        return text
    }

    /// エラー応答から人間が読めるメッセージを取り出す
    private func errorMessage(from data: Data) -> String {
        guard let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let error = object["error"] as? [String: Any],
              let message = error["message"] as? String else {
            return String(data: data.prefix(200), encoding: .utf8) ?? "(詳細なし)"
        }
        return message
    }
}
