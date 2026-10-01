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

/// 設定 › API キー で入力できる項目（1 行 = 1 項目）。
/// アプリ自身の Keychain 項目名と、環境変数名（＝中央 Keychain の service 名）の対応表を 1 か所に置く。
enum ApiKeyItem: String, CaseIterable, Identifiable {
    case soniox
    case openai
    case azureKey
    case azureEndpoint
    case elevenlabs
    case groq
    case gemini

    var id: String { rawValue }

    /// アプリ自身の Keychain 項目の service 名。
    /// 文字起こし側の `Keychain.service(for:)` と同じ名前にして、既存の保存済みキーをそのまま読む。
    var appService: String {
        switch self {
        case .soniox: return "voicekey.Soniox"
        case .openai: return "voicekey.OpenAI"
        case .azureKey: return "voicekey.AzureSpeech"
        case .azureEndpoint: return "voicekey.AzureSpeechEndpoint"
        case .elevenlabs: return "voicekey.ElevenLabs"
        case .groq: return "voicekey.Groq"
        case .gemini: return "voicekey.Gemini"
        }
    }

    /// 環境変数名（＝中央 Keychain の service 名）
    var variableName: String {
        switch self {
        case .soniox: return "SONIOX_API_KEY"
        case .openai: return "OPENAI_API_KEY"
        case .azureKey: return "AZURE_SPEECH_KEY"
        case .azureEndpoint: return "AZURE_SPEECH_ENDPOINT"
        case .elevenlabs: return "ELEVENLABS_API_KEY"
        case .groq: return "GROQ_API_KEY"
        case .gemini: return "GEMINI_API_KEY"
        }
    }
}

enum Keychain {

    /// Python 版 keyring と互換のアカウント名
    private static let account = "default"

    /// 端末固有 ID 用サービス（識別子。認証子ではない）
    private static let deviceIdService = "voicekey.DeviceId"
    /// 認証セッション（Supabase JWT）用サービス
    private static let authService = "voicekey.Auth"
    /// Mac ⇄ Windows 履歴同期の共有トークン用サービス
    private static let syncTokenService = "voicekey.SyncToken"

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
        case .soniox: return "voicekey.Soniox"
        case .azureMAI: return "voicekey.AzureSpeech"
        }
    }

    /// プロセス内キャッシュ。Keychain アクセスは数十 ms かかり、
    /// 録音のたびに走るとレイテンシに直結するため
    private static var cache: [String: String] = [:]
    private static let lock = NSLock()
    /// キャッシュの世代。set / delete のたびに進める（lock で保護）。
    /// 読み出しはロックの外で Keychain・子プロセスを叩くので、その最中に保存・削除が走ると、
    /// 読み出し側が後から古い値でキャッシュを上書きしてしまう。読み始めの世代と一致するときだけ書き戻す
    private static var cacheGeneration: UInt64 = 0

    /// device_id の初回生成を直列化する（同時呼び出しで別々の ID を生成し、サーバーの
    /// 同時利用台数上限に誤って当たるのを防ぐ）。
    private static let deviceIdLock = NSLock()

    /// キーの探索順を決める純関数（アプリ項目 → 環境変数 → 中央 Keychain）。
    ///
    /// 文字起こし・整形（`apiKey(for:)`）と字幕翻訳（`APIKeyStore.load`）の両方がここを通り、
    /// 「設定 › API キー で入れた値が最優先」を全経路でそろえる。各段は遅延評価なので、
    /// 手前で見つかれば後段（中央 Keychain の子プロセス起動など）は走らない。空文字は未設定扱い。
    static func resolve(
        app: () -> String?,
        env: () -> String?,
        central: () -> String?
    ) -> (value: String, source: APIKeySource)? {
        if let value = app(), !value.isEmpty { return (value, .app) }
        if let value = env(), !value.isEmpty { return (value, .environment) }
        if let value = central(), !value.isEmpty { return (value, .centralKeychain) }
        return nil
    }

    /// キャッシュを引く。外れたときは、あとで `storeInCache` に渡す読み始めの世代も返す
    private static func cachedValue(_ key: String) -> (value: String?, generation: UInt64) {
        lock.lock(); defer { lock.unlock() }
        return (cache[key], cacheGeneration)
    }

    /// 読み出した値をキャッシュへ書き戻す。読み始めから set / delete が挟まっていたら捨てる
    private static func storeInCache(_ key: String, _ value: String, ifGeneration generation: UInt64) {
        lock.lock(); defer { lock.unlock() }
        guard cacheGeneration == generation else { return }
        cache[key] = value
    }

    /// 録音開始のクリティカルパス向けに、キーが「あると分かっているか」だけを返す。
    ///
    /// Keychain（数十 ms）にも中央 Keychain の子プロセスにも触れず、キャッシュ済みか環境変数に
    /// あるときだけ true。false は「無い」か「まだ読んでいない」のどちらか（`apiKey(for:)` を
    /// 裏で一度呼べばキャッシュに載り、次から true になる）。
    static func isApiKeyKnown(for backend: Backend) -> Bool {
        guard backend != .appleLocal else { return false }
        if cachedValue(service(for: backend)).value != nil { return true }
        guard let envVar = keyVariableName(for: backend),
              let value = ProcessInfo.processInfo.environment[envVar] else { return false }
        return !value.isEmpty
    }

    /// API キーを取得する（アプリ Keychain → 環境変数 → 中央 Keychain の順。未設定なら nil）
    static func apiKey(for backend: Backend) -> String? {
        // ローカル（Apple）はオンデバイス処理なのでキーが要らない。Keychain も一切読まない
        guard backend != .appleLocal else { return nil }
        let svc = service(for: backend)

        let cached = cachedValue(svc)
        if let value = cached.value { return value }

        // appleLocal は上の guard で弾かれるため nil にはならない（網羅性のためだけの既定値）
        let envVar = keyVariableName(for: backend) ?? ""
        // 注意: 以前はアプリ項目を読めたとき「読めた値で書き直す」自己修復移行（delete→add）を行っていたが、
        // Apple Development 証明書への移行完了（partition_id に teamid が入った状態）後は撤去した。
        // 起動のたびに項目を作り直すと、ad-hoc 署名の実行（debug ビルド・検証ハーネス等）が
        // 一度でも鍵を読んだ時点で項目の所有が cdhash 固定に退行し、次の正規ビルドで
        // パスワード要求ダイアログが再発する原因になるため（2026-06-12 実測）。
        // もし承認ダイアログが再発した場合は、設定画面からキーを 1 回再保存すれば
        // 現アプリ所有の項目に作り直される（保存経路の delete→add は維持している）
        //
        // 中央 Keychain（service = 環境変数名 / account = shared）は、プロバイダーごとにキーを
        // 1 本だけ発行して全プロジェクトで使い回すための共通置き場（2026-08-09 導入）。
        guard let found = resolve(
            app: { read(service: svc) },
            env: { ProcessInfo.processInfo.environment[envVar] },
            central: { readCentral(service: envVar) }
        ) else {
            // 配布ビルドにプロバイダーキーは埋め込まない。どこにも無ければ未設定として nil を返す。
            return nil
        }
        switch found.source {
        case .environment:
            // 環境変数は開発時用。プロセス内で変わりうるのでキャッシュしない（従来どおり）
            return found.value
        case .centralKeychain:
            // 値は出さない（取得元と末尾 4 桁のみ）。キーがどこから来たかを後から追えないと
            // 「キー未設定」系の不具合を実機で切り分けられないため .notice で残す
            centralLogger.notice(
                "中央 Keychain から取得 service=\(envVar, privacy: .public) suffix=\(String(found.value.suffix(4)), privacy: .public)"
            )
        case .app:
            break
        }
        storeInCache(svc, found.value, ifGeneration: cached.generation)
        return found.value
    }

    /// バックエンドのキーの変数名（環境変数名＝中央 Keychain の service 名）。
    /// キー読み出しと「未設定です」の案内文の両方がここを引く（名前の対応表を 1 か所に保つため）。
    /// ローカル（Apple）はキーを使わないので nil
    static func keyVariableName(for backend: Backend) -> String? {
        switch backend {
        case .openai, .openaiLive: return "OPENAI_API_KEY"
        case .groq: return "GROQ_API_KEY"
        case .elevenlabs: return "ELEVENLABS_API_KEY"
        case .deepgram: return "DEEPGRAM_API_KEY"
        case .soniox: return "SONIOX_API_KEY"
        case .azureMAI: return "AZURE_SPEECH_KEY"
        case .appleLocal: return nil
        }
    }

    /// Microsoft MAI（Azure Speech）の接続先エンドポイントを取得する（アプリ Keychain → 環境変数 → 中央 Keychain）。
    ///
    /// Azure はリソースごとに URL が違うので、キーとは別に `AZURE_SPEECH_ENDPOINT` を持つ
    /// （秘密ではないが置き場所をキーと揃え、設定 › API キー からも入れられるようにする）。
    static func azureSpeechEndpoint() -> String? {
        let item = ApiKeyItem.azureEndpoint
        let cached = cachedValue(item.appService)
        if let value = cached.value { return value }

        guard let found = lookup(item) else { return nil }
        storeInCache(item.appService, found.value, ifGeneration: cached.generation)
        return found.value
    }

    /// 項目の値と取得元を、アプリ → 環境変数 → 中央 Keychain の順で探す（キャッシュを通さない）。
    /// 値を返すのは実際に使う経路のためだけ。設定画面は `apiKeySource(for:)` で取得元だけを見る。
    static func lookup(_ item: ApiKeyItem) -> (value: String, source: APIKeySource)? {
        resolve(
            app: { read(service: item.appService) },
            env: { ProcessInfo.processInfo.environment[item.variableName] },
            central: { readCentral(service: item.variableName) }
        )
    }

    /// アプリ自身の Keychain 項目だけを読む（字幕側の `APIKeyStore` が探索順をそろえるために使う）
    static func appValue(for item: ApiKeyItem) -> String? {
        read(service: item.appService)
    }

    /// 項目がどこから読めるか（設定画面の状態表示用。値は返さない。未設定なら nil）。
    /// Keychain・子プロセスに触れるので SwiftUI の body からは呼ばない。
    static func apiKeySource(for item: ApiKeyItem) -> APIKeySource? {
        lookup(item)?.source
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

    /// API キー（または Azure の接続先）をアプリ自身の Keychain 項目へ保存する。
    /// 中央 Keychain には書かない。保存直後から全経路で使われるようキャッシュも差し替える
    /// （`apiKey(for:)` のキャッシュキーは `service(for:)` ＝ `appService` と同じ名前）。
    /// 値は `APIKeyStore.sanitize` で正規化してから保存する（`SONIOX_API_KEY=...` のような
    /// 前置き・引用符付きで貼られても、どの保存経路からでも同じ形で入るように）。空になれば保存しない。
    @discardableResult
    static func setApiKey(_ key: String, for item: ApiKeyItem) -> Bool {
        let value = APIKeyStore.sanitize(key)
        guard !value.isEmpty else { return false }
        let ok = write(service: item.appService, value: value)
        if ok {
            // 書き込み後に世代を進める＝書き込み前から走っていた読み出しは古い値を書き戻さない
            lock.lock(); cacheGeneration &+= 1; cache[item.appService] = value; lock.unlock()
        }
        return ok
    }

    /// アプリ自身の Keychain 項目だけを削除する（環境変数・中央 Keychain には触らない）。
    /// キャッシュも捨てるので、次の読み出しから環境変数 → 中央 Keychain へ自然に戻る。
    @discardableResult
    static func deleteApiKey(for item: ApiKeyItem) -> Bool {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: item.appService,
            kSecAttrAccount as String: account,
        ]
        let status = SecItemDelete(query as CFDictionary)
        // 削除の後で世代を進めてキャッシュを捨てる。先に捨てると、削除前の項目を読んだ読み出しが
        // 同じ世代のまま古い値を書き戻し、消したはずのキーが使われ続けるため
        lock.lock(); cacheGeneration &+= 1; cache.removeValue(forKey: item.appService); lock.unlock()
        return status == errSecSuccess || status == errSecItemNotFound
    }

    // MARK: - 履歴同期トークン

    /// 履歴同期トークンを取得する（アプリ Keychain → 中央 Keychain → 環境変数）。
    static func syncToken() -> String? {
        let cached = cachedValue(syncTokenService)
        if let value = cached.value { return value }

        let value = read(service: syncTokenService)
            ?? readCentral(service: "VOICEKEY_SYNC_TOKEN")
            ?? ProcessInfo.processInfo.environment["VOICEKEY_SYNC_TOKEN"]
        guard let value, !value.isEmpty else { return nil }
        storeInCache(syncTokenService, value, ifGeneration: cached.generation)
        return value
    }

    /// 履歴同期トークンをアプリ Keychain へ保存する。
    @discardableResult
    static func setSyncToken(_ token: String) -> Bool {
        let ok = write(service: syncTokenService, value: token)
        if ok {
            lock.lock(); cacheGeneration &+= 1; cache[syncTokenService] = token; lock.unlock()
        }
        return ok
    }

    /// 履歴同期トークンをアプリ Keychain から削除する。
    @discardableResult
    static func deleteSyncToken() -> Bool {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: syncTokenService,
            kSecAttrAccount as String: account,
        ]
        let status = SecItemDelete(query as CFDictionary)
        // deleteApiKey と同じ理由で、削除の後に世代を進めてキャッシュを捨てる
        lock.lock(); cacheGeneration &+= 1; cache.removeValue(forKey: syncTokenService); lock.unlock()
        return status == errSecSuccess || status == errSecItemNotFound
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
