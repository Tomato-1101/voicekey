/// Apple 純正 Translation framework によるオンデバイス英→日翻訳（既定エンジン）
///
/// API キー不要・課金なし・ネットワーク不要。ライブ字幕の既定はこちらにしている。
///
/// セッションは `AppleTranslationHost`（画面外の SwiftUI ホスト）から受け取る。
/// `TranslationSession(installedSource:target:)` で直接作る方が簡単だが、そちらは
/// `canRequestDownloads` が false で言語モデルの導入を要求できない（本アプリで実測）。
/// 直接生成はホストが使えなかった場合のフォールバックとしてのみ残している。
///
/// セッションは 1 本を使い回す。1 文ごとに作り直すとモデルのロードが毎回走って
/// 字幕が間に合わなくなるため。
import Foundation
import OSLog
@preconcurrency import Translation

/// en→ja の言語モデルが使える状態か
@available(macOS 26.0, *)
enum AppleTranslationReadiness {
    /// すぐ翻訳できる
    case ready
    /// 言語モデルのダウンロードが必要（OS の確認 UI が出ることがある）
    case needsDownload
    /// この言語ペアに対応していない
    case unsupported
    /// 準備中に失敗した
    case failed(String)

    /// メニュー・HUD に出す説明
    var message: String {
        switch self {
        case .ready: return "Apple 翻訳: 利用可能"
        case .needsDownload: return "英→日の翻訳モデルをダウンロード中…"
        case .unsupported: return "この Mac は英→日の Apple 翻訳に対応していません"
        case let .failed(detail): return "Apple 翻訳を準備できません: \(detail)"
        }
    }
}

/// Apple のオンデバイス翻訳器
///
/// `TranslationSession` はスレッド安全が保証されないため actor に閉じ込める。
@available(macOS 26.0, *)
actor AppleTranslator: Translator {

    /// 翻訳元・翻訳先（本アプリは英→日固定）
    private static let sourceLanguage = Locale.Language(identifier: "en")
    private static let targetLanguage = Locale.Language(identifier: "ja")

    private let logger = makeCaptionLogger("AppleTranslator")
    private let availability = LanguageAvailability()
    private var session: TranslationSession?
    /// 準備完了を確認済みか（毎回 isReady を待つと 1 文ごとに余計な往復が入る）
    private var isPrepared = false

    /// 既に導入済みの場合だけ準備する（ダウンロード承認 UI を絶対に出さない）
    ///
    /// CLI ハーネス用。ハーネスでユーザー操作待ちの UI を出すと計測が止まってしまうため、
    /// 「導入済みなら使う・未導入なら諦める」に限定した入口を分けている。
    ///
    /// - Returns: 準備できたか
    func prepareIfInstalled() async -> Bool {
        if isPrepared { return true }
        let status = await availability.status(from: Self.sourceLanguage, to: Self.targetLanguage)
        logger.notice("Apple 翻訳の言語状態(ハーネス): \(String(describing: status), privacy: .public)")
        guard status == .installed else { return false }
        let session = await currentSession()
        guard await session.isReady else { return false }
        isPrepared = true
        return true
    }

    /// 言語モデルの状態を確認し、必要ならダウンロードを要求する
    ///
    /// 字幕開始時に 1 回呼ぶ。ダウンロードが要る場合は OS 側の確認 UI が出ることがあり、
    /// 完了まで数十秒以上かかる。その間を無表示にしないため、待ちに入る前に
    /// `report` で `.needsDownload` を通知してから `prepareTranslation()` を待つ。
    ///
    /// - Parameter report: 途中経過の通知（画面表示用）
    /// - Returns: 最終的な準備状況
    func prepare(report: @Sendable (AppleTranslationReadiness) -> Void = { _ in }) async -> AppleTranslationReadiness {
        let status = await availability.status(from: Self.sourceLanguage, to: Self.targetLanguage)
        logger.notice("Apple 翻訳の言語状態: \(String(describing: status), privacy: .public)")

        switch status {
        case .unsupported:
            return .unsupported
        case .installed, .supported:
            break
        @unknown default:
            break
        }

        let session = await currentSession()
        if await session.isReady {
            isPrepared = true
            return .ready
        }

        // 未インストールならここでダウンロードを要求する。
        // `canRequestDownloads` が false の環境ではダウンロードを頼めないため、
        // 「準備できません」として理由ごと返す（無言で字幕が出ないのが一番困る）。
        guard session.canRequestDownloads else {
            logger.notice("翻訳モデルのダウンロードを要求できません（canRequestDownloads=false）")
            return .failed("システム設定 > 一般 > 言語と地域 > 翻訳言語 で英語と日本語を追加してください")
        }

        logger.notice("英→日の翻訳モデルを準備します（未インストール）")
        report(.needsDownload)
        // OS のダウンロード承認 UI はウィンドウが実際に見えていないと出せないため、
        // ここだけホストを画面に出す（承認が終わるまで戻ってこない）
        await AppleTranslationHost.presentForApproval()
        defer { Task { await AppleTranslationHost.dismissApproval() } }

        do {
            try await session.prepareTranslation()
            isPrepared = true
            logger.notice("英→日の翻訳モデルの準備が完了しました")
            return .ready
        } catch {
            logger.error("翻訳モデルの準備に失敗: \(String(describing: error), privacy: .public)")
            return .failed(String(describing: error))
        }
    }

    /// 英文を日本語へ訳す
    ///
    /// - Parameters:
    ///   - text: 原文（英語）
    ///   - context: 直近の文脈。**Apple の API は文脈を受け取れないため使わない**
    ///     （LLM エンジンと同じ `Translator` で扱うためにシグネチャだけ合わせている）
    /// - Returns: 訳文（日本語）
    func translate(_ text: String, context: [TranslationExchange]) async throws -> String {
        // ここでは準備（＝承認 UI 待ちで戻ってこない可能性がある処理）を起動しない。
        // 起動すると翻訳キューが承認待ちで詰まり、字幕が丸ごと止まってしまう。
        // 準備は開始時とエンジン切替時の `prepare(report:)` だけが行う。
        guard isPrepared else {
            throw TranslationError.translationUnavailable("翻訳モデルの準備が完了していません")
        }
        let session = await currentSession()

        do {
            return try await session.translate(text).targetText
        } catch {
            // モデルが消えた・セッションが無効化された場合に備え、次回作り直す
            self.session = nil
            isPrepared = false
            throw TranslationError.translationUnavailable(String(describing: error))
        }
    }

    // MARK: - 内部処理

    /// セッションを取得する（無ければ作る）
    ///
    /// 優先は SwiftUI ホスト経由（ダウンロードを要求できる）。
    /// 取れなかった場合だけ直接生成にフォールバックする（導入済みなら翻訳はできる）。
    private func currentSession() async -> TranslationSession {
        if let session { return session }
        if let hosted = await AppleTranslationHost.session(
            source: Self.sourceLanguage, target: Self.targetLanguage
        ) {
            session = hosted
            return hosted
        }
        logger.notice("SwiftUI ホストからセッションを取得できないため直接生成に切り替えます")
        let created = TranslationSession(
            installedSource: Self.sourceLanguage, target: Self.targetLanguage
        )
        session = created
        return created
    }
}
