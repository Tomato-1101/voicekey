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

    /// 利用権（アクティベーションキー or サブスク）の状態（UI バインド用）
    enum Entitlement: Equatable {
        case unknown                        // 未確認（未ログイン時など）
        case checking                       // 確認中
        case active(Date?)                  // 有効（期限。無期限なら nil）
        case free(remaining: Int, quota: Int) // 無料体験中（残量あり。使い切るとキー入力が必要）
        case none                           // 無料体験を使い切り・未登録（キー入力が必要）
        case error(String)                  // 確認失敗
    }

    @Published private(set) var status: Status
    /// ログイン中アカウントの利用権の状態
    @Published private(set) var entitlement: Entitlement = .unknown
    /// ログイン中アカウントのメール（取得できれば）
    @Published private(set) var accountEmail: String?
    /// アクティベーションキー登録中フラグ（UI のボタン無効化用）
    @Published private(set) var redeeming = false
    /// 直近のキー登録エラー（成功時は nil）
    @Published private(set) var redeemError: String?

    /// CSRF 用の保留 state（ログイン開始〜deep link 受信の間だけ保持）
    private var pendingState: String?

    private init() {
        // 起動時に保存済みセッションがあればログイン済みから始める
        let loggedIn = (Keychain.authSession() != nil)
        status = loggedIn ? .loggedIn : .idle
        if loggedIn { refreshEntitlement() }
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
                refreshEntitlement()  // ログイン直後に利用権を確認
                // 各ストア（実績など）にログインを通知して端末横断同期を促す（#10）
                NotificationCenter.default.post(name: .voicekeyDidLogin, object: nil)
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
        entitlement = .unknown
        accountEmail = nil
        redeemError = nil
        // 各ストアにログアウトを通知して端末横断の取り込み結果を破棄させる（#10）
        NotificationCenter.default.post(name: .voicekeyDidLogout, object: nil)
    }

    // MARK: - 利用権（アクティベーションキー）

    /// ログイン中アカウントの利用権を再確認する（/api/v1/me）。
    /// UI を「確認中…」に一旦落としてから取り直す（明示操作・起動時用）。
    func refreshEntitlement() {
        guard Keychain.authSession() != nil else { entitlement = .unknown; return }
        entitlement = .checking
        Task { @MainActor in
            do {
                let s = try await BackendClient.fetchAccountStatus()
                applyStatus(s)
            } catch {
                let msg = (error as? BackendClient.BackendError)?.userMessage ?? "状態を確認できませんでした"
                entitlement = .error(msg)
            }
        }
    }

    /// 残量だけを静かに更新する（UI を「確認中…」に落とさない）。録音完了後に呼び、
    /// 「使うたびに残り回数が即減って見える」状態を担保する（消費自体はサーバーが原子的に
    /// 行うため、アプリは最新の残量を取り直すだけ）。利用権あり(paid)は録音で残量が変わら
    /// ないので無駄打ちしない。失敗は黙って無視（表示は前回値を保つ）。
    func refreshEntitlementQuiet() {
        guard Keychain.authSession() != nil else { return }
        if case .active = entitlement { return }  // 有料は残量非依存＝再取得しない
        Task { @MainActor in
            guard let s = try? await BackendClient.fetchAccountStatus() else { return }
            applyStatus(s)
        }
    }

    /// 取得済みのアカウント状態を表示状態へ反映する（refreshEntitlement / Quiet 共通）。
    private func applyStatus(_ s: BackendClient.AccountStatus) {
        accountEmail = s.email
        if s.active {
            entitlement = .active(s.activeUntil)
        } else if s.freeRemaining > 0 {
            // 無料体験中（まだ残量あり）。使い切ると .none に落ちてキー入力が要る。
            entitlement = .free(remaining: s.freeRemaining, quota: s.freeQuota)
        } else {
            entitlement = .none
        }
    }

    /// アクティベーションキーを登録する。成功すると利用権がアカウントに紐付き、表示も更新される。
    func redeem(code: String) {
        let trimmed = code.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { redeemError = "キーを入力してください"; return }
        guard !redeeming else { return }
        redeeming = true
        redeemError = nil
        Task { @MainActor in
            defer { redeeming = false }
            do {
                let until = try await BackendClient.redeemActivationKey(trimmed)
                entitlement = .active(until)
                accountEmail = accountEmail  // 変化なし（表示維持）
            } catch {
                redeemError = (error as? BackendClient.BackendError)?.userMessage ?? "キーを登録できませんでした"
            }
        }
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
