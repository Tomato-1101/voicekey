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
import NaturalLanguage
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
        static let groqModel = "translateInputGroqModel"
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

    /// Groq エンジンで使うモデル（未設定ならおすすめ）
    ///
    /// 以前は字幕の `CaptionSettings.groqModelID` を流用していたが、字幕と音声入力では向いている
    /// モデルが違いうるので用途ごとに独立させた（2026-10-11 ユーザー指示「全部モデルを選べるように」）。
    static var groqModelID: String {
        get {
            let stored = UserDefaults.standard.string(forKey: Key.groqModel)?
                .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
            return stored.isEmpty ? LLMModelCatalog.recommendedGroq : stored
        }
        set { UserDefaults.standard.set(newValue, forKey: Key.groqModel) }
    }

    /// 出力言語コードの表示名（設定 UI・ログ用）
    static func label(for code: String) -> String {
        targetLanguages.first(where: { $0.code == code })?.label ?? code
    }

    /// 録音中 HUD に出す小さな印（"en" → "→EN"）。翻訳される回だとひと目で分かるようにする
    static func badge(for code: String) -> String {
        "→" + (Locale.Language(identifier: code).languageCode?.identifier ?? code).uppercased()
    }

    /// この OS で「翻訳して入力」が使えるか（翻訳器も設定タブも macOS 26 以降のみ）
    static var isSupported: Bool {
        if #available(macOS 26.0, *) { return true }
        return false
    }

    /// 判定に使う 1 回翻訳キー（純関数・テスト用に切り出し）
    ///
    /// 既定は右⇧なので、macOS 26 未満で素通しすると右⇧を押すたびに「→EN」の印だけ出て原文が入る。
    /// 26 未満は設定タブ自体が出ず外す手段も無いので、ここで丸ごと無効（空）にする。
    static func effectiveTranslateOnceKey(_ key: [String], supported: Bool) -> [String] {
        supported ? key : []
    }

    /// 録音開始時に、この録音を翻訳するか（26 未満は翻訳器が無いので常に false）
    static func shouldTranslate(alwaysOn: Bool, translateOnce: Bool, supported: Bool) -> Bool {
        supported && (alwaysOn || translateOnce)
    }

    /// 録音中に 1 回翻訳キーを押したあとの「翻訳するか」（押すたびに付け外しする）
    ///
    /// 常時 ON の設定で翻訳している録音は、キーでは外さない（外せるのはキーで付けた翻訳だけ）。
    static func toggledTranslate(current: Bool, alwaysOn: Bool) -> Bool {
        alwaysOn ? current : !current
    }

    /// 1 回翻訳キーが他のキー設定と重なっていないか（設定画面の警告文。重なりが無ければ空）
    ///
    /// 黙って効かなくなる組み合わせだけを拾う（純関数・テスト対象）:
    /// - 録音キーの一部（同じ・個数違いで含まれる）: 録音のたびに翻訳になるので、判定側で無視している
    /// - ハンズフリーキー（録音キーと併せて覆われる場合も）: ハンズフリーを優先して無視している
    /// - 再貼り付けキーが 1 回翻訳キーに含まれる（同じを含む）: 待機中に押すと再貼り付けが走る
    /// 1 回翻訳キーが録音キーを含む（例 右⌘＋右⇧）は「録音キーと同時に押す」使い方なので警告しない。
    static func translateOnceKeyWarnings(
        key: [String], slotHotkeys: [[String]], handsfreeKey: [String], repasteKey: [String]
    ) -> [String] {
        guard !key.isEmpty else { return [] }
        var warnings: [String] = []
        for (index, slot) in slotHotkeys.enumerated() where !slot.isEmpty {
            if KeyToken.conflicts(key, slot) || KeyToken.isCovered(key, by: slot) {
                warnings.append("録音キー \(index + 1) と重なっています。この 1 回翻訳キーは効きません")
            }
        }
        if warnings.isEmpty, !handsfreeKey.isEmpty {
            let covered = KeyToken.conflicts(key, handsfreeKey) || KeyToken.isCovered(key, by: handsfreeKey)
                || slotHotkeys.contains { !$0.isEmpty && KeyToken.isCovered(key, by: $0 + handsfreeKey) }
            if covered {
                warnings.append("ハンズフリーキーと重なっています。ハンズフリーが優先され、この 1 回翻訳キーは効きません")
            }
        }
        if !repasteKey.isEmpty,
           KeyToken.conflicts(key, repasteKey) || KeyToken.isCovered(repasteKey, by: key) {
            warnings.append("再貼り付けキーと重なっています。録音していないときに押すと再貼り付けが動きます")
        }
        return warnings
    }

    /// 「この 1 回だけ翻訳」キーが押されているか（純関数・テスト用に切り出し）
    ///
    /// - Parameters:
    ///   - key: 1 回翻訳キーの設定（空なら無効）
    ///   - slotHotkey: 録音キー（とハンズフリーキー）の設定。1 回翻訳キーがこれに含まれていると
    ///     録音（ハンズフリー起動）のたびに翻訳になってしまうので、その場合は押されていても翻訳扱いにしない
    ///   - pressed: いま押されているキー
    static func isTranslateOnceRequested(key: [String], slotHotkey: [String], pressed: Set<String>) -> Bool {
        guard !key.isEmpty else { return false }
        let slotNames = slotHotkey.reduce(into: Set<String>()) { $0.formUnion(KeyToken.acceptableNames(for: $1)) }
        let coveredBySlot = key.allSatisfy { !KeyToken.acceptableNames(for: $0).isDisjoint(with: slotNames) }
        if coveredBySlot { return false }
        return key.allSatisfy { !KeyToken.acceptableNames(for: $0).isDisjoint(with: pressed) }
    }

    /// 押されたキーが 1 回翻訳キーの一部か（録音中に押されたキーを翻訳の指示として拾うかの判定）
    static func isTranslateOnceToken(_ token: String, key: [String]) -> Bool {
        key.contains { KeyToken.acceptableNames(for: $0).contains(token) }
    }

    /// テキストの言語を推定する（推定できなければ fallback）。
    ///
    /// 「話す言語の設定＝翻訳元」と決め打つと、英語で話したときに ja→en として扱われて
    /// 訳せない／壊れるため、入力そのものから判定する（オフライン・数 ms）。
    ///
    /// 候補を絞らないと、日中で共通の漢字だけの短文（「了解」「人工知能」など）が中国語と判定され、
    /// 翻訳元を取り違える（実測）。候補は話す言語・翻訳先・日本語・英語に絞り、話す言語を強く優先する。
    ///
    /// - Parameters:
    ///   - fallback: 話す言語の設定（推定できないときの答え・最も優先する候補）
    ///   - target: 翻訳先（既に翻訳先の言語で話したことを見分けるために候補へ入れる）
    static func detectLanguageCode(_ text: String, fallback: String, target: String? = nil) -> String {
        let recognizer = NLLanguageRecognizer()
        let spoken = nlLanguages(for: fallback)
        let candidates = languageCandidates(spoken: fallback, target: target ?? "")
        recognizer.languageConstraints = candidates
        // 話す言語に強い事前確率を置く（短い文ほど事前確率が効き、長い文は中身で決まる）。
        // 翻訳先が中国語のとき、候補に入った中国語へ短い漢字文が流れるのをこれで防ぐ。
        // 0.97 は実測: 0.9 だと「了解」が zh、0.99 だと短い中国語文「我们明天去上海吧」まで ja になる
        let spokenWeight = 0.97
        let others = candidates.filter { !spoken.contains($0) }
        var hints: [NLLanguage: Double] = [:]
        for language in spoken { hints[language] = spokenWeight / Double(spoken.count) }
        for language in others { hints[language] = (spoken.isEmpty ? 1.0 : 1 - spokenWeight) / Double(others.count) }
        recognizer.languageHints = hints
        recognizer.processString(text)
        guard let language = recognizer.dominantLanguage, language != .undetermined else { return fallback }
        return language.rawValue
    }

    /// 言語判定の候補（話す言語・翻訳先・日本語・英語の順に重複なし）
    static func languageCandidates(spoken: String, target: String) -> [NLLanguage] {
        var result: [NLLanguage] = []
        for code in [spoken, target, "ja", "en"] {
            for language in nlLanguages(for: code) where !result.contains(language) {
                result.append(language)
            }
        }
        return result
    }

    /// 言語コード（"ja" / "en-US" / "zh-Hans" など）を NaturalLanguage の言語へ写す
    private static func nlLanguages(for code: String) -> [NLLanguage] {
        let trimmed = code.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return [] }
        let base = Locale.Language(identifier: trimmed).languageCode?.identifier ?? trimmed
        guard base == "zh" else { return [NLLanguage(rawValue: base)] }
        // 中国語は字体ごとに別の言語として判定される。指定が無ければ両方を候補にする
        if trimmed.contains("Hant") { return [.traditionalChinese] }
        if trimmed.contains("Hans") { return [.simplifiedChinese] }
        return [.simplifiedChinese, .traditionalChinese]
    }

    /// 同じ言語か（地域・字体の違いは無視して言語コードだけで比べる。"zh-Hans" と "zh" は同じ扱い）
    static func isSameLanguage(_ a: String, _ b: String) -> Bool {
        let codeA = Locale.Language(identifier: a).languageCode?.identifier ?? a
        let codeB = Locale.Language(identifier: b).languageCode?.identifier ?? b
        return codeA == codeB
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
    /// Apple 翻訳の翻訳元。初期値は話す言語の設定（準備用の見込み）で、実際の翻訳時は
    /// 入力から推定した言語に差し替える（何語で話しても翻訳先へ訳すため）
    private var source: Locale.Language
    private var sourceCode: String
    /// 出力言語（"en" など）
    private let target: Locale.Language
    private let targetCode: String
    /// 話す言語の設定（`sourceCode` は判定のたびに書き換わるので、言語判定の優先候補は別に持つ）
    private let spokenCode: String
    /// Groq 呼び出し用（字幕と同じく待ちすぎない。落ちたら原文を入力する）
    private let urlSession: URLSession = {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.timeoutIntervalForRequest = 20
        configuration.timeoutIntervalForResource = 30
        configuration.waitsForConnectivity = false
        return URLSession(configuration: configuration)
    }()

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
        self.spokenCode = self.sourceCode
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
        if engine == .apple {
            // Apple は翻訳元を明示する必要があるので入力から推定する（推定できなければ設定の言語）。
            // 既に出力言語なら訳す必要が無い（Apple 翻訳は同一ペアをそもそも受け付けない）。
            // 失敗扱いにすると入力のたびに「翻訳できなかった」通知が出てしまうので、原文をそのまま
            // 最終テキストとして返す（実機ログで ja→ja の指定を検出したため 2026-08-23 に追加）。
            let detected = DictationTranslation.detectLanguageCode(trimmed, fallback: spokenCode, target: targetCode)
            if DictationTranslation.isSameLanguage(detected, targetCode) {
                return DictationTranslationResult(text: text, didTranslate: true, failureReason: nil)
            }
            useSource(detected)
        }
        // Groq はプロンプト側で原文の言語を推定させる（既に出力言語ならそのまま返させる）
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

    /// Groq（OpenAI 互換）の LLM 翻訳
    ///
    /// 字幕の `GroqTranslator` は出力上限が 512 トークン固定で、gpt-oss は推論トークンもこれに含むため
    /// 長い発話の訳が途中で切れる。音声入力は 1 回きりの長文もあるので、入力長に応じた上限で
    /// 非ストリーミングで呼ぶ（貼り付けは全文が揃ってからなので、ストリーミングの利点も無い）。
    private func translateWithGroq(_ text: String) async throws -> String {
        guard let key = Keychain.apiKey(for: .groq), !key.isEmpty else { throw TranslationError.missingAPIKey }
        var model = DictationTranslation.groqModelID
        let systemPrompt = Self.groqSystemPrompt(targetCode: targetCode)
        var (data, http) = try await sendGroq(model: model, key: key, systemPrompt: systemPrompt, text: text)
        // モデルの提供終了だけは、その 1 回をおすすめで取り直して設定も書き換える（字幕側と同じ方針）
        if http.statusCode != 200,
           LLMModelCatalog.isModelUnavailable(status: http.statusCode, data: data),
           let replacement = LLMModelCatalog.replaceUnavailable(.dictationGroq, failedModel: model) {
            model = replacement
            (data, http) = try await sendGroq(model: model, key: key, systemPrompt: systemPrompt, text: text)
        }
        guard http.statusCode == 200 else {
            throw GroqTranslator.mapError(status: http.statusCode, http: http, data: data)
        }
        ApiUsageStore.shared.recordChatUsage(fromJSON: data, provider: .groq, model: model, purpose: .translation)
        return try Self.groqTranslatedText(from: data)
    }

    /// 1 回分の chat/completions を送る（非 200 も本文ごと返す）
    private func sendGroq(
        model: String, key: String, systemPrompt: String, text: String
    ) async throws -> (Data, HTTPURLResponse) {
        guard let url = URL(string: "\(GroqTranslator.endpointBase)/chat/completions") else {
            throw TranslationError.badRequest(message: "エンドポイントが不正です")
        }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("Bearer \(key)", forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(
            withJSONObject: Self.groqRequestBody(model: model, systemPrompt: systemPrompt, text: text))
        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await urlSession.data(for: request)
        } catch {
            throw TranslationError.network((error as NSError).localizedDescription)
        }
        guard let http = response as? HTTPURLResponse else {
            throw TranslationError.badResponse("HTTP レスポンスではありません")
        }
        return (data, http)
    }

    /// 翻訳リクエストの本文（純関数・テスト対象）
    static func groqRequestBody(model: String, systemPrompt: String, text: String) -> [String: Any] {
        var body: [String: Any] = [
            "model": model,
            "messages": [
                ["role": "system", "content": systemPrompt],
                ["role": "user", "content": text],
            ],
            "max_completion_tokens": LLMModelCatalog.maxCompletionTokens(inputCharacters: text.count),
        ]
        // 推論モデルの思考を抑える等、モデル別の指定を足す（整形・字幕と同じ表）
        body.merge(LLMModelCatalog.extraParameters(model: model)) { _, new in new }
        return body
    }

    /// 応答から訳文を取り出す（純関数・テスト対象）
    ///
    /// 出力上限で切れた訳は文の途中で終わっているので、訳として貼らずに失敗扱い（＝原文を入力）にする。
    static func groqTranslatedText(from data: Data) throws -> String {
        if LLMModelCatalog.isTruncated(responseJSON: data) {
            throw TranslationError.badResponse("出力が上限で途中で切れました")
        }
        return try GroqTranslator.extractText(from: data)
    }

    /// 音声入力向けのシステムプロンプト（訳文だけを返させる）
    static func groqSystemPrompt(targetCode: String) -> String {
        let language = DictationTranslation.label(for: targetCode)
        return """
        あなたは音声入力の翻訳エンジンです。入力された文（何語かは決まっていません）を\(language)へ自然に翻訳し、\
        訳文だけを出力してください。説明・注釈・引用符・前置きは一切付けないでください。\
        入力が既に\(language)ならそのまま出力してください。
        """
    }

    /// Apple 翻訳の翻訳元を切り替える（変わったときだけセッションを作り直す）
    private func useSource(_ code: String) {
        guard code != sourceCode else { return }
        sourceCode = code
        source = Locale.Language(identifier: code)
        session = nil
        isPrepared = false
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
