//
//  AuthClient.swift
//  ブラウザ経由ログインと Supabase セッションの維持（製品版・Phase 5 段階4）
//
//  フロー: アプリが state を生成 → 既定ブラウザで /auth/app?state=... を開く →
//  ユーザーがログイン → サーバーがワンタイムコードを発行 → deep link
//  voicekey://auth?code=&state= でアプリに戻る → exchange でコードをトークンに交換 →
//  Keychain に保存。トークンは URL に乗らず、ワンタイムコードの交換でのみ取得する。
//
//  このファイルは「ログイン URL 生成 / コード交換 / トークン更新」を担う。
//  URL スキーム登録・deep link 受信・ログイン UI 配線は後続の増分で行う。
//  サーバー契約は voicekey-site の app/{auth/app, api/v1/auth/{exchange,refresh}} と一致させる。
//

import Foundation
import Security

enum AuthClient {

    /// ログイン処理のエラー（ユーザー向け日本語メッセージ付き）
    enum AuthError: Error {
        case notLoggedIn        // リフレッシュするセッションが無い（未ログイン）
        case codeInvalid        // 410: ワンタイムコード失効/使用済み/不正
        case sessionExpired     // 401: refresh_token も失効 → 要再ログイン
        case server(Int)        // その他の非 200
        case network(Error)     // 通信失敗
        case invalidResponse    // レスポンス解釈不能

        var userMessage: String {
            switch self {
            case .notLoggedIn: return "ログインしていません"
            case .codeInvalid: return "ログインコードが無効か期限切れです。もう一度お試しください"
            case .sessionExpired: return "ログインの有効期限が切れました。再度ログインしてください"
            case .server(let code): return "サーバーエラー (HTTP \(code))"
            case .network(let e): return "通信に失敗しました: \(e.localizedDescription)"
            case .invalidResponse: return "サーバー応答を解釈できませんでした"
            }
        }
    }

    /// 短いタイムアウトの専用セッション（ログインは待たせすぎない）
    private static let session: URLSession = {
        let c = URLSessionConfiguration.ephemeral
        c.timeoutIntervalForRequest = 15
        c.timeoutIntervalForResource = 60
        c.waitsForConnectivity = false
        return URLSession(configuration: c)
    }()

    // MARK: - ログイン URL / state

    /// CSRF 対策のランダム state を生成する。
    /// 呼び出し側がこれを保持し、deep link で戻ってきた state と照合する。
    static func makeState() -> String {
        var bytes = [UInt8](repeating: 0, count: 16)
        _ = SecRandomCopyBytes(kSecRandomDefault, bytes.count, &bytes)
        return bytes.map { String(format: "%02x", $0) }.joined()
    }

    /// ブラウザで開くログイン URL を生成する。
    /// device_id / platform はサーバーがログインコードに紐付けるために渡す。
    static func makeLoginURL(state: String) -> URL? {
        guard var comps = URLComponents(string: ServerConfig.baseURL + ServerConfig.authAppPath) else {
            return nil
        }
        comps.queryItems = [
            URLQueryItem(name: "state", value: state),
            URLQueryItem(name: "device_id", value: Keychain.deviceId()),
            URLQueryItem(name: "platform", value: "mac"),
        ]
        return comps.url
    }

    // MARK: - コード交換 / トークン更新

    /// ワンタイムコードをトークンに交換し、Keychain に保存する。
    @discardableResult
    static func exchange(code: String) async throws -> AuthSession {
        guard let url = ServerConfig.url(ServerConfig.exchangePath) else {
            throw AuthError.invalidResponse
        }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: [
            "code": code,
            "device_id": Keychain.deviceId(),
            "platform": "mac",
        ])
        let data = try await send(req)
        let s = try decodeSession(data)
        _ = Keychain.saveAuthSession(s)
        return s
    }

    /// 進行中のリフレッシュを共有するためのゲート（並行リフレッシュ競合を防ぐ）。
    /// 同じ refresh_token を同時に複数回使うと GoTrue の rotation で
    /// `refresh_token_already_used` となり、reuse 検知で全セッションが revoke される
    /// （＝ログイン済みでも「ログインが必要です」になる）。それを避けるため、
    /// 同時に来た refresh は 1 本の Task に集約して結果を共有する。
    private static let refreshLock = NSLock()
    private static var inFlightRefresh: Task<AuthSession, Error>?

    /// 保存済み refresh_token で access_token を更新し、Keychain に保存する。
    /// 並行呼び出しは進行中の 1 本に集約する（同じ refresh_token の二重使用を防ぐ）。
    @discardableResult
    static func refresh() async throws -> AuthSession {
        // ロック操作は同期ヘルパに閉じ込める（async コンテキストで NSLock を直接触らない）。
        try await sharedRefreshTask().value
    }

    /// 進行中の更新があればそれを、無ければ新規に作って共有する（同期・ロック下で完結）。
    private static func sharedRefreshTask() -> Task<AuthSession, Error> {
        refreshLock.lock(); defer { refreshLock.unlock() }
        if let existing = inFlightRefresh { return existing }
        let task = Task {
            defer { clearInFlightRefresh() }  // 完了時に自分でクリア（次の更新を新規に作らせる）
            return try await performRefresh()
        }
        inFlightRefresh = task
        return task
    }

    /// 進行中フラグを下ろす（refresh Task の完了時に一度だけ呼ぶ）。
    private static func clearInFlightRefresh() {
        refreshLock.lock(); defer { refreshLock.unlock() }
        inFlightRefresh = nil
    }

    /// 実際のリフレッシュ処理（refresh() からのみ呼ぶ。直列化はラッパー側で保証する）。
    /// refresh_token も失効していれば（401）セッションを破棄して .sessionExpired を投げる。
    /// 409（refresh 競合）は他が更新済み＝セッションは有効なので破棄しない。
    private static func performRefresh() async throws -> AuthSession {
        guard let current = Keychain.authSession() else { throw AuthError.notLoggedIn }
        guard let url = ServerConfig.url(ServerConfig.refreshPath) else {
            throw AuthError.invalidResponse
        }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(
            withJSONObject: ["refresh_token": current.refreshToken]
        )
        let data: Data
        do {
            data = try await send(req)
        } catch AuthError.sessionExpired {
            _ = Keychain.clearAuthSession()  // 復帰不能 → 再ログインへ
            throw AuthError.sessionExpired
        }
        let s = try decodeSession(data)
        _ = Keychain.saveAuthSession(s)
        return s
    }

    /// 有効な access_token を保証する。失効 60 秒前を切っていればリフレッシュする。
    /// 認証付きリクエストの直前に呼ぶ（BackendClient からの配線は後続増分）。
    static func ensureValidSession() async throws {
        guard let s = Keychain.authSession() else { throw AuthError.notLoggedIn }
        if s.expiresAt - Date().timeIntervalSince1970 < 60 {
            _ = try await refresh()
        }
    }

    /// ログアウト（セッション破棄）。device_id は識別子なので残す。
    static func logout() {
        _ = Keychain.clearAuthSession()
        BackendClient.clearTokenCache()  // 別アカウントでの短命トークン再利用を防ぐ
    }

    // MARK: - 内部

    /// サーバー応答（access_token / refresh_token / expires_at(UNIX秒)）を AuthSession へ。
    private static func decodeSession(_ data: Data) throws -> AuthSession {
        struct Resp: Decodable {
            let access_token: String
            let refresh_token: String
            let expires_at: Double?
        }
        guard let r = try? JSONDecoder().decode(Resp.self, from: data) else {
            throw AuthError.invalidResponse
        }
        // expires_at 欠落時は GoTrue 既定 TTL（1時間後）を仮定する
        let exp = r.expires_at ?? (Date().timeIntervalSince1970 + 3600)
        return AuthSession(accessToken: r.access_token, refreshToken: r.refresh_token, expiresAt: exp)
    }

    /// リクエストを投げ、200 ならボディを返す。認証系のステータスを AuthError へ写す。
    private static func send(_ req: URLRequest) async throws -> Data {
        let data: Data
        let resp: URLResponse
        do {
            (data, resp) = try await session.data(for: req)
        } catch {
            throw AuthError.network(error)
        }
        guard let http = resp as? HTTPURLResponse else { throw AuthError.invalidResponse }
        switch http.statusCode {
        case 200: return data
        case 401: throw AuthError.sessionExpired
        case 410: throw AuthError.codeInvalid
        default: throw AuthError.server(http.statusCode)
        }
    }
}
