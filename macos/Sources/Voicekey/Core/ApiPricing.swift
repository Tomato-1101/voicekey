//
//  ApiPricing.swift
//  API 単価表と料金計算（API 利用料金の見える化の単一ソース）
//
//  単価はここ 1 ファイルにだけ置く。記録側（ApiUsageStore）は数量だけを保存し、
//  料金は表示のたびにこの表から計算する＝単価を直せば過去の使用量にもそのまま反映される。
//  公式の料金ページで確認できなかった単価は推測で埋めず nil（＝単価未確認）にする。
//

import Foundation

/// 課金元のプロバイダー（記録と表示のキー）。保存は rawValue の文字列で行う。
enum ApiProvider: String, Codable, CaseIterable {
    case soniox
    case openai
    case microsoft
    case elevenlabs
    case groq
    case gemini
    case deepgram
    case apple

    /// 画面に出す表示名（実プロバイダー名・personal の方針どおり）
    var label: String {
        switch self {
        case .soniox: return "Soniox"
        case .openai: return "OpenAI"
        case .microsoft: return "Microsoft"
        case .elevenlabs: return "ElevenLabs"
        case .groq: return "Groq"
        case .gemini: return "Gemini"
        case .deepgram: return "Deepgram"
        case .apple: return "Apple"
        }
    }

    /// 文字起こしの Backend から課金元を引く（openaiLive も請求は OpenAI）
    init(backend: Backend) {
        switch backend {
        case .openai, .openaiLive: self = .openai
        case .groq: self = .groq
        case .elevenlabs: self = .elevenlabs
        case .deepgram: self = .deepgram
        case .appleLocal: self = .apple
        case .soniox: self = .soniox
        case .azureMAI: self = .microsoft
        }
    }
}

/// API を何のために呼んだか（内訳の表示用）。保存は rawValue の文字列で行う。
enum ApiUsagePurpose: String, Codable, CaseIterable {
    case transcription
    case formatting
    case translation
    case captionTranslation

    var label: String {
        switch self {
        case .transcription: return "文字起こし"
        case .formatting: return "テキスト整形"
        case .translation: return "翻訳して入力"
        case .captionTranslation: return "字幕の翻訳"
        }
    }
}

/// 1 モデルぶんの単価
enum ApiPrice: Equatable {
    /// 無料（Apple のオンデバイス認識・翻訳）
    case free
    /// 音声の時間課金（USD / 時間）。minimumBilledSeconds は 1 リクエストあたりの最低課金秒数
    case perAudioHour(usd: Double, minimumBilledSeconds: Double)
    /// LLM のトークン課金（USD / 100 万トークン）
    case perMillionTokens(input: Double, output: Double)
}

enum ApiPricing {

    /// 単価を公式ページで確認した日（画面の注記に出す）
    static let checkedOn = "2026-10-02"

    /// 本人が実際にお金を払っている契約のあるプロバイダー（「実際に払った分」表示の対象）。
    /// 2026-10-02 本人申告＋メール確認: OpenAI はカードチャージの領収あり、Soniox・Azure（MAI）は有料契約。
    /// Groq / ElevenLabs / Deepgram は請求メールが無い（無料枠）、Gemini も無料枠運用のため含めない。
    /// 契約が変わったらここ 1 か所だけ直す。
    static let paidProviders: Set<ApiProvider> = [.openai, .soniox, .microsoft]

    /// 単価表。キーは「プロバイダー/モデル ID（小文字）」。ここに無いモデルは単価未確認（nil）。
    /// 各行のコメントに出典 URL と確認日を書く。値を推測で足さないこと。
    private static let table: [String: ApiPrice] = [
        // Soniox リアルタイム $0.12/時間（実体は音声トークン課金・約 3 万トークン/時の概算）
        // 出典 https://soniox.com/pricing（2026-10-02 確認）
        "soniox/stt-rt-v5": .perAudioHour(usd: 0.12, minimumBilledSeconds: 0),
        // OpenAI gpt-transcribe $0.0045/分 = $0.27/時間
        // 出典 https://developers.openai.com/api/docs/pricing（2026-10-02 確認）
        "openai/gpt-transcribe": .perAudioHour(usd: 0.0045 * 60, minimumBilledSeconds: 0),
        // OpenAI gpt-live-transcribe（Realtime）$0.017/分 = $1.02/時間
        // 出典 https://developers.openai.com/api/docs/pricing（2026-10-02 確認）
        "openai/gpt-live-transcribe": .perAudioHour(usd: 0.017 * 60, minimumBilledSeconds: 0),
        // Microsoft MAI-Transcribe-2 $0.10/時間（2026 年末までの期間限定価格）
        // 出典 https://microsoft.ai/news/mai-transcribe-2-is-the-fastest-most-accurate-and-cheapest-speech-recognition-model-in-the-world/
        //      （Azure の料金ページにも 12/31/2026 までのプロモーション表記・2026-10-02 確認）
        "microsoft/mai-transcribe-2": .perAudioHour(usd: 0.10, minimumBilledSeconds: 0),
        // ElevenLabs Scribe v2 $0.22/時間（scribe_v1 は料金ページに無いので未確認のまま）
        // 出典 https://elevenlabs.io/pricing/api（2026-10-02 確認）
        "elevenlabs/scribe_v2": .perAudioHour(usd: 0.22, minimumBilledSeconds: 0),
        // Groq whisper-large-v3-turbo $0.04/時間・1 リクエスト最低 10 秒課金
        // 出典 https://console.groq.com/docs/speech-to-text / https://console.groq.com/docs/models（2026-10-02 確認）
        "groq/whisper-large-v3-turbo": .perAudioHour(usd: 0.04, minimumBilledSeconds: 10),
        // Groq LLM（llama-3.1-8b-instant / llama-3.3-70b-versatile は「Contact Sales」表記＝単価未確認）
        // 出典 https://console.groq.com/docs/models（2026-10-02 確認）
        "groq/openai/gpt-oss-20b": .perMillionTokens(input: 0.075, output: 0.30),
        "groq/openai/gpt-oss-120b": .perMillionTokens(input: 0.15, output: 0.60),
        "groq/qwen/qwen3.8-27b": .perMillionTokens(input: 0.80, output: 4.00),
        // Gemini 3.5 Flash-Lite 有料枠 入力 $0.30 / 出力 $2.50（出力は思考トークン込み）。
        // 無料枠で使っている場合は実際には 0 円だが、どちらの枠かはアプリから分からないので有料枠で見積もる
        // 出典 https://ai.google.dev/gemini-api/docs/pricing（2026-10-02 確認）
        "gemini/gemini-3.5-flash-lite": .perMillionTokens(input: 0.30, output: 2.50),
    ]

    /// 単価を引く。nil = 単価未確認（画面には「単価未確認」と出す）。Apple は常に無料。
    static func price(provider: ApiProvider, model: String) -> ApiPrice? {
        if provider == .apple { return .free }
        return table["\(provider.rawValue)/\(model.lowercased())"]
    }

    /// 1 リクエストぶんの課金対象秒数（最低課金秒数を適用）。単価未確認なら実秒数のまま
    static func billedSeconds(provider: ApiProvider, model: String, audioSeconds: Double) -> Double {
        let seconds = max(0, audioSeconds)
        guard seconds > 0,
              case .perAudioHour(_, let minimum)? = price(provider: provider, model: model)
        else { return seconds }
        return max(seconds, minimum)
    }

    /// 数量から料金（USD）を計算する。nil = 単価未確認。
    /// 音声は課金対象秒数（billedSeconds 済み）を、LLM は入出力トークン数を使う。
    static func cost(
        provider: ApiProvider, model: String,
        billedAudioSeconds: Double, inputTokens: Int, outputTokens: Int
    ) -> Double? {
        guard let price = price(provider: provider, model: model) else { return nil }
        switch price {
        case .free:
            return 0
        case .perAudioHour(let usd, _):
            return billedAudioSeconds / 3600 * usd
        case .perMillionTokens(let input, let output):
            return Double(inputTokens) / 1_000_000 * input + Double(outputTokens) / 1_000_000 * output
        }
    }

    /// 「$0.12」形式の表示文字列。1 セント未満は桁を増やして 0 に潰さない
    static func formattedUSD(_ usd: Double) -> String {
        if usd > 0 && usd < 0.01 { return String(format: "$%.4f", usd) }
        return String(format: "$%.2f", usd)
    }
}

/// 料金の表示モード。ホームの全数値とメニューの「今日の API 代」が同じモードに従う。
enum ApiCostMode: String, CaseIterable {
    /// 定価（無料枠で使った分も単価どおりに計上）
    case list
    /// 実際に払った分（有料契約のプロバイダーだけ計上。それ以外は無料枠として 0 円）
    case paid

    /// UserDefaults のキー（ホームの @AppStorage と共有）
    static let defaultsKey = "apiCostMode"

    /// 保存されているモード（無い・壊れた値は定価）
    static var current: ApiCostMode {
        ApiCostMode(rawValue: UserDefaults.standard.string(forKey: defaultsKey) ?? "") ?? .list
    }

    /// このモードで無料枠として扱う（0 円に落とす）プロバイダーか
    func isFreeTier(_ provider: ApiProvider?) -> Bool {
        guard self == .paid, let provider else { return false }
        return !ApiPricing.paidProviders.contains(provider)
    }
}
