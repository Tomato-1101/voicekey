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
        case freeQuotaExhausted   // 402: 無料体験を使い切った（要アクティベーション）
        case noSubscription       // 403: サブスク無効
        case deviceLimit          // 409: 同時利用デバイス上限
        case rateLimited          // 429: 発行/利用が多すぎる
        case providerUnavailable  // 503: サーバー側プロバイダ未設定
        case server(Int)          // その他の非 200
        case network(Error)       // 通信失敗
        case invalidResponse      // レスポンス解釈不能
        case message(String)      // サーバーが返した日本語メッセージをそのまま表示する

        var userMessage: String {
            switch self {
            case .unauthenticated: return "ログインが必要です"
            case .unauthorized: return "ログインの有効期限が切れました。再度ログインしてください"
            case .freeQuotaExhausted: return "無料体験を使い切りました。続けて使うにはアクティベーションキーの登録が必要です（設定 → アカウント）"
            case .noSubscription: return "利用するにはアクティベーションキーの登録が必要です（設定 → アカウント）"
            case .deviceLimit: return "利用できるデバイス数の上限に達しました"
            case .rateLimited: return "リクエストが多すぎます。少し待ってからお試しください"
            case .providerUnavailable: return "サーバー側の設定エラーです。時間をおいてお試しください"
            case .server(let code): return "サーバーエラー (HTTP \(code))"
            case .network(let e): return "通信に失敗しました: \(e.localizedDescription)"
            case .invalidResponse: return "サーバー応答を解釈できませんでした"
            case .message(let m): return m
            }
        }
    }

    /// Deepgram の短命トークンと失効時刻
    struct EphemeralToken {
        let token: String
        let expiresAt: Date
    }

    /// ログイン中アカウントの状態（メール・利用権の有無/期限・無料体験の残量）
    struct AccountStatus {
        let email: String?
        let active: Bool          // 有効な利用権（アクティベーションキー or サブスク）があるか
        let activeUntil: Date?    // 利用権の期限（active=true のとき）
        let freeUsed: Int         // 無料体験の消費済み回数
        let freeQuota: Int        // 無料体験の上限回数
        let freeRemaining: Int    // 無料体験の残り回数（active=false のとき意味を持つ）
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

    /// 製品版サーバー経由（短命トークン / プロキシ）を使うべきかを返す。
    ///
    /// ローカルに認証セッション（access_token）があれば true ＝ ログイン済み。
    /// 各文字起こし/整形プリミティブはこれが true のときだけサーバー経路に切り替え、
    /// false のときは従来の埋め込み/設定キーによる直叩きを使う（段階3 の並存ガード）。
    static var isLoggedIn: Bool {
        Keychain.authSession() != nil
    }

    /// 短命トークンのキャッシュと取得の集約（録音ごとの往復を省く）。
    /// TTL(60秒)内なら録音をまたいで同じ JWT を再利用する。Deepgram は接続確立時にのみ
    /// トークンを検証し、確立後の長い録音中は再検証しないため、残時間が短くても接続には十分。
    /// 同時取得は 1 本の Task に集約し、二重発行（＝サーバーのレート/台数枠の浪費）を防ぐ。
    private static let tokenLock = NSLock()
    private static var cachedToken: EphemeralToken?
    private static var inFlightToken: Task<EphemeralToken, Error>?

    /// Deepgram「高速リアルタイム」用の短命 JWT を取得する。
    /// 録音直前に呼び、返ってきた JWT で Deepgram WebSocket を `Bearer` 認証で開く（段階3）。
    /// 十分な残時間のキャッシュがあればネットワーク往復ゼロで即返す（2回目以降の録音を高速化）。
    static func fetchEphemeralToken() async throws -> EphemeralToken {
        // ロック操作は同期ヘルパに閉じ込める（async コンテキストで NSLock を直接触らない）。
        let (cached, task) = cachedOrInFlightToken()
        if let cached { return cached }  // 往復ゼロ
        return try await task!.value     // cached が nil なら task は必ず非 nil
    }

    /// キャッシュ確認と取得の集約を同期・ロック下で行う。
    /// 戻り値はちょうど一方が非 nil（有効キャッシュ or 取得 Task）。
    private static func cachedOrInFlightToken() -> (EphemeralToken?, Task<EphemeralToken, Error>?) {
        tokenLock.lock(); defer { tokenLock.unlock() }
        // 1) 残 15 秒超のキャッシュがあれば即返す
        if let c = cachedToken, c.expiresAt.timeIntervalSinceNow > 15 {
            return (c, nil)
        }
        // 2) 進行中の取得があれば相乗りする（録音開始の二重呼び出しを 1 往復に集約）
        if let existing = inFlightToken {
            return (nil, existing)
        }
        let task = Task {
            defer { clearInFlightToken() }  // 完了時に自分でクリア
            let tok = try await performFetchEphemeralToken()
            storeToken(tok)  // クリアより前にキャッシュへ（取得直後の呼び出しを取りこぼさない）
            return tok
        }
        inFlightToken = task
        return (nil, task)
    }

    /// 進行中フラグを下ろす（取得 Task の完了時に一度だけ呼ぶ）。
    private static func clearInFlightToken() {
        tokenLock.lock(); defer { tokenLock.unlock() }
        inFlightToken = nil
    }

    /// 取得したトークンをキャッシュへ保存する（同期・ロック下）。
    private static func storeToken(_ tok: EphemeralToken) {
        tokenLock.lock(); defer { tokenLock.unlock() }
        cachedToken = tok
    }

    /// 実際のトークン取得（fetchEphemeralToken からのみ呼ぶ。集約はラッパー側で保証する）。
    private static func performFetchEphemeralToken() async throws -> EphemeralToken {
        try? await AuthClient.ensureValidSession()  // 失効間際なら先にリフレッシュ
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

    /// 短命トークンのキャッシュを破棄する（ログアウト時。別アカウントでの再利用を防ぐ）。
    static func clearTokenCache() {
        tokenLock.lock()
        cachedToken = nil
        tokenLock.unlock()
    }

    /// ログイン中アカウントの状態（メール・利用権の有無/期限）を取得する。
    /// 設定画面の表示・ゲート判定の事前確認に使う（200 固定＝未契約でも 200 で active:false）。
    static func fetchAccountStatus() async throws -> AccountStatus {
        try? await AuthClient.ensureValidSession()
        let req = try authorizedRequest(path: ServerConfig.mePath)  // GET（httpMethod 未設定）
        let data = try await send(req)
        struct Resp: Decodable {
            let email: String?; let active: Bool?; let active_until: String?
            let free_used: Int?; let free_quota: Int?; let free_remaining: Int?
        }
        guard let r = try? JSONDecoder().decode(Resp.self, from: data) else {
            throw BackendError.invalidResponse
        }
        return AccountStatus(
            email: r.email,
            active: r.active ?? false,
            activeUntil: r.active_until.flatMap(Self.parseISO),
            freeUsed: r.free_used ?? 0,
            freeQuota: r.free_quota ?? 0,
            freeRemaining: r.free_remaining ?? 0
        )
    }

    /// アクティベーションキーを登録（消費）する。成功すると利用権がアカウントに紐付く。
    /// 戻り値は利用権の期限（サーバーが返した場合）。失敗はサーバーの日本語メッセージ付きで throw。
    @discardableResult
    static func redeemActivationKey(_ code: String) async throws -> Date? {
        try? await AuthClient.ensureValidSession()
        var req = try authorizedRequest(path: ServerConfig.redeemPath)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        let trimmed = code.trimmingCharacters(in: .whitespacesAndNewlines)
        req.httpBody = try? JSONSerialization.data(withJSONObject: ["code": trimmed])
        let (data, status) = try await sendStatus(req)
        struct Resp: Decodable { let ok: Bool?; let active_until: String?; let error: String? }
        let r = try? JSONDecoder().decode(Resp.self, from: data)
        if status == 200, r?.ok == true {
            return r?.active_until.flatMap(Self.parseISO)
        }
        // サーバーの日本語メッセージ（「キーが見つかりません」等）を優先表示する。
        if let msg = r?.error, !msg.isEmpty { throw BackendError.message(msg) }
        throw mapStatus(status)
    }

    // MARK: - 使用実績のアカウント連携（#10）

    /// 1 日分の実績（サーバーへ送る形＝この端末の絶対値）。録音時間はミリ秒で送る。
    struct StatsDayPayload: Encodable {
        let day: String          // yyyy-MM-dd
        let chars: Int
        let sessions: Int
        let duration_ms: Int
    }

    /// この端末の日次実績をサーバーへ送る（POST /api/v1/stats/sync）。
    /// サーバーは (user_id, device_id, day) で絶対値 upsert するので、同じ値を再送しても
    /// 二重計上されない（冪等）。最大 60 日。失敗は throw（呼び出し側はだいたい無視する）。
    static func syncStats(days: [StatsDayPayload]) async throws {
        guard !days.isEmpty else { return }
        try? await AuthClient.ensureValidSession()
        var req = try authorizedRequest(path: ServerConfig.statsSyncPath)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        struct Body: Encodable { let days: [StatsDayPayload] }
        req.httpBody = try? JSONEncoder().encode(Body(days: Array(days.prefix(60))))
        _ = try await send(req)
    }

    /// アカウント横断の使用実績スナップショット（日次＋累計）。
    struct AccountStats {
        /// day(yyyy-MM-dd) → 端末横断で合算した 1 日分
        let daily: [String: DayStat]
        /// 累計文字数（端末横断）
        let totalCharacters: Int
        /// 累計回数（端末横断）
        let totalSessions: Int
        /// 累計録音秒数（端末横断）
        let totalRecordingSeconds: Double
    }

    /// アカウント横断の使用実績を取得する（GET /api/v1/stats）。
    /// サーバーが端末横断で日ごとに合算した daily と累計 totals を返す。ミリ秒は秒へ戻す。
    static func fetchStats() async throws -> AccountStats {
        try? await AuthClient.ensureValidSession()
        let req = try authorizedRequest(path: ServerConfig.statsPath)  // GET
        let data = try await send(req)
        struct Day: Decodable { let day: String; let chars: Int?; let sessions: Int?; let duration_ms: Int? }
        struct Totals: Decodable { let chars: Int?; let sessions: Int?; let duration_ms: Int? }
        struct Resp: Decodable { let daily: [Day]?; let totals: Totals? }
        guard let r = try? JSONDecoder().decode(Resp.self, from: data) else {
            throw BackendError.invalidResponse
        }
        var daily: [String: DayStat] = [:]
        for d in r.daily ?? [] {
            daily[d.day] = DayStat(
                characters: d.chars ?? 0,
                recordingSeconds: Double(d.duration_ms ?? 0) / 1000.0,
                sessions: d.sessions ?? 0)
        }
        return AccountStats(
            daily: daily,
            totalCharacters: r.totals?.chars ?? 0,
            totalSessions: r.totals?.sessions ?? 0,
            totalRecordingSeconds: Double(r.totals?.duration_ms ?? 0) / 1000.0)
    }

    /// ElevenLabs「正確性」プロキシで文字起こしする（WAV を multipart 送信）。
    static func transcribeElevenLabs(wav: Data, language: String) async throws -> String {
        try? await AuthClient.ensureValidSession()  // 失効間際なら先にリフレッシュ
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
        try? await AuthClient.ensureValidSession()  // 失効間際なら先にリフレッシュ
        var req = try authorizedRequest(path: ServerConfig.formatProxyPath)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: ["text": text])
        let data = try await send(req)
        struct Resp: Decodable { let text: String? }
        return (try? JSONDecoder().decode(Resp.self, from: data))?.text ?? text
    }

    /// アプリ内フィードバックを自社サーバーへ送る（認証は任意＝未ログインでも送れる）。
    /// ログイン済みなら Bearer を付けて user_id に紐付ける。サブスク有効性は問わない。
    /// 失敗は throw で返す（呼び出し側がユーザーに表示する）。
    static func submitFeedback(_ message: String) async throws {
        guard let url = ServerConfig.url(ServerConfig.feedbackPath) else {
            throw BackendError.invalidResponse
        }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.setValue(Keychain.deviceId(), forHTTPHeaderField: "x-device-id")
        req.setValue("mac", forHTTPHeaderField: "x-platform")
        // ログイン済みなら Bearer を付ける（未ログインでも送れるよう必須にしない）
        if let auth = Keychain.authSession() {
            req.setValue("Bearer \(auth.accessToken)", forHTTPHeaderField: "Authorization")
        }
        let version = Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "?"
        req.httpBody = try? JSONSerialization.data(
            withJSONObject: ["message": message, "app_version": version]
        )
        _ = try await send(req)
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

    /// リクエストを投げ、200 ならボディを返す。非 200 はステータスをエラーへ写す。
    /// 401 かつ Authorization 付きなら、一度だけトークンをリフレッシュして再試行する
    /// （失効間際を ensureValidSession で先回りしきれなかった場合の保険）。
    private static func send(_ req: URLRequest, allowRefresh: Bool = true) async throws -> Data {
        let data: Data
        let resp: URLResponse
        do {
            (data, resp) = try await session.data(for: req)
        } catch {
            throw BackendError.network(error)
        }
        guard let http = resp as? HTTPURLResponse else { throw BackendError.invalidResponse }
        if http.statusCode == 200 { return data }
        // 認証付きリクエストが 401 のときだけリフレッシュ＋再試行（匿名のフィードバック等は対象外）
        if http.statusCode == 401,
           allowRefresh,
           req.value(forHTTPHeaderField: "Authorization") != nil,
           (try? await AuthClient.refresh()) != nil,
           let auth = Keychain.authSession() {
            var retry = req
            retry.setValue("Bearer \(auth.accessToken)", forHTTPHeaderField: "Authorization")
            return try await send(retry, allowRefresh: false)
        }
        throw mapStatus(http.statusCode)
    }

    /// 認証付きで送信し (body, statusCode) を返す。非 200 でも throw しない
    /// （呼び出し側がレスポンス本文の error メッセージを使えるようにするため）。
    /// 401 のときだけ一度リフレッシュして再試行する。
    private static func sendStatus(_ req: URLRequest, allowRefresh: Bool = true) async throws -> (Data, Int) {
        let data: Data
        let resp: URLResponse
        do {
            (data, resp) = try await session.data(for: req)
        } catch {
            throw BackendError.network(error)
        }
        guard let http = resp as? HTTPURLResponse else { throw BackendError.invalidResponse }
        if http.statusCode == 401,
           allowRefresh,
           req.value(forHTTPHeaderField: "Authorization") != nil,
           (try? await AuthClient.refresh()) != nil,
           let auth = Keychain.authSession() {
            var retry = req
            retry.setValue("Bearer \(auth.accessToken)", forHTTPHeaderField: "Authorization")
            return try await sendStatus(retry, allowRefresh: false)
        }
        return (data, http.statusCode)
    }

    private static func mapStatus(_ code: Int) -> BackendError {
        switch code {
        case 401: return .unauthorized
        case 402: return .freeQuotaExhausted
        case 403: return .noSubscription
        case 409: return .deviceLimit
        case 429: return .rateLimited
        case 503: return .providerUnavailable
        default: return .server(code)
        }
    }

    /// ISO8601（小数秒あり/なし両対応）を Date へ。解釈不能なら nil。
    private static func parseISO(_ s: String) -> Date? {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let d = f.date(from: s) { return d }
        f.formatOptions = [.withInternetDateTime]
        return f.date(from: s)
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
