//
//  Keychain.swift
//  API キーの Keychain 保存・読み出し
//
//  サービス名・アカウント名は Python 版（keyring）と同一にしてあり、
//  Python 版で保存済みの API キーをそのまま読める。
//

import Foundation
import Security

enum Keychain {

    /// Python 版 keyring と互換のアカウント名
    private static let account = "default"

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
        return ProcessInfo.processInfo.environment[envVar]
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
        // 既存エントリがあれば更新、なければ追加
        let update: [String: Any] = [kSecValueData as String: data]
        var status = SecItemUpdate(query as CFDictionary, update as CFDictionary)
        if status == errSecItemNotFound {
            var add = query
            add[kSecValueData as String] = data
            status = SecItemAdd(add as CFDictionary, nil)
        }
        return status == errSecSuccess
    }
}
