/// ライブ字幕が使う API キー（プロバイダー別）の探索
///
/// 探索順（文字起こし側の `Keychain.apiKey(for:)` と同じ。順序は `Keychain.resolve` の 1 か所で決める）:
///   1. **アプリ自身の Keychain 項目**（設定 › API キー で保存した値。`voicekey.Gemini` / `voicekey.Groq` / `voicekey.OpenAI`）
///   2. 環境変数（`GEMINI_API_KEY` / `GROQ_API_KEY` / `OPENAI_API_KEY`）
///   3. **中央 Keychain**（service = 変数名 / account = `shared`）
///
/// 中央 Keychain は `/usr/bin/security` を子プロセスで起動して読む。SecItem で直読みすると
/// **項目ごとにアクセス承認ダイアログが出る**ため（`security` コマンドは既に許可済み）。
///
/// **ここからは書き込みを一切しない**（保存は設定 › API キー の `Keychain.setApiKey` だけ）。
/// voicekey 本体の Keychain 項目は読むだけで作り直さない（作り直すとアクセス許可がリセットされ、
/// 毎回ダイアログが出るようになるため）。
/// キーが無くても既定エンジン（Apple のオンデバイス翻訳）で字幕は完全に動く。
/// キーの値は絶対にログ・UI へ出さない（出すのは「どこから読めたか」と末尾 4 桁のみ）。
import Foundation
import OSLog

/// キーを使うプロバイダー（raw value がそのまま環境変数名・中央 Keychain のサービス名）
enum APIProvider: String, CaseIterable {
    case gemini = "GEMINI_API_KEY"
    case groq = "GROQ_API_KEY"
    case openai = "OPENAI_API_KEY"

    /// 画面に出す名前
    var displayName: String {
        switch self {
        case .gemini: return "Gemini"
        case .groq: return "Groq"
        case .openai: return "OpenAI"
        }
    }

    /// 設定 › API キー の項目（アプリ Keychain 項目名の対応）。Groq / OpenAI は文字起こしと同じ項目を共用する
    var keyItem: ApiKeyItem {
        switch self {
        case .gemini: return .gemini
        case .groq: return .groq
        case .openai: return .openai
        }
    }
}

/// キーがどこから読めたか
enum APIKeySource: String {
    case app = "アプリ"
    case environment = "環境変数"
    case centralKeychain = "共有Keychain"

    /// メニュー等に出す短い表記
    var displayName: String { rawValue }

    /// 設定 › API キー の状態表示
    var statusLabel: String {
        switch self {
        case .app: return "アプリに保存済み"
        case .environment: return "環境変数から読込"
        case .centralKeychain: return "中央 Keychain から読込"
        }
    }
}

/// API キーの読み出し（読み取り専用）
enum APIKeyStore {

    /// 中央 Keychain のアカウント名（service は変数名そのもの）
    static let centralKeychainAccount = "shared"
    /// 既定プロバイダー
    static let defaultProvider: APIProvider = .gemini

    private static let logger = makeCaptionLogger("APIKeyStore")

    /// 指定プロバイダーの API キーを探索する
    ///
    /// - Parameter provider: 対象プロバイダー（既定は Gemini）
    /// - Returns: 見つかったキーと取得元。どこにも無ければ nil
    static func load(_ provider: APIProvider = defaultProvider) -> (key: String, source: APIKeySource)? {
        guard let found = Keychain.resolve(
            app: { Keychain.appValue(for: provider.keyItem).map(sanitize) },
            env: { ProcessInfo.processInfo.environment[provider.rawValue].map(sanitize) },
            central: { readFromCentralKeychain(provider).map(sanitize) }
        ) else { return nil }
        return (found.value, found.source)
    }

    /// キーが設定済みかどうか（値は返さない）
    static func isConfigured(_ provider: APIProvider = defaultProvider) -> Bool { load(provider) != nil }

    /// 表示用のマスク文字列を作る（末尾 4 桁のみ見せる）
    ///
    /// - Parameter key: 対象のキー
    /// - Returns: 例 `••••abcd`
    static func masked(_ key: String) -> String {
        let suffix = key.count >= 4 ? String(key.suffix(4)) : String(repeating: "•", count: max(0, key.count))
        return "••••\(suffix)"
    }

    /// 貼り付け・環境変数由来のゴミを落とす
    ///
    /// `export KEY=xxx` 形式のまま入っていることがあるため、右辺だけを取り出して
    /// 前後の空白・改行・引用符を除去する。
    /// 前置きは設定 › API キー の全項目（`ApiKeyItem.variableName`）で外す。字幕の 3 社だけに
    /// 絞っていると、`SONIOX_API_KEY=...` 等をそのまま貼ったとき前置きごと保存されて 401 になるため。
    static func sanitize(_ raw: String) -> String {
        var value = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        if value.hasPrefix("export ") { value = String(value.dropFirst("export ".count)) }
        for item in ApiKeyItem.allCases {
            let prefix = "\(item.variableName)="
            if value.hasPrefix(prefix) {
                value = String(value.dropFirst(prefix.count))
                break
            }
        }
        value = value.trimmingCharacters(in: .whitespacesAndNewlines)
        for quote in ["\"", "'"] where value.hasPrefix(quote) && value.hasSuffix(quote) && value.count >= 2 {
            value = String(value.dropFirst().dropLast())
        }
        return value.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    // MARK: - 内部処理

    /// 中央 Keychain（service = 変数名 / account = shared）から読む
    ///
    /// `/usr/bin/security` を子プロセスで起動する。SecItem で直読みすると項目ごとに
    /// アクセス承認ダイアログが出てしまい、無人実行できなくなるため。
    ///
    /// - Parameter provider: 対象プロバイダー
    /// - Returns: 取れた値（無ければ nil）。値はログに出さない。
    private static func readFromCentralKeychain(_ provider: APIProvider) -> String? {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/security")
        process.arguments = [
            "find-generic-password", "-s", provider.rawValue, "-a", centralKeychainAccount, "-w",
        ]
        let output = Pipe()
        process.standardOutput = output
        process.standardError = Pipe()
        do {
            try process.run()
        } catch {
            logger.notice("security の起動に失敗しました provider=\(provider.rawValue, privacy: .public)")
            return nil
        }
        let data = output.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        guard process.terminationStatus == 0 else { return nil }
        let value = String(data: data, encoding: .utf8)?
            .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        return value.isEmpty ? nil : value
    }
}
