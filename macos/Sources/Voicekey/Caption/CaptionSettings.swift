/// ライブ字幕の設定（UserDefaults 永続化）
///
/// 「翻訳エンジン」「モデル ID」「読み上げ ON/OFF」「原文表示 ON/OFF」「HUD の位置・大きさ」を保持する。
/// 秘密情報（API キー）はここに置かない（`APIKeyStore` が Keychain / 環境変数から読む）。
///
/// **ConfigStore（@MainActor）とは別に、この静的アクセサを置いている理由**:
/// 字幕の音声・翻訳経路は音声 IO スレッドや翻訳キューから設定を**同期的に**読む必要があり、
/// MainActor 隔離された ConfigStore を await できない。永続先のキーは ConfigStore と共通なので、
/// 値の正本はひとつ（UserDefaults）のまま保たれる。UI から触る分は ConfigStore 側の
/// @Published プロパティを使う（voicekey の設定作法に合わせるため）。
import Foundation

/// 翻訳に使うエンジン
enum TranslationEngine: String, CaseIterable {
    /// Apple 純正 Translation framework（オンデバイス・キー不要・無料）
    case apple
    /// Gemini API（LLM 品質。実測で聞き間違いの修復が最も良い）
    case gemini
    /// Groq API（OpenAI 互換・SSE ストリーミング対応・非常に高速）
    case groq

    /// メニューに出す表示名
    var displayName: String {
        switch self {
        case .apple: return "Apple 翻訳（オンデバイス・無料）"
        case .gemini: return "Gemini API"
        case .groq: return "Groq API"
        }
    }

    /// クラウド（API キーが要る・部分訳では呼ばない）か
    var isCloud: Bool { self != .apple }

    /// このエンジンが使う API キーのプロバイダー（Apple は不要なので nil）
    var apiProvider: APIProvider? {
        switch self {
        case .apple: return nil
        case .gemini: return .gemini
        case .groq: return .groq
        }
    }
}

/// どのアプリの音を拾うか
enum CaptureScopeMode: String, CaseIterable {
    /// 最前面のアプリだけ（既定。裏で鳴っている音楽を訳さない）
    case frontmost
    /// すべてのアプリ（自プロセスは除外）
    case all
}

/// 字幕設定へのアクセス点（スレッドを問わず同期的に読める）
enum CaptionSettings {

    /// UserDefaults のキー。voicekey 本体の設定と混ざらないよう `caption` 接頭辞で揃える。
    enum Key {
        static let scope = "captionScope"
        static let engine = "captionEngine"
        static let geminiModel = "captionGeminiModel"
        static let groqModel = "captionGroqModel"
        static let autoStart = "captionAutoStart"
        static let everStarted = "captionEverStarted"
        static let speak = "captionSpeak"
        static let showSource = "captionShowSource"
        static let hudScale = "captionHudScale"
    }

    // MARK: - 検証・ハーネス用の環境変数

    /// 字幕機能を丸ごと無効化する（`VOICEKEY_CAPTION_DISABLE=1`）
    ///
    /// `scripts/dev/fullscreen_helper.swift` のような**画素判定のハーネス**に字幕パネルが
    /// 映り込むと誤判定するため、ハーネス側からこの env を立てて字幕を出さないようにする。
    static var isDisabledByEnvironment: Bool {
        ProcessInfo.processInfo.environment["VOICEKEY_CAPTION_DISABLE"] == "1"
    }

    /// 翻訳器をモックに固定するか（`VOICEKEY_CAPTION_TRANSLATOR=mock`）
    ///
    /// 外部通信も言語モデルも無しで、HUD・読み上げ・流量制御の結線だけを検証するために使う。
    static var usesMockTranslator: Bool {
        ProcessInfo.processInfo.environment["VOICEKEY_CAPTION_TRANSLATOR"] == "mock"
    }

    // MARK: - 設定値

    /// キャプチャ対象のモード（既定は最前面のアプリだけ）
    ///
    /// 既定を `frontmost` にしているのは 2026-08-09 のユーザー明示要望による
    /// （裏で流している音楽が字幕に混ざるのを避けたい）。
    static var captureScopeMode: CaptureScopeMode {
        get {
            guard let raw = UserDefaults.standard.string(forKey: Key.scope),
                  let mode = CaptureScopeMode(rawValue: raw) else { return .frontmost }
            return mode
        }
        set { UserDefaults.standard.set(newValue.rawValue, forKey: Key.scope) }
    }

    /// 実際に使うモード（環境変数 `VOICEKEY_CAPTION_SCOPE` が優先）
    ///
    /// e2e / TTS ハーネスは `say` の音を拾う前提なので、スクリプト側で
    /// `VOICEKEY_CAPTION_SCOPE=all` を明示して従来モードで回す。
    static var effectiveCaptureScopeMode: CaptureScopeMode {
        if let raw = ProcessInfo.processInfo.environment["VOICEKEY_CAPTION_SCOPE"],
           let mode = CaptureScopeMode(rawValue: raw) {
            return mode
        }
        return captureScopeMode
    }

    /// 最前面アプリの代わりに対象へ固定する PID（環境変数 `VOICEKEY_CAPTION_TARGET_PID`。検証用）
    static var fixedCaptureTargetPID: pid_t? {
        guard let raw = ProcessInfo.processInfo.environment["VOICEKEY_CAPTION_TARGET_PID"],
              let pid = pid_t(raw), pid > 0 else { return nil }
        return pid
    }

    /// 使用する翻訳エンジン（既定 Apple）
    ///
    /// 既定をオンデバイスにしているのは、キー設定なしで初回から使えて課金も発生しないため。
    static var translationEngine: TranslationEngine {
        get {
            guard let raw = UserDefaults.standard.string(forKey: Key.engine),
                  let engine = TranslationEngine(rawValue: raw) else { return .apple }
            return engine
        }
        set { UserDefaults.standard.set(newValue.rawValue, forKey: Key.engine) }
    }

    /// 既定の Gemini モデル
    ///
    /// 6 エンジンのベンチ（2026-08-10）で、聞き間違いの修復 7/7・中央値 739ms・
    /// 10 文 0.163 円と質/速度/費用の釣り合いが最良だった。
    static let defaultGeminiModelID = "gemini-3.5-flash-lite"

    /// Gemini エンジンで使うモデル ID
    static var geminiModelID: String {
        get { storedModelID(Key.geminiModel, fallback: defaultGeminiModelID) }
        set { storeModelID(newValue, key: Key.geminiModel, fallback: defaultGeminiModelID) }
    }

    /// 既定の Groq モデル（production ラインナップから質と速度の釣り合いで選定）
    static let defaultGroqModelID = "llama-3.3-70b-versatile"

    /// Groq エンジンで使うモデル ID
    static var groqModelID: String {
        get { storedModelID(Key.groqModel, fallback: defaultGroqModelID) }
        set { storeModelID(newValue, key: Key.groqModel, fallback: defaultGroqModelID) }
    }

    /// 起動と同時に字幕を開始するか（既定 ON）
    ///
    /// 「Mac を開いている間ずっと翻訳」（2026-08-10 ユーザー指示）。voicekey 自体が
    /// ログイン起動するので、これで常時稼働になる。
    static var startsOnLaunch: Bool {
        get {
            guard UserDefaults.standard.object(forKey: Key.autoStart) != nil else { return true }
            return UserDefaults.standard.bool(forKey: Key.autoStart)
        }
        set { UserDefaults.standard.set(newValue, forKey: Key.autoStart) }
    }

    /// 一度でも字幕を開始したことがあるか
    ///
    /// **初回だけは自動開始しない**ための記録。初回起動時はシステム音声録音の TCC ダイアログが
    /// 出るため、voicekey が作り込んだ初回プロンプトの直列化（マイク→入力監視→アクセシビリティ）に
    /// 割り込ませない。ユーザーがメニューから「ライブ字幕を開始」した時点で true になり、
    /// 以後の起動では自動開始する。
    static var hasEverStarted: Bool {
        get { UserDefaults.standard.bool(forKey: Key.everStarted) }
        set { UserDefaults.standard.set(newValue, forKey: Key.everStarted) }
    }

    /// 起動時に自動開始してよいか（設定 ON かつ 2 回目以降かつハーネス無効化でない）
    static var shouldAutoStartNow: Bool {
        !isDisabledByEnvironment && startsOnLaunch && hasEverStarted
    }

    /// 訳文を読み上げるか（既定 OFF）
    ///
    /// 勝手に音を出さないための既定値。ユーザーがメニューで明示的に ON にする。
    static var speakTranslation: Bool {
        get { UserDefaults.standard.bool(forKey: Key.speak) }
        set { UserDefaults.standard.set(newValue, forKey: Key.speak) }
    }

    /// 原文（英語）を字幕に併記するか（既定 ON）
    static var showSourceText: Bool {
        get {
            guard UserDefaults.standard.object(forKey: Key.showSource) != nil else { return true }
            return UserDefaults.standard.bool(forKey: Key.showSource)
        }
        set { UserDefaults.standard.set(newValue, forKey: Key.showSource) }
    }

    /// 字幕 HUD の大きさ倍率（幅とフォントを同時に拡大縮小する）
    ///
    /// 1 つのつまみにしているのは、幅・フォント・高さを別々に持たせると
    /// ドラッグ操作の意味が分かりにくくなるため。高さは内容に追従する。
    static var hudScale: CGFloat {
        get {
            let stored = UserDefaults.standard.object(forKey: Key.hudScale) as? Double
            return CGFloat(stored ?? 1.0)
        }
        set { UserDefaults.standard.set(Double(newValue), forKey: Key.hudScale) }
    }

    // MARK: - 内部処理

    /// モデル ID を読む（空なら既定値）
    private static func storedModelID(_ key: String, fallback: String) -> String {
        let stored = UserDefaults.standard.string(forKey: key)?
            .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        return stored.isEmpty ? fallback : stored
    }

    /// モデル ID を書く（既定値と同じならキーごと消す）
    private static func storeModelID(_ value: String, key: String, fallback: String) {
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
        if trimmed.isEmpty || trimmed == fallback {
            UserDefaults.standard.removeObject(forKey: key)
        } else {
            UserDefaults.standard.set(trimmed, forKey: key)
        }
    }
}
