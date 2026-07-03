//
//  OnboardingView.swift
//  初回起動オンボーディング（Phase 5）
//
//  初回起動でアプリがバックグラウンドに黙って常駐すると「何これ？」となるため、
//  起動時にセットアップウィンドウを自動表示し、権限（マイク／アクセシビリティ／
//  入力監視）を順に案内・取得してからアプリ本体を開始する。
//  文言は Phase 4 で刷新した新語彙（録音キー／文字起こしモード／リアルタイム／
//  スタンダード／ハンズフリーキー／文章を自動で整える）に合わせる。
//
//  ウィンドウ生成は FeedbackView と同じ NSWindow + NSHostingController パターン
//  （生成・クローズは VoicekeyApp.swift の StatusItemController が管理する）。
//

import AppKit
import AVFoundation
import ApplicationServices
import Combine
import CoreGraphics
import SwiftUI

// MARK: - ステップ定義

/// オンボーディングのステップ（rawValue は中断再開用に UserDefaults へ保存する）。
enum OnboardingStep: Int, CaseIterable, Comparable {
    case welcome = 0        // ようこそ（概要）
    case microphone         // マイク権限
    case accessibility      // アクセシビリティ権限
    case inputMonitoring    // 入力監視権限
    case login              // ログイン（あとで可）
    case done               // 完了（使い方）

    static func < (lhs: OnboardingStep, rhs: OnboardingStep) -> Bool {
        lhs.rawValue < rhs.rawValue
    }
}

// MARK: - 起動時の分岐判定（純粋ロジック・テスト対象）

/// アプリ起動時にオンボーディングをどう扱うか。
enum OnboardingLaunchAction: Equatable {
    /// 指定ステップからオンボーディングを表示する（本体 startup は表示完了まで遅延）
    case show(fromStep: Int)
    /// 既存ユーザー: 完了フラグを補完するだけで表示しない（本体はそのまま起動）
    case autoComplete
    /// 完了済み: 何もしない（本体はそのまま起動）
    case skip
}

/// オンボーディング表示要否を副作用なしで判定する（UserDefaults に触れないので単体テスト可能）。
enum OnboardingDecider {

    /// 起動時のアクションを決める。
    /// - Parameters:
    ///   - didCompleteOnboarding: オンボーディング完了フラグ
    ///   - didSetupLaunchAtLogin: ログイン時自動起動の登録済みフラグ（＝オンボーディング機能導入前から使っている既存ユーザーの目印）
    ///   - savedStep: 入力監視の再起動などで中断していた場合の再開ステップ（nil＝中断なし）
    static func launchAction(
        didCompleteOnboarding: Bool,
        didSetupLaunchAtLogin: Bool,
        savedStep: Int?
    ) -> OnboardingLaunchAction {
        // 中断再開が最優先: 入力監視の権限反映のためにプロセス再起動した場合など、
        // どの状態であってもその位置から必ず続きを表示する。
        if let savedStep, savedStep >= 0 {
            return .show(fromStep: savedStep)
        }
        // すでに完了しているなら二度と出さない。
        if didCompleteOnboarding {
            return .skip
        }
        // 完了フラグは無いが自動起動が登録済み＝機能導入前から使っている既存ユーザー。
        // いきなりセットアップを出すと戸惑うので、フラグを補完して以後出さない。
        if didSetupLaunchAtLogin {
            return .autoComplete
        }
        // 完全な初回起動（新規インストール）→ 最初から表示する。
        return .show(fromStep: 0)
    }
}

// MARK: - オンボーディングの状態モデル

/// オンボーディングの進行・権限状態を保持し、権限取得の導線を提供する。
@MainActor
final class OnboardingModel: ObservableObject {

    @Published var step: OnboardingStep

    // 各権限の取得状況（1 秒ポーリングで更新）
    @Published var micGranted = false
    @Published var micDenied = false        // 既に拒否済み（設定を開く導線に切替）
    @Published var axTrusted = false
    @Published var inputGranted = false      // 入力監視の権限そのもの
    @Published var tapCreatable = false      // 実際に CGEventTap を作れるか
    @Published var needsRestart = false      // 権限はあるがタップ作成に失敗＝再起動が必要

    /// 完了設定の表示（ステップ 6）に使う現在設定
    let config: ConfigStore
    /// 「使い始める」/ ウィンドウを閉じたときに呼ぶ（完了扱い）
    let onFinish: () -> Void
    /// 入力監視の反映にプロセス再起動が必要なときに呼ぶ
    let onRestart: () -> Void

    private var pollTimer: Timer?

    init(
        startStep: OnboardingStep,
        config: ConfigStore,
        onFinish: @escaping () -> Void,
        onRestart: @escaping () -> Void
    ) {
        self.step = startStep
        self.config = config
        self.onFinish = onFinish
        self.onRestart = onRestart
        // 現在ステップの権限状況を即時に一度取り込む（再開時に✓を最初から出すため）
        refreshCurrentPermission()
    }

    /// 1 秒間隔のポーリングを開始する（ウィンドウ表示中のみ）。
    func startPolling() {
        pollTimer?.invalidate()
        pollTimer = Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { [weak self] _ in
            // Timer はメイン RunLoop 上で発火するため、そのままメインアクターとして扱う
            MainActor.assumeIsolated { self?.refreshCurrentPermission() }
        }
    }

    /// ポーリングを止める（ウィンドウクローズ時）。
    func stopPolling() {
        pollTimer?.invalidate()
        pollTimer = nil
    }

    /// 現在ステップに対応する権限状態を読み直す。
    func refreshCurrentPermission() {
        switch step {
        case .microphone:
            let status = AVCaptureDevice.authorizationStatus(for: .audio)
            micGranted = (status == .authorized)
            micDenied = (status == .denied || status == .restricted)
        case .accessibility:
            axTrusted = AXIsProcessTrusted()
        case .inputMonitoring:
            inputGranted = CGPreflightListenEventAccess()
            if inputGranted {
                // 権限が付いていても、プロセス起動時にタップが作れなかった環境では
                // 再起動しないと有効化されないことが実測である。作成可否を確かめる。
                tapCreatable = HotkeyMonitor.canCreateEventTap()
                needsRestart = !tapCreatable
            } else {
                tapCreatable = false
                needsRestart = false
            }
        default:
            break
        }
    }

    // MARK: - 権限取得の導線

    /// マイク許可を要求する。未決定ならシステムダイアログ、拒否済みなら設定を開く。
    func requestMic() {
        let status = AVCaptureDevice.authorizationStatus(for: .audio)
        switch status {
        case .notDetermined:
            AVCaptureDevice.requestAccess(for: .audio) { granted in
                Task { @MainActor in
                    self.micGranted = granted
                    self.micDenied = !granted
                }
            }
        case .denied, .restricted:
            // 一度拒否するとダイアログは二度と出ないため、システム設定へ誘導する
            openSettings(pane: "Privacy_Microphone")
        default:
            micGranted = true
        }
    }

    /// アクセシビリティの許可プロンプトを出す（既に信頼済みなら何も起きない）。
    func requestAccessibility() {
        let key = kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String
        let options = [key: true] as CFDictionary
        _ = AXIsProcessTrustedWithOptions(options)
    }

    /// 入力監視の許可を要求する。未許可なら OS へ登録＋プロンプト、許可済みならタップ作成を試す。
    func requestInputMonitoring() {
        if CGPreflightListenEventAccess() {
            inputGranted = true
            tapCreatable = HotkeyMonitor.canCreateEventTap()
            needsRestart = !tapCreatable
        } else {
            // 呼ぶとアプリが「入力監視」リストへ登録され、システムのプロンプトが出る
            _ = CGRequestListenEventAccess()
        }
    }

    /// 指定の権限ペインをシステム設定で開く。
    func openSettings(pane: String) {
        if let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?\(pane)") {
            NSWorkspace.shared.open(url)
        }
    }

    // MARK: - ステップ遷移

    /// 次のステップへ進む。
    func goNext() {
        if let next = OnboardingStep(rawValue: step.rawValue + 1) {
            step = next
            refreshCurrentPermission()
        }
    }

    /// 前のステップへ戻る。
    func goBack() {
        if let prev = OnboardingStep(rawValue: step.rawValue - 1) {
            step = prev
            refreshCurrentPermission()
        }
    }
}

// MARK: - オンボーディング画面

/// 初回セットアップの案内画面（6 ステップ）。
struct OnboardingView: View {

    @ObservedObject var model: OnboardingModel
    /// ログイン状態は司令塔（アプリ全体で 1 つ）を購読して✓を出す
    @ObservedObject private var login = LoginCoordinator.shared

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            ScrollView {
                content
                    .padding(.horizontal, 40)
                    .padding(.vertical, 28)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            Divider()
            footer
        }
        .clipShape(RoundedRectangle(cornerRadius: 22))  // ヘッダ/フッタが島の角からはみ出さないように
        .glassIsland(cornerRadius: 22)                  // 中央 1 島（影＋リムで浮かせる）
        .padding(20)                                    // 島の四周に backdrop を見せる
        // 島マージン分だけウィンドウを拡張（620x548 → 660x588）
        // 高さは fullSizeContentView でタイトルバーと一体化した分の実効高減（約 28pt）を補正済み
        .frame(width: 660, height: 588)
        .glassButtons()             // 配下の Button を一括ガラス化（戻る・あとで等の副ボタンはこれを継承）
        .frostedWindowBackground()  // ウィンドウ全面のすりガラス下地
        .onAppear { model.startPolling() }
        .onDisappear { model.stopPolling() }
    }

    // MARK: ヘッダー（タイトル + ステップインジケーター）

    private var header: some View {
        VStack(spacing: 14) {
            Text("VoiceKey へようこそ")
                .font(.system(size: 15, weight: .semibold))
            stepIndicator
        }
        .padding(.vertical, 16)
        .frame(maxWidth: .infinity)
    }

    /// 6 ステップのドット表示（現在＝アクセント／通過済み＝塗り）。
    private var stepIndicator: some View {
        HStack(spacing: 8) {
            ForEach(OnboardingStep.allCases, id: \.rawValue) { s in
                Capsule()
                    .fill(color(for: s))
                    .frame(width: s == model.step ? 22 : 8, height: 8)
                    .animation(.easeInOut(duration: 0.2), value: model.step)
            }
        }
        // ドット列をひとまとまりのガラスチップにする（ドット自体の色ロジックは不変）
        .padding(.horizontal, 12)
        .padding(.vertical, 7)
        .glassCapsule()
    }

    private func color(for s: OnboardingStep) -> Color {
        if s == model.step { return .accentColor }
        return s < model.step ? Color.accentColor.opacity(0.5) : Color.secondary.opacity(0.25)
    }

    // MARK: 本文（ステップごと）

    @ViewBuilder
    private var content: some View {
        switch model.step {
        case .welcome: welcomeStep
        case .microphone: microphoneStep
        case .accessibility: accessibilityStep
        case .inputMonitoring: inputMonitoringStep
        case .login: loginStep
        case .done: doneStep
        }
    }

    // 1. ようこそ
    private var welcomeStep: some View {
        stepBody(
            icon: "waveform.circle.fill",
            title: "録音キーを押して話すだけ",
            body: "録音キーを押して話すだけ。離すとカーソル位置に文字が入ります。\n\nまずマイクなどの許可を順番に設定します。数十秒で終わります。"
        )
    }

    // 2. マイク
    private var microphoneStep: some View {
        stepBody(
            icon: "mic.fill",
            title: "マイクを許可",
            body: "あなたの声を文字にするために、マイクの使用を許可してください。録音は録音キーを押している間だけ行われます。",
            granted: model.micGranted,
            grantedText: "マイクを許可しました"
        )
    }

    // 3. アクセシビリティ
    private var accessibilityStep: some View {
        stepBody(
            icon: "figure.wave",
            title: "アクセシビリティを許可",
            body: "文字起こしの結果を、いま使っているアプリのカーソル位置へ貼り付けるために必要です。「許可する」を押し、システム設定で VoiceKey をオンにしてください。",
            granted: model.axTrusted,
            grantedText: "アクセシビリティを許可しました"
        )
    }

    // 4. 入力監視
    private var inputMonitoringStep: some View {
        stepBody(
            icon: "keyboard",
            title: "入力監視を許可",
            body: "録音キーの押し下げを検知するために必要です。「許可する」を押し、システム設定で VoiceKey をオンにしてください。",
            granted: model.inputGranted && model.tapCreatable,
            grantedText: "入力監視を許可しました",
            extra: {
                // 権限は付いたがタップ作成に失敗＝プロセス再起動が要る場合の案内
                if model.needsRestart {
                    VStack(alignment: .leading, spacing: 8) {
                        Label("反映にはアプリの再起動が必要です", systemImage: "arrow.clockwise.circle")
                            .font(.callout)
                            .foregroundStyle(.orange)
                        Button("アプリを再起動") { model.onRestart() }
                            .glassProminentButton()
                    }
                }
            }
        )
    }

    // 5. ログイン（あとで可）
    private var loginStep: some View {
        VStack(alignment: .leading, spacing: 16) {
            stepHeader(icon: "person.crop.circle", title: "ログイン")
            Text("ログインすると無料体験で文字起こしが使えます。ログインはブラウザで行います。あとで設定画面からでも可能です。")
                .font(.body)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)

            loginStatusRow

            if !isLoggedIn {
                Button("ブラウザでログイン") { login.beginLogin() }
                    .glassProminentButton()
            }
        }
    }

    /// ログイン状態の表示行（司令塔の status を購読）。
    @ViewBuilder
    private var loginStatusRow: some View {
        switch login.status {
        case .loggedIn:
            Label(login.accountEmail.map { "ログイン済み（\($0)）" } ?? "ログイン済み",
                  systemImage: "checkmark.circle.fill")
                .foregroundStyle(.green)
        case .waiting:
            Label("ブラウザでログインを完了してください…", systemImage: "safari")
                .foregroundStyle(.secondary)
        case .exchanging:
            Label("ログイン処理中…", systemImage: "arrow.triangle.2.circlepath")
                .foregroundStyle(.secondary)
        case .failed(let msg):
            Label(msg, systemImage: "exclamationmark.triangle.fill")
                .foregroundStyle(.orange)
        case .idle:
            EmptyView()
        }
    }

    // 6. 完了（使い方カード）
    private var doneStep: some View {
        VStack(alignment: .leading, spacing: 16) {
            stepHeader(icon: "checkmark.seal.fill", title: "準備ができました")
            Text("下の録音キーを押して話すと、その場に文字が入ります。設定はメニューバーのアイコンからいつでも変えられます。")
                .font(.body)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)

            // 実際の既定設定を ConfigStore から読んで表示する（ハードコードしない）
            VStack(spacing: 10) {
                usageCard(slotLabel: "録音キー 1（メイン）", slot: model.config.slot1)
                usageCard(slotLabel: "録音キー 2（サブ）", slot: model.config.slot2)
            }
        }
    }

    /// 録音キー 1 つ分の使い方カード（キー表記・文字起こしモード・録音のしかた）。
    private func usageCard(slotLabel: String, slot: SlotConfig) -> some View {
        HStack(spacing: 12) {
            Image(systemName: "command")
                .font(.title2)
                .foregroundStyle(.tint)
                .frame(width: 32)
            VStack(alignment: .leading, spacing: 3) {
                Text(slotLabel)
                    .font(.subheadline).bold()
                Text("キー: \(slot.hotkeyLabel) ／ \(slot.backend.label) ／ \(slot.mode.label)")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            }
            Spacer()
        }
        .padding(12)
        .glassSurface(cornerRadius: 10)  // 使い方カードをガラス面に
    }

    // MARK: フッター（ナビゲーションボタン）

    private var footer: some View {
        HStack {
            // 戻る（最初のステップ以外）
            if model.step != .welcome {
                Button("戻る") { model.goBack() }
            }
            Spacer()
            footerPrimaryButtons
        }
        .padding(.horizontal, 20)
        .padding(.vertical, 14)
    }

    @ViewBuilder
    private var footerPrimaryButtons: some View {
        switch model.step {
        case .welcome:
            Button("セットアップを始める") { model.goNext() }
                .glassProminentButton()
                .keyboardShortcut(.defaultAction)

        case .microphone:
            permissionButtons(
                granted: model.micGranted,
                denied: model.micDenied,
                deniedPane: "Privacy_Microphone",
                request: { model.requestMic() }
            )

        case .accessibility:
            permissionButtons(
                granted: model.axTrusted,
                denied: false,
                deniedPane: "Privacy_Accessibility",
                request: { model.requestAccessibility() },
                openSettingsPane: "Privacy_Accessibility"
            )

        case .inputMonitoring:
            // 再起動が必要なときは進めない（再起動ボタンは本文側に出している）
            permissionButtons(
                granted: model.inputGranted && model.tapCreatable,
                denied: false,
                deniedPane: "Privacy_ListenEvent",
                request: { model.requestInputMonitoring() },
                openSettingsPane: "Privacy_ListenEvent"
            )

        case .login:
            HStack(spacing: 10) {
                if isLoggedIn {
                    Button("次へ") { model.goNext() }
                        .glassProminentButton()
                        .keyboardShortcut(.defaultAction)
                } else {
                    Button("あとで") { model.goNext() }
                }
            }

        case .done:
            Button("使い始める") { model.onFinish() }
                .glassProminentButton()
                .keyboardShortcut(.defaultAction)
        }
    }

    /// 権限ステップ共通のボタン群。
    /// - 許可済み → 「次へ」（primary）
    /// - 拒否済み → 「システム設定を開く」
    /// - 未許可  → 「許可する」＋（必要なら）「システム設定を開く」
    @ViewBuilder
    private func permissionButtons(
        granted: Bool,
        denied: Bool,
        deniedPane: String,
        request: @escaping () -> Void,
        openSettingsPane: String? = nil
    ) -> some View {
        HStack(spacing: 10) {
            if granted {
                Button("次へ") { model.goNext() }
                    .glassProminentButton()
                    .keyboardShortcut(.defaultAction)
            } else if denied {
                Button("システム設定を開く") { model.openSettings(pane: deniedPane) }
                    .glassProminentButton()
            } else {
                if let openSettingsPane {
                    Button("システム設定を開く") { model.openSettings(pane: openSettingsPane) }
                }
                Button("許可する") { request() }
                    .glassProminentButton()
            }
        }
    }

    // MARK: 補助ビュー

    /// ログイン済みか（ログイン or 利用権あり）。
    private var isLoggedIn: Bool {
        if case .loggedIn = login.status { return true }
        return false
    }

    /// 権限系ステップの本文（アイコン・見出し・説明・✓・追加要素）を組み立てる。
    private func stepBody(
        icon: String,
        title: String,
        body: String,
        granted: Bool = false,
        grantedText: String = "",
        @ViewBuilder extra: () -> some View = { EmptyView() }
    ) -> some View {
        VStack(alignment: .leading, spacing: 16) {
            stepHeader(icon: icon, title: title)
            Text(body)
                .font(.body)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            if granted {
                Label(grantedText, systemImage: "checkmark.circle.fill")
                    .font(.callout)
                    .foregroundStyle(.green)
            }
            extra()
        }
    }

    /// アイコン + 見出し。アイコンは「アプリらしい顔」を作るためガラス島の中でヒーロー化する。
    private func stepHeader(icon: String, title: String) -> some View {
        HStack(spacing: 14) {
            heroIcon(icon)
            Text(title)
                .font(.title2).bold()
        }
    }

    /// ステップのヒーローアイコン（アクセントのグラデ円＋白シンボル＋グロー影）。
    private func heroIcon(_ icon: String) -> some View {
        Image(systemName: icon)
            .font(.system(size: 26, weight: .semibold))
            .foregroundStyle(.white)
            .frame(width: 56, height: 56)
            .background(
                Circle().fill(LinearGradient(
                    colors: [Color.accentColor, Color.accentColor.opacity(0.7)],
                    startPoint: .topLeading, endPoint: .bottomTrailing
                ))
            )
            // 上縁の白リムでガラスの厚み、アクセントのグロー影で「光る顔」を作る
            .overlay(
                Circle().strokeBorder(LinearGradient(
                    colors: [.white.opacity(0.5), .white.opacity(0.0)],
                    startPoint: .top, endPoint: .bottom
                ), lineWidth: 1)
            )
            .shadow(color: Color.accentColor.opacity(0.45), radius: 10, x: 0, y: 4)
    }
}
