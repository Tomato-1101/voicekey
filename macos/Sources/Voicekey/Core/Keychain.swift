//
//  Keychain.swift
//  API キーの Keychain 保存・読み出し
//
//  サービス名・アカウント名は Python 版（keyring）と同一にしてあり、
//  Python 版で保存済みの API キーをそのまま読める。
//

import Foundation
import Security

/// 保存する認証セッション（Supabase）。
/// expiresAt は access_token の失効時刻（UNIX エポック秒）。
/// JSON のフィールド名は Windows 版（secrets.py）と揃える（snake_case）。
struct AuthSession: Codable {
    var accessToken: String
    var refreshToken: String
    var expiresAt: Double

    enum CodingKeys: String, CodingKey {
        case accessToken = "access_token"
        case refreshToken = "refresh_token"
        case expiresAt = "expires_at"
    }
}

enum Keychain {

    /// Python 版 keyring と互換のアカウント名
    private static let account = "default"

    /// 端末固有 ID 用サービス（識別子。認証子ではない）
    private static let deviceIdService = "voicekey.DeviceId"
    /// 認証セッション（Supabase JWT）用サービス
    private static let authService = "voicekey.Auth"

    /// バックエンドごとのサービス識別子（Python 版と同一）
    static func service(for backend: Backend) -> String {
        switch backend {
        case .openai: return "voicekey.OpenAI"
        case .groq: return "voicekey.Groq"
        case .elevenlabs: return "voicekey.ElevenLabs"
        case .deepgram: return "voicekey.Deepgram"
        }
    }

    /// プロセス内キャッシュ。Keychain アクセスは数十 ms かかり、
    /// 録音のたびに走るとレイテンシに直結するため
    private static var cache: [String: String] = [:]
    private static let lock = NSLock()

    /// device_id の初回生成を直列化する（同時呼び出しで別々の ID を生成し、サーバーの
    /// 同時利用台数上限に誤って当たるのを防ぐ）。
    private static let deviceIdLock = NSLock()

    /// API キーを取得する（Keychain → 環境変数の順。未設定なら nil）
    static func apiKey(for backend: Backend) -> String? {
        let svc = service(for: backend)

        lock.lock()
        if let cached = cache[svc] {
            lock.unlock()
            return cached
        }
        lock.unlock()

        if let value = read(service: svc) {
            // 注意: 以前はここで「読めた値で書き直す」自己修復移行（delete→add）を行っていたが、
            // Apple Development 証明書への移行完了（partition_id に teamid が入った状態）後は撤去した。
            // 起動のたびに項目を作り直すと、ad-hoc 署名の実行（debug ビルド・検証ハーネス等）が
            // 一度でも鍵を読んだ時点で項目の所有が cdhash 固定に退行し、次の正規ビルドで
            // パスワード要求ダイアログが再発する原因になるため（2026-06-12 実測）。
            // もし承認ダイアログが再発した場合は、設定画面からキーを 1 回再保存すれば
            // 現アプリ所有の項目に作り直される（保存経路の delete→add は維持している）
            lock.lock(); cache[svc] = value; lock.unlock()
            return value
        }
        // 環境変数フォールバック（開発時用）
        let envVar: String
        switch backend {
        case .openai: envVar = "OPENAI_API_KEY"
        case .groq: envVar = "GROQ_API_KEY"
        case .elevenlabs: envVar = "ELEVENLABS_API_KEY"
        case .deepgram: envVar = "DEEPGRAM_API_KEY"
        }
        if let env = ProcessInfo.processInfo.environment[envVar], !env.isEmpty {
            return env
        }
        // 配布ビルドにプロバイダーキーは埋め込まない（製品版はサーバー経由）。
        // Keychain にも環境変数にも無ければ未設定として nil を返す。
        return nil
    }

    /// API キーを保存する
    @discardableResult
    static func setApiKey(_ key: String, for backend: Backend) -> Bool {
        let svc = service(for: backend)
        let ok = write(service: svc, value: key)
        if ok {
            lock.lock(); cache[svc] = key; lock.unlock()
        }
        return ok
    }

    /// API キーを削除する
    @discardableResult
    static func deleteApiKey(for backend: Backend) -> Bool {
        let svc = service(for: backend)
        lock.lock(); cache.removeValue(forKey: svc); lock.unlock()
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: svc,
            kSecAttrAccount as String: account,
        ]
        let status = SecItemDelete(query as CFDictionary)
        return status == errSecSuccess || status == errSecItemNotFound
    }

    /// キーが設定済みかどうか
    static func hasApiKey(for backend: Backend) -> Bool {
        apiKey(for: backend) != nil
    }

    // MARK: - 製品版バックエンド接続・認証（device_id / Supabase セッション）

    /// 端末固有 ID を取得する（無ければ生成して保存）。
    /// これは識別子であって認証子ではない（認証は Supabase JWT で行う）。
    /// サーバー側の同時台数上限・悪用検知のために使う。
    static func deviceId() -> String {
        // ロック保持下で「読み直し → 無ければ生成」を直列化する。同時に複数スレッドが
        // 入っても、最初の 1 本だけが生成・保存し、後続はその値を読み直して共有する。
        deviceIdLock.lock(); defer { deviceIdLock.unlock() }
        if let existing = read(service: deviceIdService) {
            return existing
        }
        let newId = UUID().uuidString
        _ = write(service: deviceIdService, value: newId)
        return newId
    }

    /// 保存済みの認証セッションを取得する（未保存・破損時は nil）
    static func authSession() -> AuthSession? {
        guard let json = read(service: authService),
              let data = json.data(using: .utf8) else { return nil }
        return try? JSONDecoder().decode(AuthSession.self, from: data)
    }

    /// 認証セッションを保存する
    @discardableResult
    static func saveAuthSession(_ session: AuthSession) -> Bool {
        guard let data = try? JSONEncoder().encode(session),
              let json = String(data: data, encoding: .utf8) else { return false }
        return write(service: authService, value: json)
    }

    /// 認証セッションを削除する（ログアウト時）。device_id は識別子なので残す
    @discardableResult
    static func clearAuthSession() -> Bool {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: authService,
            kSecAttrAccount as String: account,
        ]
        let status = SecItemDelete(query as CFDictionary)
        return status == errSecSuccess || status == errSecItemNotFound
    }

    // MARK: - 低レベル操作

    private static func read(service: String) -> String? {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
        ]
        var result: AnyObject?
        let status = SecItemCopyMatching(query as CFDictionary, &result)
        guard status == errSecSuccess,
              let data = result as? Data,
              let value = String(data: data, encoding: .utf8),
              !value.isEmpty else {
            return nil
        }
        return value
    }

    private static func write(service: String, value: String) -> Bool {
        let data = Data(value.utf8)
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
        // SecItemUpdate だと他アプリ（Python 版 keyring・旧署名ビルド）所有の項目が
        // ACL ごと残り、現アプリは読み取りのたびに承認を求められる。
        // 削除 → 新規追加にすることで項目を常に現アプリが作成して所有権を取る
        SecItemDelete(query as CFDictionary)
        var add = query
        add[kSecValueData as String] = data
        let status = SecItemAdd(add as CFDictionary, nil)
        return status == errSecSuccess
    }
}
