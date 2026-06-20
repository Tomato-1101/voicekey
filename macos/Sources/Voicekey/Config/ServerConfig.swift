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
    /// バックエンドのベース URL。配布ビルドは本番、開発ビルドは localhost。
    /// preview 検証などで切り替えたい場合は環境変数 VOICEKEY_SERVER_URL で上書き可。
    static var baseURL: String {
        if let override = ProcessInfo.processInfo.environment["VOICEKEY_SERVER_URL"],
           !override.isEmpty {
            return override.hasSuffix("/") ? String(override.dropLast()) : override
        }
        return EmbeddedKeys.isDist ? "https://voicekey.vercel.app" : "http://localhost:3000"
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

    /// ベース URL とパスを結合した完全な URL を返す
    static func url(_ path: String) -> URL? {
        URL(string: baseURL + path)
    }
}
