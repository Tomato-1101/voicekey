//
//  DictationTranslator.swift
//  「翻訳して入力」— 文字起こしの最終テキストを訳してから貼り付ける
//
//  本命の用途は「日本語で話す → 英語が入力される」。どのバックエンド（クラウド/ローカル）の
//  結果にも同じように効かせるため、貼り付け直前の最終テキストに **1 回だけ** 適用する
//  （部分訳はしない＝クラウドを高頻度に叩かない、という字幕側と同じ恒久方針）。
//
//  エンジンは 2 つだけ:
//    - Apple（既定）: オンデバイス翻訳。無料・オフライン・キー不要。
//    - Groq        : LLM 翻訳。字幕の `GroqTranslator` をそのまま使う。
//  Gemini は選択肢に出さない（課金を勝手に発生させない恒久方針）。
//
//  失敗したら必ず原文を返す（テキストを失わないことが最優先）。
//

import Foundation
import os.log
@preconcurrency import Translation

private let log = Logger(subsystem: "com.voicekey.app", category: "dictation.translate")

/// 「翻訳して入力」に使うエンジン
enum DictationTranslationEngine: String, Codable, CaseIterable, Identifiable {
    /// Apple のオンデバイス翻訳（既定・無料・オフライン）
    case apple
    /// Groq の LLM 翻訳（口語のニュアンスに強いが API キーと通信が要る）
    case groq

    var id: String { rawValue }

    var label: String {
        switch self {
        case .apple: return "Apple 翻訳（オンデバイス・無料）"
        case .groq: return "Groq（LLM・APIキーが必要）"
        }
    }
}

/// 「翻訳して入力」の設定へのアクセス点
///
/// 字幕の `CaptionSettings` と同じ考え方で、UserDefaults を正本にして
/// MainActor 外（貼り付けパイプライン）からも同期的に読めるようにする。
/// UI は `ConfigStore` の @Published ミラー経由で触る。
enum DictationTranslation {

    enum Key {
        static let enabled = "translateInputEnabled"
        static let target = "translateInputTarget"
        static let engine = "translateInputEngine"
    }

    /// 出力言語の選択肢（Apple のオンデバイス翻訳が対応する主要言語から絞る）
    ///
    /// 既定は英語。増やしすぎると選ぶのが面倒になるだけなので、実際に使う数個に留める。
    static let targetLanguages: [(code: String, label: String)] = [
        ("en", "英語"),
        ("zh-Hans", "中国語（簡体）"),
        ("ko", "韓国語"),
        ("es", "スペイン語"),
        ("ja", "日本語"),
    ]

    /// 翻訳して入力するか（全体で 1 つのトグル。スロット単位ではない）
    static var isEnabled: Bool {
        get { UserDefaults.standard.bool(forKey: Key.enabled) }
        set { UserDefaults.standard.set(newValue, forKey: Key.enabled) }
    }

    /// 出力言語コード（既定 en）
    static var targetLanguage: String {
        get {
            let stored = UserDefaults.standard.string(forKey: Key.target) ?? ""
            return targetLanguages.contains(where: { $0.code == stored }) ? stored : "en"
        }
        set { UserDefaults.standard.set(newValue, forKey: Key.target) }
    }

    /// 使用するエンジン（既定 Apple）
    static var engine: DictationTranslationEngine {
        get {
            guard let raw = UserDefaults.standard.string(forKey: Key.engine),
                  let value = DictationTranslationEngine(rawValue: raw) else { return .apple }
            return value
        }
        set { UserDefaults.standard.set(newValue.rawValue, forKey: Key.engine) }
    }

    /// 出力言語コードの表示名（設定 UI・ログ用）
    static func label(for code: String) -> String {
        targetLanguages.first(where: { $0.code == code })?.label ?? code
    }
}

/// 翻訳結果（原文フォールバックしたかが呼び出し側で分かるようにする）
struct DictationTranslationResult {
    /// 貼り付けるテキスト（失敗時は原文そのまま）
    let text: String
    /// 実際に訳せたか
    let didTranslate: Bool
    /// 訳せなかった理由（ログ・HUD 用。成功時は nil）
    let failureReason: String?
}

/// 最終テキストを 1 回だけ訳す翻訳器
///
/// `TranslationSession` はスレッド安全が保証されないため actor に閉じ込める
/// （字幕の `AppleTranslator` と同じ作法）。Apple 側のセッションは使い回す
/// ＝ 1 回目でモデルをロードしたら 2 回目以降は往復だけになる。
@available(macOS 26.0, *)
actor DictationTranslator {

    private let engine: DictationTranslationEngine
    /// 翻訳元＝話す言語（音声入力の言語設定と同じ。"ja" など）
    private let source: Locale.Language
    private let sourceCode: String
    /// 出力言語（"en" など）
    private let target: Locale.Language
    private let targetCode: String

    private let availability = LanguageAvailability()
    private var session: TranslationSession?
    private var isPrepared = false

    /// - Parameters:
    ///   - engine: 使用するエンジン
    ///   - sourceLanguage: 話す言語のコード（音声入力の言語設定。空なら "ja"）
    ///   - targetLanguage: 出力言語コード
    init(engine: DictationTranslationEngine, sourceLanguage: String, targetLanguage: String) {
        self.engine = engine
        let from = sourceLanguage.trimmingCharacters(in: .whitespacesAndNewlines)
        // Apple の TranslationSession は翻訳元の省略を許さないので、未指定なら日本語とみなす
        // （このアプリの主用途が「日本語で話して他言語を入力する」ため）
        self.sourceCode = from.isEmpty ? "ja" : from
        self.source = Locale.Language(identifier: self.sourceCode)
        self.targetCode = targetLanguage
        self.target = Locale.Language(identifier: targetLanguage)
    }

    /// 初回遅延を消すための下準備（トグル ON 時・録音開始時に呼ぶ）
    ///
    /// **ダウンロード承認 UI は絶対に出さない**（字幕の `prepareIfInstalled` と同じ方針）。
    /// 未導入なら false を返し、実際の翻訳時は原文フォールバックになる。
    ///
    /// - Returns: すぐ訳せる状態なら true
    @discardableResult
    func prepare() async -> Bool {
        guard engine == .apple else { return true }  // Groq は準備不要（毎回 HTTP）
        if isPrepared { return true }
        let status = await availability.status(from: source, to: target)
        log.notice(
            "Apple 翻訳の言語状態(\(self.sourceCode, privacy: .public)→\(self.targetCode, privacy: .public)): \(String(describing: status), privacy: .public)"
        )
        guard status == .installed else { return false }
        let session = currentSession()
        guard (try? await session.prepareTranslation()) != nil else { return false }
        isPrepared = true
        return true
    }

    /// 最終テキストを訳す。失敗したら必ず原文を返す。
    ///
    /// - Parameter text: 文字起こし・整形・置換まで済んだ最終テキスト
    /// - Returns: 貼り付けるテキストと、訳せたかどうか
    func translate(_ text: String) async -> DictationTranslationResult {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else {
            return DictationTranslationResult(text: text, didTranslate: false, failureReason: "empty")
        }
        do {
            let translated: String
            switch engine {
            case .apple: translated = try await translateWithApple(trimmed)
            case .groq: translated = try await translateWithGroq(trimmed)
            }
            let result = translated.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !result.isEmpty else {
                return DictationTranslationResult(text: text, didTranslate: false, failureReason: "empty-result")
            }
            return DictationTranslationResult(text: result, didTranslate: true, failureReason: nil)
        } catch {
            let reason = (error as? TranslationError)?.description ?? String(describing: error)
            log.error("翻訳に失敗したため原文を入力します: \(reason, privacy: .public)")
            return DictationTranslationResult(text: text, didTranslate: false, failureReason: reason)
        }
    }

    // MARK: - エンジン別

    /// Apple のオンデバイス翻訳
    private func translateWithApple(_ text: String) async throws -> String {
        // 未準備なら 1 度だけ準備を試す（導入済みなら数十 ms で終わる）
        if !isPrepared, await !prepare() {
            throw TranslationError.translationUnavailable(
                "システム設定 > 一般 > 言語と地域 > 翻訳言語 で \(DictationTranslation.label(for: sourceCode)) と \(DictationTranslation.label(for: targetCode)) を追加してください"
            )
        }
        do {
            return try await currentSession().translate(text).targetText
        } catch {
            // モデルが外された・セッションが無効化された場合に備えて作り直させる
            session = nil
            isPrepared = false
            throw TranslationError.translationUnavailable(String(describing: error))
        }
    }

    /// Groq（OpenAI 互換）の LLM 翻訳。字幕側の実装をそのまま使い、
    /// システムプロンプトだけ「この言語へ訳す」に差し替える。
    private func translateWithGroq(_ text: String) async throws -> String {
        let target = targetCode
        let translator = GroqTranslator(
            keyProvider: { Keychain.apiKey(for: .groq) },
            modelProvider: { CaptionSettings.groqModelID },
            systemPromptProvider: { Self.groqSystemPrompt(targetCode: target) }
        )
        return try await translator.translate(text, context: [])
    }

    /// 音声入力向けのシステムプロンプト（訳文だけを返させる）
    static func groqSystemPrompt(targetCode: String) -> String {
        let language = DictationTranslation.label(for: targetCode)
        return """
        あなたは音声入力の翻訳エンジンです。入力された文を\(language)へ自然に翻訳し、\
        訳文だけを出力してください。説明・注釈・引用符・前置きは一切付けないでください。\
        入力が既に\(language)ならそのまま出力してください。
        """
    }

    /// 翻訳セッションを取得する（無ければ作る）
    ///
    /// 字幕の `AppleTranslationHost` は 1 つの言語ペアしか保持できないため、こちらは
    /// `TranslationSession(installedSource:target:)` を直接使う。ダウンロード要求はできないが、
    /// 本機能は「導入済みなら訳す・未導入なら原文」で十分（勝手にダイアログを出さない）。
    private func currentSession() -> TranslationSession {
        if let session { return session }
        let created = TranslationSession(installedSource: source, target: target)
        session = created
        return created
    }
}
