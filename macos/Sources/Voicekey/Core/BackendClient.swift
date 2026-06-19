//
//  BackendClient.swift
//  自社バックエンド経由でプロバイダを使うためのクライアント（製品版・Phase 5 段階2）
//
//  製品版は長期 API キーをアプリに持たない。Supabase の access_token で自社サーバーに
//  認証し、サーバーがサブスク有効性を検証する。Deepgram は短命 JWT を受け取って
//  アプリが直叩き（低レイテンシ核心を維持）、ElevenLabs/Groq はサーバープロキシ経由で叩く。
//
//  このファイルは「クライアントの提供」だけを担う。既存の Transcriber /
//  StreamingTranscriber / TextFormatter への配線は段階3 で行う。
//  サーバー契約は voicekey-site の app/api/v1/{auth/ephemeral,transcribe/elevenlabs,format} と一致させる。
//

import Foundation

enum BackendClient {

    /// バックエンド呼び出しのエラー（ユーザー向け日本語メッセージ付き）
    enum BackendError: Error {
        case unauthenticated      // ローカルに認証セッションが無い（未ログイン）
        case unauthorized         // 401: トークン無効/期限切れ（要再ログイン・リフレッシュ）
        case noSubscription       // 403: サブスク無効
        case deviceLimit          // 409: 同時利用デバイス上限
        case rateLimited          // 429: 発行/利用が多すぎる
        case providerUnavailable  // 503: サーバー側プロバイダ未設定
        case server(Int)          // その他の非 200
        case network(Error)       // 通信失敗
        case invalidResponse      // レスポンス解釈不能

        var userMessage: String {
            switch self {
            case .unauthenticated: return "ログインが必要です"
            case .unauthorized: return "ログインの有効期限が切れました。再度ログインしてください"
            case .noSubscription: return "サブスクリプションが有効ではありません"
            case .deviceLimit: return "利用できるデバイス数の上限に達しました"
            case .rateLimited: return "リクエストが多すぎます。少し待ってからお試しください"
            case .providerUnavailable: return "サーバー側の設定エラーです。時間をおいてお試しください"
            case .server(let code): return "サーバーエラー (HTTP \(code))"
            case .network(let e): return "通信に失敗しました: \(e.localizedDescription)"
            case .invalidResponse: return "サーバー応答を解釈できませんでした"
            }
        }
    }

    /// Deepgram の短命トークンと失効時刻
    struct EphemeralToken {
        let token: String
        let expiresAt: Date
    }

    /// 短い接続タイムアウトの専用セッション（録音直前に叩くため待たせない）
    private static let session: URLSession = {
        let c = URLSessionConfiguration.ephemeral
        c.timeoutIntervalForRequest = 15
        c.timeoutIntervalForResource = 60
        c.waitsForConnectivity = false
        return URLSession(configuration: c)
    }()

    // MARK: - 公開 API

    /// Deepgram「高速リアルタイム」用の短命 JWT を取得する。
    /// 録音直前に呼び、返ってきた JWT で Deepgram WebSocket を `Bearer` 認証で開く（段階3）。
    static func fetchEphemeralToken() async throws -> EphemeralToken {
        var req = try authorizedRequest(path: ServerConfig.ephemeralPath)
        req.httpMethod = "POST"
        let data = try await send(req)
        struct Resp: Decodable { let token: String; let expires_in: Int }
        guard let r = try? JSONDecoder().decode(Resp.self, from: data) else {
            throw BackendError.invalidResponse
        }
        return EphemeralToken(
            token: r.token,
            expiresAt: Date().addingTimeInterval(TimeInterval(r.expires_in))
        )
    }

    /// ElevenLabs「正確性」プロキシで文字起こしする（WAV を multipart 送信）。
    static func transcribeElevenLabs(wav: Data, language: String) async throws -> String {
        var req = try authorizedRequest(path: ServerConfig.elevenLabsProxyPath)
        req.httpMethod = "POST"
        let boundary = "vk-\(UUID().uuidString)"
        req.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")
        req.httpBody = multipartBody(boundary: boundary, wav: wav, language: language)
        let data = try await send(req)
        struct Resp: Decodable { let text: String? }
        return (try? JSONDecoder().decode(Resp.self, from: data))?.text ?? ""
    }

    /// Groq テキスト整形プロキシ（モデル/プロンプトはサーバー固定。text のみ送る）。
    /// 失敗時は呼び出し側で原文フォールバックする想定なので throw で返す。
    static func formatText(_ text: String) async throws -> String {
        var req = try authorizedRequest(path: ServerConfig.formatProxyPath)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: ["text": text])
        let data = try await send(req)
        struct Resp: Decodable { let text: String? }
        return (try? JSONDecoder().decode(Resp.self, from: data))?.text ?? text
    }

    // MARK: - 内部

    /// 認証ヘッダ（Bearer access_token + device_id + platform）付きリクエストを組み立てる
    private static func authorizedRequest(path: String) throws -> URLRequest {
        guard let auth = Keychain.authSession() else { throw BackendError.unauthenticated }
        guard let url = ServerConfig.url(path) else { throw BackendError.invalidResponse }
        var req = URLRequest(url: url)
        req.setValue("Bearer \(auth.accessToken)", forHTTPHeaderField: "Authorization")
        req.setValue(Keychain.deviceId(), forHTTPHeaderField: "x-device-id")
        req.setValue("mac", forHTTPHeaderField: "x-platform")
        return req
    }

    /// リクエストを投げ、200 ならボディを返す。非 200 はステータスをエラーへ写す
    private static func send(_ req: URLRequest) async throws -> Data {
        let data: Data
        let resp: URLResponse
        do {
            (data, resp) = try await session.data(for: req)
        } catch {
            throw BackendError.network(error)
        }
        guard let http = resp as? HTTPURLResponse else { throw BackendError.invalidResponse }
        guard http.statusCode == 200 else { throw mapStatus(http.statusCode) }
        return data
    }

    private static func mapStatus(_ code: Int) -> BackendError {
        switch code {
        case 401: return .unauthorized
        case 403: return .noSubscription
        case 409: return .deviceLimit
        case 429: return .rateLimited
        case 503: return .providerUnavailable
        default: return .server(code)
        }
    }

    /// ElevenLabs プロキシ用の multipart ボディ（field: language, file=audio.wav）
    private static func multipartBody(boundary: String, wav: Data, language: String) -> Data {
        var body = Data()
        func append(_ s: String) { body.append(Data(s.utf8)) }
        if !language.isEmpty {
            append("--\(boundary)\r\n")
            append("Content-Disposition: form-data; name=\"language\"\r\n\r\n")
            append("\(language)\r\n")
        }
        append("--\(boundary)\r\n")
        append("Content-Disposition: form-data; name=\"file\"; filename=\"audio.wav\"\r\n")
        append("Content-Type: audio/wav\r\n\r\n")
        body.append(wav)
        append("\r\n--\(boundary)--\r\n")
        return body
    }
}
