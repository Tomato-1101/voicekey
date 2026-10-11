//
//  LLMModelCatalog.swift
//  LLM（Groq / Gemini）のモデル一覧・おすすめ・モデル別パラメータ・廃止モデルの差し替えを 1 か所に集める
//
//  2026-10-11: Groq が llama 系を API から廃止し、整形・翻訳して入力・字幕翻訳が毎回失敗していた
//  （ログで 158 回連続）。モデル名が各所に直書きされていたのが原因なので、ここに集約し、
//  - 既知モデルの静的一覧（表示名・一言説明・おすすめ）
//  - API から取った実在一覧のキャッシュ（無料の GET のみ。1 日 1 回＋設定画面を開いたとき）
//  - 起動時に一度だけの保存値移行（廃止モデル → おすすめ）
//  - 実行時に「提供終了」で失敗したときの自動差し替え＋通知 1 回
//  をまとめて持つ。
//

import Foundation
import os.log

private let log = Logger(subsystem: "com.voicekey.app", category: "llm.models")

/// モデルを選べる LLM プロバイダー
enum LLMProvider: String, CaseIterable {
    case groq
    case gemini

    /// API キーの探索に使うプロバイダー（設定 › API キー → 環境変数 → 中央 Keychain の順に読む）
    var apiProvider: APIProvider {
        switch self {
        case .groq: return .groq
        case .gemini: return .gemini
        }
    }
}

/// 既知モデル 1 件（設定 UI の表示用）
struct LLMModelInfo: Equatable {
    let id: String
    /// Picker に出す名前
    let name: String
    /// 選んだときに項目名の下へ出す一言説明
    let note: String
    let recommended: Bool
}

/// Picker の選択肢 1 件
struct LLMModelOption: Identifiable, Equatable {
    let id: String
    let label: String
}

/// モデルを保存している設定（用途ごとに独立）
enum LLMModelSetting: String, CaseIterable {
    /// 文章の整形（Groq）
    case formatting
    /// 翻訳して入力（Groq）
    case dictationGroq
    /// ライブ字幕の翻訳（Groq）
    case captionGroq
    /// ライブ字幕の翻訳（Gemini）
    case captionGemini

    var provider: LLMProvider {
        self == .captionGemini ? .gemini : .groq
    }

    /// UserDefaults のキー。正本はそれぞれ ConfigStore / DictationTranslation / CaptionSettings と同じキー
    /// （ここから書いても各所の読み出しにそのまま効くようにするため、キー名を揃えている）
    var defaultsKey: String {
        switch self {
        case .formatting: return "formatModel"
        case .dictationGroq: return DictationTranslation.Key.groqModel
        case .captionGroq: return CaptionSettings.Key.groqModel
        case .captionGemini: return CaptionSettings.Key.geminiModel
        }
    }

    /// 通知・ログに出す用途名
    var label: String {
        switch self {
        case .formatting: return "整形"
        case .dictationGroq: return "翻訳して入力"
        case .captionGroq, .captionGemini: return "字幕翻訳"
        }
    }
}

/// 「提供終了で差し替えた」1 件（通知と ConfigStore の写しの更新に使う）
struct LLMModelReplacement {
    let setting: LLMModelSetting
    let oldModel: String
    let newModel: String

    var noticeText: String {
        "モデル \(oldModel) は提供終了のため \(newModel) に切り替えました"
    }
}

enum LLMModelCatalog {

    // MARK: - おすすめと既知モデル

    /// Groq のおすすめ。2026-10-11 実測（翻訳・無料枠・3 回中央値）で日→英 215ms / 英→日 220ms と最速、質も良好
    static let recommendedGroq = "qwen/qwen3.8-27b"
    /// Gemini のおすすめ。字幕の既定として使ってきたものを維持（実在確認済み・無料枠の対象）
    static let recommendedGemini = "gemini-3.5-flash-lite"

    static func recommended(_ provider: LLMProvider) -> String {
        provider == .groq ? recommendedGroq : recommendedGemini
    }

    /// Groq の既知モデル（先頭がおすすめ）。速度は 2026-10-11 の翻訳実測
    static let groqModels: [LLMModelInfo] = [
        .init(id: recommendedGroq, name: "Qwen3.8 27B",
              note: "最速（約 220ms）で質も良好", recommended: true),
        .init(id: "openai/gpt-oss-120b", name: "gpt-oss 120B",
              note: "最も自然な訳・少し遅い（約 400ms）", recommended: false),
        .init(id: "openai/gpt-oss-20b", name: "gpt-oss 20B",
              note: "速い（約 280〜460ms）", recommended: false),
        .init(id: "allam-2-7b", name: "ALLaM 2 7B",
              note: "アラビア語向け。日本語は崩れやすい", recommended: false),
    ]

    /// Gemini の既知モデル（先頭がおすすめ。generateContent 対応の flash 系）
    static let geminiModels: [LLMModelInfo] = [
        .init(id: recommendedGemini, name: "Gemini 3.5 Flash-Lite",
              note: "無料枠の対象・速い。字幕の既定", recommended: true),
        .init(id: "gemini-3.8-flash", name: "Gemini 3.8 Flash", note: "最新世代の Flash", recommended: false),
        .init(id: "gemini-3.7-flash", name: "Gemini 3.7 Flash", note: "Flash", recommended: false),
        .init(id: "gemini-3.6-flash", name: "Gemini 3.6 Flash", note: "Flash", recommended: false),
        .init(id: "gemini-3.5-flash", name: "Gemini 3.5 Flash", note: "Flash", recommended: false),
        .init(id: "gemini-3.1-flash-lite", name: "Gemini 3.1 Flash-Lite", note: "旧世代の Lite", recommended: false),
        .init(id: "gemini-2.5-flash", name: "Gemini 2.5 Flash", note: "旧世代の Flash", recommended: false),
        .init(id: "gemini-2.5-flash-lite", name: "Gemini 2.5 Flash-Lite", note: "旧世代の Lite", recommended: false),
        .init(id: "gemini-flash-latest", name: "Gemini Flash（最新の別名）",
              note: "常に最新の Flash を指す別名（中身が予告なく変わる）", recommended: false),
        .init(id: "gemini-flash-lite-latest", name: "Gemini Flash-Lite（最新の別名）",
              note: "常に最新の Flash-Lite を指す別名（中身が予告なく変わる）", recommended: false),
    ]

    static func known(_ provider: LLMProvider) -> [LLMModelInfo] {
        provider == .groq ? groqModels : geminiModels
    }

    /// 取得一覧が無くても廃止と分かっている Groq モデルか（llama 系は 2026-10 に API から廃止。
    /// kimi-k2-instruct は以前から 404）。取得一覧が手に入る前の初回起動でも直せるように静的に持つ
    static func isKnownRetired(_ id: String, provider: LLMProvider) -> Bool {
        guard provider == .groq else { return false }
        let lower = id.lowercased()
        return lower.hasPrefix("llama") || lower.hasPrefix("meta-llama/") || lower == "moonshotai/kimi-k2-instruct"
    }

    // MARK: - モデル別の追加パラメータ

    /// リクエストに足すモデル別パラメータ（Groq の chat/completions 用）
    ///
    /// 推論モデルは既定のままだと思考トークンを吐いて遅く・高くなる（整形・翻訳には不要）。
    /// gpt-oss は "none" を受け付けないので "low"＋思考を応答に含めない、qwen3 は思考そのものを切る。
    static func extraParameters(model: String) -> [String: Any] {
        let lower = model.lowercased()
        if lower.hasPrefix("openai/gpt-oss") {
            return ["reasoning_effort": "low", "include_reasoning": false]
        }
        if lower.hasPrefix("qwen/qwen3") {
            return ["reasoning_effort": "none"]
        }
        return [:]
    }

    // MARK: - 出力の長さ

    /// 出力トークン上限の天井（長い独り言でも 1 回の整形・翻訳で返し切れる量。これ以上は待ち時間の方が問題）
    static let maxCompletionTokensCeiling = 8192

    /// 入力の長さに応じた出力トークン上限（`max_completion_tokens`）
    ///
    /// gpt-oss などの推論モデルは思考トークンもこの上限に数える。固定 512 だと長い文の整形・翻訳が
    /// 途中で切れうるので、出力が入力とほぼ同じ長さになる前提で「入力文字数 × 3 ＋ 思考ぶん 512」を確保する
    /// （日本語は 1 文字 1〜2 トークン）。全モデルに同じ式を使う＝推論しないモデルでも切れない。
    static func maxCompletionTokens(inputCharacters: Int) -> Int {
        min(max(0, inputCharacters) * 3 + 512, maxCompletionTokensCeiling)
    }

    /// OpenAI 互換の chat/completions 応答が上限で途中で切れたか（`finish_reason == "length"`）
    ///
    /// 切れた訳文・整形文を貼ると発話の後半が黙って消えるので、呼び出し側は原文を返す。
    static func isTruncated(responseJSON data: Data) -> Bool {
        guard let root = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let choices = root["choices"] as? [[String: Any]] else { return false }
        return (choices.first?["finish_reason"] as? String) == "length"
    }

    // MARK: - 廃止エラーの判定

    /// 「モデルが提供終了／存在しない」による失敗か
    ///
    /// Groq は 400 `model_decommissioned` / 404 `model_not_found`、Gemini は 404 `NOT_FOUND`。
    /// それ以外（レート制限・キー不正・一般の 400）は差し替えても直らないので false。
    static func isModelUnavailable(status: Int, data: Data) -> Bool {
        guard status == 400 || status == 404 else { return false }
        guard let root = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let error = root["error"] as? [String: Any] else { return false }
        if let code = error["code"] as? String,
           ["model_decommissioned", "model_not_found"].contains(code) {
            return true
        }
        // Gemini は code が数値で、status 文字列が NOT_FOUND になる
        if status == 404, (error["status"] as? String) == "NOT_FOUND" { return true }
        let message = (error["message"] as? String ?? "").lowercased()
        return message.contains("decommissioned")
    }

    // MARK: - 実行時の差し替え

    /// 差し替えたときに呼ぶ（AppController が ConfigStore の写しを直して HUD に通知を出す）
    static var onModelReplaced: ((LLMModelReplacement) -> Void)? {
        get { lock.withLock { replacedHandler } }
        set { lock.withLock { replacedHandler = newValue } }
    }

    private static let lock = NSLock()
    private static var replacedHandler: ((LLMModelReplacement) -> Void)?
    /// 通知済みの「設定/モデル」。同時に飛んでいた複数リクエストが続けて失敗しても通知は 1 回にする
    private static var notified: Set<String> = []
    /// 直近に強制取得した時刻（設定画面の各 Picker が同時に開いても 1 回にまとめる）
    private static var lastForcedFetch: [LLMProvider: Date] = [:]

    /// 提供終了で失敗したモデルをおすすめへ差し替える
    ///
    /// 設定をおすすめへ書き換え、通知は（設定とモデルの組ごとに）1 回だけ出す。
    /// 失敗したのがおすすめ自身なら差し替え先が無いので nil（呼び出し側は従来どおり失敗扱い）。
    ///
    /// - Returns: 再試行に使うモデル（差し替えないときは nil）
    @discardableResult
    static func replaceUnavailable(
        _ setting: LLMModelSetting, failedModel: String, defaults: UserDefaults = .standard
    ) -> String? {
        let replacement = recommended(setting.provider)
        guard failedModel != replacement else { return nil }
        defaults.set(replacement, forKey: setting.defaultsKey)
        let (isFirst, handler) = lock.withLock { () -> (Bool, ((LLMModelReplacement) -> Void)?) in
            (notified.insert("\(setting.rawValue)/\(failedModel)").inserted, replacedHandler)
        }
        log.notice("モデル提供終了のため差し替え: \(setting.rawValue, privacy: .public) \(failedModel, privacy: .public) → \(replacement, privacy: .public)")
        ActionLog.shared.write(
            "llm", "[モデル] \(setting.label): \(failedModel) は提供終了のため \(replacement) へ切り替え（自動再試行）")
        if isFirst {
            handler?(LLMModelReplacement(setting: setting, oldModel: failedModel, newModel: replacement))
        }
        return replacement
    }

    // MARK: - 起動時の移行（一度だけ）

    private static let migrationFlagKey = "didMigrateLLMModelsV20"

    /// 保存値を差し替えるべきか（差し替え先を返す。不要なら nil）
    ///
    /// Groq: 廃止と分かっているモデル、または取得一覧があるのにそこに無いモデル。
    /// Gemini: 一覧の絞り込み（flash 系だけ）から外れた正当なモデルを消さないよう、起動時には触らない
    /// （提供終了なら実行時の 404 で差し替わる）。
    static func migratedModel(_ stored: String, provider: LLMProvider, available: [String]?) -> String? {
        let id = stored.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !id.isEmpty, provider == .groq, id != recommended(provider) else { return nil }
        return isUnavailable(id, provider: provider, available: available) ? recommended(provider) : nil
    }

    /// 保存済みの全モデル設定を一度だけ見直す（ConfigStore の初期化で写しを読む前に呼ぶ）
    ///
    /// 毎回やると、取得一覧に一時的に出てこなかったモデルまで起動のたびに消えるので一度きりにする
    /// （以後の提供終了は実行時の差し替えで拾う）。
    static func migrateStoredModelsIfNeeded(defaults: UserDefaults) {
        guard !defaults.bool(forKey: migrationFlagKey) else { return }
        for setting in LLMModelSetting.allCases {
            guard let stored = defaults.string(forKey: setting.defaultsKey),
                  let replacement = migratedModel(
                    stored, provider: setting.provider, available: cachedAvailable(setting.provider, defaults: defaults))
            else { continue }
            defaults.set(replacement, forKey: setting.defaultsKey)
            log.notice("保存モデルを移行: \(setting.rawValue, privacy: .public) \(stored, privacy: .public) → \(replacement, privacy: .public)")
        }
        defaults.set(true, forKey: migrationFlagKey)
    }

    // MARK: - 実在一覧（API から取得してキャッシュ）

    private static func cacheKey(_ provider: LLMProvider) -> String { "llmModelCache.\(provider.rawValue).ids" }
    private static func cacheDateKey(_ provider: LLMProvider) -> String { "llmModelCache.\(provider.rawValue).fetchedAt" }

    /// キャッシュ済みの実在一覧（一度も取れていなければ nil）
    static func cachedAvailable(_ provider: LLMProvider, defaults: UserDefaults = .standard) -> [String]? {
        guard let ids = defaults.stringArray(forKey: cacheKey(provider)), !ids.isEmpty else { return nil }
        return ids
    }

    /// Groq `GET /openai/v1/models` の応答からチャットに使えるモデルだけを取り出す
    static func parseGroqModels(_ data: Data) -> [String]? {
        guard let root = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let list = root["data"] as? [[String: Any]] else { return nil }
        // 音声認識・読み上げ・安全分類器は chat/completions で文章を返さないので選択肢に出さない
        let excluded = ["whisper", "orpheus", "tts", "guard", "embed", "playai"]
        return list.compactMap { item -> String? in
            guard let id = item["id"] as? String else { return nil }
            if (item["active"] as? Bool) == false { return nil }
            let lower = id.lowercased()
            return excluded.contains(where: { lower.contains($0) }) ? nil : id
        }.sorted()
    }

    /// Gemini `GET /v1beta/models` の応答から generateContent 対応の flash 系だけを取り出す
    static func parseGeminiModels(_ data: Data) -> [String]? {
        guard let root = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let list = root["models"] as? [[String: Any]] else { return nil }
        // 読み上げ・画像生成・音声対話・埋め込みは文章の翻訳に使えない
        let excluded = ["tts", "image", "omni", "live", "audio", "embedding", "computer"]
        return list.compactMap { item -> String? in
            guard let name = item["name"] as? String,
                  let methods = item["supportedGenerationMethods"] as? [String],
                  methods.contains("generateContent") else { return nil }
            let id = name.hasPrefix("models/") ? String(name.dropFirst("models/".count)) : name
            let lower = id.lowercased()
            guard lower.hasPrefix("gemini"), lower.contains("flash") else { return nil }
            return excluded.contains(where: { lower.contains($0) }) ? nil : id
        }.sorted()
    }

    /// 実在一覧を取り直す（無料の一覧 GET のみ。生成 API は呼ばない）
    ///
    /// - Parameter force: true なら 1 日の間隔を無視する（設定画面を開いたとき）。ただし 60 秒以内の重複は省く
    static func refreshAvailable(_ provider: LLMProvider, force: Bool, defaults: UserDefaults = .standard) async {
        let now = Date()
        if force {
            let skip = lock.withLock { () -> Bool in
                if let last = lastForcedFetch[provider], now.timeIntervalSince(last) < 60 { return true }
                lastForcedFetch[provider] = now
                return false
            }
            if skip { return }
        } else {
            let fetchedAt = defaults.double(forKey: cacheDateKey(provider))
            if fetchedAt > 0, now.timeIntervalSince1970 - fetchedAt < 24 * 60 * 60 { return }
        }
        // キーの探索は子プロセスを起動するので、呼び出し側（UI）ではなくここ（非メイン）で行う
        guard let key = APIKeyStore.load(provider.apiProvider)?.key else { return }

        var request: URLRequest
        switch provider {
        case .groq:
            request = URLRequest(url: URL(string: "https://api.groq.com/openai/v1/models")!)
            request.setValue("Bearer \(key)", forHTTPHeaderField: "Authorization")
        case .gemini:
            request = URLRequest(url: URL(string: "https://generativelanguage.googleapis.com/v1beta/models?pageSize=1000")!)
            // キーは URL に載せない（各種ログへ残るため）
            request.setValue(key, forHTTPHeaderField: "x-goog-api-key")
        }
        request.timeoutInterval = 10

        do {
            let (data, response) = try await URLSession(configuration: .ephemeral).data(for: request)
            guard (response as? HTTPURLResponse)?.statusCode == 200 else {
                log.notice("モデル一覧の取得に失敗: \(provider.rawValue, privacy: .public) HTTP \((response as? HTTPURLResponse)?.statusCode ?? -1)")
                return
            }
            let ids = provider == .groq ? parseGroqModels(data) : parseGeminiModels(data)
            // 空の一覧で上書きすると全モデルが「利用不可」に見えるので、取れたときだけ保存する
            guard let ids, !ids.isEmpty else { return }
            defaults.set(ids, forKey: cacheKey(provider))
            defaults.set(now.timeIntervalSince1970, forKey: cacheDateKey(provider))
        } catch {
            log.notice("モデル一覧の取得に失敗: \(provider.rawValue, privacy: .public) \(error.localizedDescription, privacy: .public)")
        }
    }

    // MARK: - 設定 UI 用

    /// 保存値が使えないモデルか
    ///
    /// - 取得一覧にあるなら使える（静的な廃止リストより実在を優先する。同名で復活・別名で残っている場合に
    ///   使えるモデルを「利用不可」と出して差し替えさせないため）
    /// - 廃止と分かっているなら使えない
    /// - Groq だけ「取得一覧に無い」も使えない扱い。Gemini の取得一覧は flash 系に絞っているので、
    ///   一覧に無いだけでは pro 系など有効なモデルまで提供終了に見えてしまう（実際の提供終了は実行時の 404 で差し替わる）
    static func isUnavailable(_ id: String, provider: LLMProvider, available: [String]?) -> Bool {
        if available?.contains(id) == true { return false }
        if isKnownRetired(id, provider: provider) { return true }
        guard provider == .groq, let available else { return false }
        return !available.contains(id)
    }

    /// Picker の選択肢（おすすめ → 既知 → 取得一覧にだけあるもの → 一覧に無い保存値）
    static func options(_ provider: LLMProvider, current: String, available: [String]?) -> [LLMModelOption] {
        var result: [LLMModelOption] = []
        for info in known(provider) {
            // 取得一覧があるときは、そこに無い既知モデル（提供終了）は選ばせない
            if let available, !available.contains(info.id), !info.recommended { continue }
            result.append(.init(id: info.id, label: info.recommended ? "\(info.name)（おすすめ）" : info.name))
        }
        for id in available ?? [] where !result.contains(where: { $0.id == id }) {
            result.append(.init(id: id, label: id))
        }
        if !current.isEmpty, !result.contains(where: { $0.id == current }) {
            let unavailable = isUnavailable(current, provider: provider, available: available)
            result.append(.init(id: current, label: unavailable ? "\(current)（利用不可）" : current))
        } else if !current.isEmpty, isUnavailable(current, provider: provider, available: available),
                  let index = result.firstIndex(where: { $0.id == current }) {
            // おすすめ自体が一覧から消えた場合も、選択中なら利用不可と分かるようにする
            result[index] = .init(id: current, label: "\(result[index].label)（利用不可）")
        }
        return result
    }

    /// 選択中モデルの一言説明（Picker の項目名の下に出す）
    static func detail(_ provider: LLMProvider, id: String, available: [String]?) -> String {
        if isUnavailable(id, provider: provider, available: available) {
            return "このモデルは提供終了か、使えない状態です。おすすめ（\(recommended(provider))）に切り替えてください。"
        }
        if let info = known(provider).first(where: { $0.id == id }) {
            return "\(info.note)（\(info.id)）"
        }
        return "API の一覧から取得したモデルです（\(id)）"
    }
}
