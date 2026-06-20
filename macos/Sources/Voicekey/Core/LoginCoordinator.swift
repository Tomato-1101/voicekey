//
//  LoginCoordinator.swift
//  ブラウザ経由ログインの司令塔（製品版・Phase 5 段階4・増分2）
//
//  ログイン開始（state 生成 → ブラウザを開く）と deep link 受信
//  （voicekey://auth?code=&state= の解析 → state 照合 → コード交換）を束ねる。
//  CSRF 対策の state は「ログイン開始〜deep link 受信」の間だけメモリに保持し、
//  戻ってきた state と一致しなければ無視する（トークンは URL に乗らずコード交換でのみ取得）。
//
//  URL スキーム登録（Info.plist）と OS からの URL 受信配線（AppDelegate）は増分3で行う。
//

import AppKit
import Foundation

@MainActor
final class LoginCoordinator: ObservableObject {

    /// アプリ全体で 1 つ（メニュー／設定 UI と deep link ハンドラが同じ保留 state を共有する）
    static let shared = LoginCoordinator()

    /// ログインの進行状態（UI バインド用）
    enum Status: Equatable {
        case idle           // 未ログイン・待機なし
        case waiting        // ブラウザでログイン待ち
        case exchanging     // コードをトークンに交換中
        case loggedIn       // ログイン済み
        case failed(String) // 失敗（ユーザー向けメッセージ）
    }

    @Published private(set) var status: Status

    /// CSRF 用の保留 state（ログイン開始〜deep link 受信の間だけ保持）
    private var pendingState: String?

    private init() {
        // 起動時に保存済みセッションがあればログイン済みから始める
        status = (Keychain.authSession() != nil) ? .loggedIn : .idle
    }

    /// ログインを開始する: state を生成し、既定ブラウザでログインページを開く。
    func beginLogin() {
        let state = AuthClient.makeState()
        pendingState = state
        guard let url = AuthClient.makeLoginURL(state: state) else {
            status = .failed("ログインURLを生成できませんでした")
            return
        }
        status = .waiting
        NSWorkspace.shared.open(url)
    }

    /// deep link を処理する: URL を解析 → state 照合 → コード交換。
    /// 戻り値は「この URL が自分（ログイン）宛てとして処理されたか」。
    /// 別用途の voicekey:// URL は false を返し、呼び出し側で無視させる。
    @discardableResult
    func handleDeepLink(_ url: URL) -> Bool {
        guard let parsed = Self.parseAuthURL(url) else { return false }
        // state 照合（CSRF）。保留 state と一致しなければ受け付けない。
        guard let pending = pendingState, parsed.state == pending else {
            status = .failed("ログイン要求が一致しませんでした")
            return true
        }
        pendingState = nil
        status = .exchanging
        Task { @MainActor in
            do {
                _ = try await AuthClient.exchange(code: parsed.code)
                status = .loggedIn
            } catch {
                let msg = (error as? AuthClient.AuthError)?.userMessage ?? "ログインに失敗しました"
                status = .failed(msg)
            }
        }
        return true
    }

    /// ログアウト（セッション破棄）。
    func logout() {
        AuthClient.logout()
        pendingState = nil
        status = .idle
    }

    /// voicekey://auth?code=&state= を解析する（純粋関数）。
    /// scheme/host が一致し code・state が揃っていれば (code, state) を返す。
    static func parseAuthURL(_ url: URL) -> (code: String, state: String)? {
        guard url.scheme == "voicekey",
              url.host == "auth",
              let comps = URLComponents(url: url, resolvingAgainstBaseURL: false) else {
            return nil
        }
        let items = comps.queryItems ?? []
        guard let code = items.first(where: { $0.name == "code" })?.value, !code.isEmpty,
              let state = items.first(where: { $0.name == "state" })?.value, !state.isEmpty else {
            return nil
        }
        return (code, state)
    }
}
