//
//  ServerConfig.swift
//  自社バックエンド（製品版: 短命キー発行・プロキシ）の接続先
//
//  製品版（release）は長期 API キーをアプリに同梱せず、自社サーバーがサブスク
//  有効性を検証して Deepgram の短命トークンを発行し、ElevenLabs/Groq はサーバー
//  プロキシ経由で叩く。ここはその接続先（ユーザー設定ではなくビルド依存の定数）。
//  Windows 版（src/config/constants.py + utils/secrets.get_server_base_url）と
//  値・意味を揃える。
//

import Foundation

enum ServerConfig {
    /// バックエンドのベース URL を解決する（テスト可能な純関数）。
    /// 配布ビルドは本番固定で、環境変数 override を無視する（接続先汚染を防ぐ）。
    /// 開発ビルドのみ preview 検証用に VOICEKEY_SERVER_URL での上書きを許可する。
    static func resolveBaseURL(isDist: Bool, override: String?) -> String {
        if !isDist, let override = override, !override.isEmpty {
            return override.hasSuffix("/") ? String(override.dropLast()) : override
        }
        return isDist ? "https://voicekey.vercel.app" : "http://localhost:3000"
    }

    /// バックエンドのベース URL。配布ビルドは本番、開発ビルドは localhost。
    static var baseURL: String {
        resolveBaseURL(
            isDist: EmbeddedKeys.isDist,
            override: ProcessInfo.processInfo.environment["VOICEKEY_SERVER_URL"]
        )
    }

    /// Deepgram 短命 JWT 発行
    static let ephemeralPath = "/api/v1/auth/ephemeral"
    /// 正確性モード（ElevenLabs scribe）のプロキシ
    static let elevenLabsProxyPath = "/api/v1/transcribe/elevenlabs"
    /// Groq テキスト整形プロキシ
    static let formatProxyPath = "/api/v1/format"
    /// アプリ内フィードバック送信（認証は任意＝未ログインでも送れる）
    static let feedbackPath = "/api/v1/feedback"

    // ブラウザ経由ログイン（段階4）
    /// ブラウザで開くログイン入口（state 付き）
    static let authAppPath = "/auth/app"
    /// ワンタイムコード → トークン交換
    static let exchangePath = "/api/v1/auth/exchange"
    /// refresh_token → トークン更新
    static let refreshPath = "/api/v1/auth/refresh"

    // アクティベーションキー（段階5）
    /// アクティベーションキーの登録（消費）。ログイン中アカウントに利用権を紐付ける
    static let redeemPath = "/api/v1/activation/redeem"
    /// ログイン中アカウントの状態（メール・利用権の有無/期限）取得
    static let mePath = "/api/v1/me"
    /// 無料体験の消費確定（録音成功後に保留 jti を送る＝段階1。録音開始の往復から消費を外す）
    static let usageConfirmPath = "/api/v1/usage/confirm"

    // 使用実績のアカウント連携（#10）
    /// 実績の送信（この端末の日次・絶対値を upsert）
    static let statsSyncPath = "/api/v1/stats/sync"
    /// 実績の取得（アカウント横断の日次集計）
    static let statsPath = "/api/v1/stats"

    /// ベース URL とパスを結合した完全な URL を返す
    static func url(_ path: String) -> URL? {
        URL(string: baseURL + path)
    }
}
