//
//  Keychain.swift
//  API キーの Keychain 保存・読み出し
//
//  サービス名・アカウント名は Python 版（keyring）と同一にしてあり、
//  Python 版で保存済みの API キーをそのまま読める。
//

import Foundation
import OSLog
import Security

/// 中央 Keychain からの取得を記録するロガー（値は出さない）
private let centralLogger = Logger(subsystem: "com.voicekey.app", category: "Keychain")

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
        // openaiLive（gpt-live-transcribe）は同じ OpenAI のキーを使うので項目を共用する
        // （設定画面で OpenAI キーを 1 回入れれば REST もライブも動く）
        case .openai, .openaiLive: return "voicekey.OpenAI"
        case .groq: return "voicekey.Groq"
        case .elevenlabs: return "voicekey.ElevenLabs"
        case .deepgram: return "voicekey.Deepgram"
        // ローカル（Apple）はキーを使わない。項目は作らない（apiKey が先に nil を返す）
        case .appleLocal: return "voicekey.AppleLocal"
        // Gemini は中央 Keychain（GEMINI_API_KEY / account=shared）の 1 本を読むだけで、
        // アプリ固有の項目は作らない（字幕の Gemini 翻訳と同じ鍵を共有する）
        case .gemini: return "voicekey.Gemini"
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
        // ローカル（Apple）はオンデバイス処理なのでキーが要らない。Keychain も一切読まない
        guard backend != .appleLocal else { return nil }
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
        case .openai, .openaiLive: envVar = "OPENAI_API_KEY"
        case .groq: envVar = "GROQ_API_KEY"
        case .elevenlabs: envVar = "ELEVENLABS_API_KEY"
        case .deepgram: envVar = "DEEPGRAM_API_KEY"
        // 上の guard で弾かれるためここには来ない（網羅性のためだけの分岐）
        case .appleLocal: envVar = ""
        case .gemini: envVar = "GEMINI_API_KEY"
        }
        if let env = ProcessInfo.processInfo.environment[envVar], !env.isEmpty {
            return env
        }
        // 中央 Keychain（service = 環境変数名 / account = shared）。
        // プロバイダーごとにキーを 1 本だけ発行して全プロジェクトで使い回すための共通置き場
        // （2026-08-09 導入。字幕側の APIKeyStore は既にここを読んでいる）。
        if let central = readCentral(service: envVar) {
            // 値は出さない（取得元と末尾 4 桁のみ）。キーがどこから来たかを後から追えないと
            // 「キー未設定」系の不具合を実機で切り分けられないため .notice で残す
            centralLogger.notice(
                "中央 Keychain から取得 service=\(envVar, privacy: .public) suffix=\(String(central.suffix(4)), privacy: .public)"
            )
            lock.lock(); cache[svc] = central; lock.unlock()
            return central
        }
        // 配布ビルドにプロバイダーキーは埋め込まない（製品版はサーバー経由）。
        // どこにも無ければ未設定として nil を返す。
        return nil
    }

    /// 中央 Keychain（service = 環境変数名 / account = `shared`）から読む
    ///
    /// `/usr/bin/security` を子プロセスで起動する。SecItem で直読みすると項目ごとに
    /// アクセス承認ダイアログが出てしまうため（字幕側の `APIKeyStore` と同じ方式）。
    private static func readCentral(service: String) -> String? {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/security")
        process.arguments = ["find-generic-password", "-s", service, "-a", "shared", "-w"]
        let output = Pipe()
        process.standardOutput = output
        process.standardError = Pipe()
        do {
            try process.run()
        } catch {
            return nil
        }
        let data = output.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        guard process.terminationStatus == 0 else { return nil }
        let value = String(data: data, encoding: .utf8)?
            .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        return value.isEmpty ? nil : value
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
        // personal エディションはアカウント/バックエンドを一切使わない。旧 release DIST 利用時に
        // 残った認証トークンが Keychain にあってもログイン扱いにせず nil を返す＝起動時の
        // 利用権確認・warm ループ・短命トークン取得などのサーバー往復を根本から発生させない
        // （BackendClient.isLoggedIn も本メソッド依存なので連動して false になる）。
        if EmbeddedKeys.isPersonal { return nil }
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

    /// Keychain の低レベル操作（read/delete/add）。テスト容易性のため注入可能にする
    /// （本物の Security 関数に触れずに write の手順を検証できる＝テストでパスワード
    /// ダイアログを出さない）。
    struct Ops {
        var read: (String) -> String?
        var delete: (String) -> Void
        var add: (String, String) -> Bool
    }

    /// 本番用 SecItem 操作
    private static let realOps = Ops(
        read: { read(service: $0) },
        delete: { svc in
            let query: [String: Any] = [
                kSecClass as String: kSecClassGenericPassword,
                kSecAttrService as String: svc,
                kSecAttrAccount as String: account,
            ]
            SecItemDelete(query as CFDictionary)
        },
        add: { svc, val in
            var query: [String: Any] = [
                kSecClass as String: kSecClassGenericPassword,
                kSecAttrService as String: svc,
                kSecAttrAccount as String: account,
            ]
            query[kSecValueData as String] = Data(val.utf8)
            return SecItemAdd(query as CFDictionary, nil) == errSecSuccess
        }
    )

    private static func write(service: String, value: String) -> Bool {
        write(service: service, value: value, ops: realOps)
    }

    /// テスト可能な write 本体（ops を注入）。
    ///
    /// SecItemUpdate ではなく delete→add を使う: SecItemUpdate だと他アプリ
    /// （Python 版 keyring・旧署名ビルド）所有の項目が ACL ごと残り、現アプリは読み取りの
    /// たびに承認ダイアログを求められる（2026-06-12 実測）。delete→add で項目を常に現アプリが
    /// 作成して所有権を取る。
    ///
    /// ただし delete 後に add が失敗すると旧資格情報まで失う（#17）ため、書き込み前に旧値を
    /// 控え、add 失敗時は旧値の復元を試みる（ベストエフォート）。所有権（delete→add）と
    /// 資格情報の保全を両立させる。
    static func write(service: String, value: String, ops: Ops) -> Bool {
        let previous = ops.read(service)
        ops.delete(service)
        if ops.add(service, value) {
            return true
        }
        // 追加失敗: 旧値があれば復元して資格情報の消失を防ぐ
        if let previous {
            _ = ops.add(service, previous)
        }
        return false
    }
}
