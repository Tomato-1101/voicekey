/// 翻訳器の抽象と、キー未設定時に結線を確認するためのモック実装
///
/// 実装は 4 つ:
///   - `AppleTranslator`  : Apple 純正 Translation framework（既定・オンデバイス・キー不要）
///   - `GeminiTranslator` : Gemini API（任意・無料枠あり・LLM 品質）
///   - `GroqTranslator`   : Groq API（任意・OpenAI 互換・SSE ストリーミング対応）
///   - `MockTranslator`   : API キーが無い環境で HUD/読み上げ/流量制御の結線だけ検証する
///
/// クラウドの翻訳器は `FallbackTranslator` で包んで使う（失敗・429 のときに
/// Apple のオンデバイス翻訳へ落として字幕を止めないため）。
import Foundation
import OSLog

/// 直近の「原文 → 訳文」ペア（翻訳時の文脈として使う）
@available(macOS 26.0, *)
struct TranslationExchange: Sendable {
    /// 原文（英語）
    let source: String
    /// 訳文（日本語）
    let translated: String
}

/// 翻訳エラー
///
/// ユーザーに出す文言は `userMessage`。原因の違い（認証／接続／レート）が
/// 文言で区別できるようにしている。
@available(macOS 26.0, *)
enum TranslationError: Error, CustomStringConvertible {
    /// API キーが見つからない
    case missingAPIKey
    /// ネットワークに繋がらない
    case network(String)
    /// キーが無効（401 / 403）
    case unauthorized
    /// レート制限・過負荷（429 / 529）
    case rateLimited(retryAfter: TimeInterval?)
    /// サーバ側エラー
    case serverError(status: Int, message: String)
    /// リクエストが不正（400 など）
    case badRequest(message: String)
    /// 応答を解釈できない
    case badResponse(String)
    /// オンデバイス翻訳が使えない（言語モデル未導入・非対応など）
    case translationUnavailable(String)

    var description: String {
        switch self {
        case .missingAPIKey:
            return "APIキーが未設定"
        case let .network(detail):
            return "ネットワークエラー: \(detail)"
        case .unauthorized:
            return "認証エラー（APIキーが無効）"
        case let .rateLimited(retryAfter):
            return "レート制限（retry-after=\(retryAfter.map { String(format: "%.1f", $0) } ?? "なし")）"
        case let .serverError(status, message):
            return "サーバエラー(\(status)): \(message)"
        case let .badRequest(message):
            return "リクエストエラー: \(message)"
        case let .badResponse(detail):
            return "応答を解釈できません: \(detail)"
        case let .translationUnavailable(detail):
            return "オンデバイス翻訳を利用できません: \(detail)"
        }
    }

    /// ユーザー向けの短い説明（メニューや設定画面に出す）
    ///
    /// クラウドのエンジンが複数（Gemini / Groq）になったため、文言は
    /// 特定サービス名ではなく「クラウド翻訳」で共通化している。
    var userMessage: String {
        switch self {
        case .missingAPIKey:
            return "クラウド翻訳のAPIキーが設定されていません。メニューの「APIキーを設定…」から登録するか、翻訳エンジンを Apple に戻してください。"
        case .network:
            return "翻訳APIのサーバーに接続できません。ネットワーク接続を確認してください。"
        case .unauthorized:
            return "クラウド翻訳のAPIキーが無効です。メニューの「APIキーを設定…」から入力し直してください。"
        case .rateLimited:
            return "リクエストが混み合っています（レート制限）。しばらく待つと自動で復帰します。"
        case let .serverError(status, _):
            return "翻訳API側でエラーが発生しました（\(status)）。しばらく待つと復帰します。"
        case let .badRequest(message):
            return "リクエストが拒否されました: \(message)"
        case .badResponse:
            return "翻訳の応答を読み取れませんでした。"
        case let .translationUnavailable(detail):
            return "Apple のオンデバイス翻訳を利用できません: \(detail)"
        }
    }
}

/// 英語 → 日本語の翻訳器
@available(macOS 26.0, *)
protocol Translator: Sendable {
    /// 1 文（または 1 まとまり）を訳す
    ///
    /// - Parameters:
    ///   - text: 原文（英語）
    ///   - context: 直近の確定ペア（古い順）。文脈維持のために使う
    /// - Returns: 訳文（日本語）
    func translate(_ text: String, context: [TranslationExchange]) async throws -> String
}

/// 訳文を少しずつ受け取れる翻訳器
///
/// SSE（Server-Sent Events）に対応した API 用。訳し終わるのを待たずに
/// 届いた分から字幕へ流し込めるので、体感の遅延が縮む。
/// OpenAI 互換 API はすべて同じ形（`choices[0].delta.content`）なので、
/// Groq / OpenAI で同じ実装を使い回せる。
@available(macOS 26.0, *)
protocol StreamingTranslator: Translator {
    /// 1 文をストリーミングで訳す
    ///
    /// - Parameters:
    ///   - text: 原文（英語）
    ///   - context: 直近の確定ペア（古い順）
    ///   - onDelta: 追記分が届くたびに呼ばれる（任意スレッド）
    /// - Returns: 訳文の全文（日本語）
    func translateStreaming(
        _ text: String,
        context: [TranslationExchange],
        onDelta: @escaping @Sendable (String) -> Void
    ) async throws -> String
}

/// クラウド翻訳が失敗したら別の翻訳器へ落とす包み
///
/// クラウドは 429（レート制限）や瞬断で普通に失敗する。そのたびに字幕が原文のままに
/// なると使い物にならないので、Apple のオンデバイス翻訳へ落として必ず訳を出す
/// （恒久方針: クラウドは任意・既定は Apple・失敗時は Apple にフォールバック）。
@available(macOS 26.0, *)
struct FallbackTranslator: StreamingTranslator {
    /// 本命（クラウド）
    let primary: Translator
    /// 落とし先（Apple のオンデバイス翻訳）
    let fallback: Translator

    private var logger: Logger { makeCaptionLogger("FallbackTranslator") }

    func translate(_ text: String, context: [TranslationExchange]) async throws -> String {
        do {
            return try await primary.translate(text, context: context)
        } catch {
            return try await useFallback(text, context: context, reason: error)
        }
    }

    func translateStreaming(
        _ text: String,
        context: [TranslationExchange],
        onDelta: @escaping @Sendable (String) -> Void
    ) async throws -> String {
        guard let streaming = primary as? StreamingTranslator else {
            return try await translate(text, context: context)
        }
        do {
            return try await streaming.translateStreaming(text, context: context, onDelta: onDelta)
        } catch {
            return try await useFallback(text, context: context, reason: error)
        }
    }

    /// 落とし先で訳し直す（理由は必ず notice に残す）
    ///
    /// 落とし先まで失敗したときは元の失敗を投げる（原因が分からなくなるのを避けるため）。
    private func useFallback(
        _ text: String, context: [TranslationExchange], reason: Error
    ) async throws -> String {
        let detail = (reason as? TranslationError)?.description ?? String(describing: reason)
        logger.notice("クラウド翻訳に失敗したため Apple 翻訳へ切り替えます 理由=\(detail, privacy: .public)")
        do {
            return try await fallback.translate(text, context: context)
        } catch {
            throw reason
        }
    }
}

/// API キーが無い環境用のモック翻訳器
///
/// 実際の翻訳はせず、原文をそのまま印付きで返す。HUD 表示・流量制御・読み上げの
/// 結線が正しいかを、外部通信なしで検証するために使う。
@available(macOS 26.0, *)
struct MockTranslator: Translator {
    /// 実 API のレイテンシを粗く模した遅延（秒）
    let latency: TimeInterval

    init(latency: TimeInterval = 0.4) {
        self.latency = latency
    }

    func translate(_ text: String, context: [TranslationExchange]) async throws -> String {
        try? await Task.sleep(for: .seconds(latency))
        return "【モック訳】\(text)"
    }
}
