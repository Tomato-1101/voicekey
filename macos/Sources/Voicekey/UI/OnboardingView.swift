//
//  OnboardingView.swift
//  初回起動オンボーディング（フルウィンドウ・2 ペイン再デザイン）
//
//  初回起動でアプリがバックグラウンドに黙って常駐すると「何これ？」となるため、
//  メインウィンドウ内でガイドを全面表示し、権限（マイク／アクセシビリティ／入力監視）を
//  1 ステップ 1 個ずつ案内・取得してから、マイクテスト・ホットキーテスト・実際の音声入力体験まで
//  やってもらう。完了でホーム（ダッシュボード）に着地する。
//
//  権限プロンプトの直列化（最重要）: TCC プロンプトを誘発する処理は各権限ステップの「許可する」
//  ボタンでしか呼ばない。本体の録音／入力監視サブシステムはオンボーディング中は起動せず、
//  マイクテスト／ホットキーテスト／練習の各ステップで対応サブシステムだけを起動する
//  （AppController.startMicSubsystem / startHotkeySubsystem）。これで「初回起動で権限ポップアップが
//  一気に複数出る」事故を防ぐ。
//
//  レイアウト: 左＝白磁のコンテンツ（大見出し・短い本文・黒ピル CTA）、右＝淡いパステルウォッシュ＋
//  実物大のモックアップ（レベルメーター・巨大キー・擬似アプリ）。全画面インタースティシャルは
//  セクションの区切りに使う。ガラスは Glass.swift の共通トークンを使い、CTA だけはこの画面専用の
//  黒ピル（Typeless 参照）にする（紫・ネオンは使わない）。
//

import AppKit
import AVFoundation
import ApplicationServices
import Combine
import CoreGraphics
import SwiftUI

// MARK: - ステップ定義

/// オンボーディングのステップ（rawValue は中断再開用に UserDefaults へ保存する）。
///
/// ようこそ → 権限 3 つ → ログイン → 動作確認（マイク／ホットキーのテスト）→ 体験（練習 3 種）→ まとめ。
enum OnboardingStep: Int, CaseIterable, Comparable {
    case welcome = 0        // ようこそ（全画面インタースティシャル）
    case microphone         // マイク権限
    case accessibility      // アクセシビリティ権限
    case inputMonitoring    // 入力監視権限
    case login              // ログイン（あとで可）
    case checkIntro         // 動作確認のインタースティシャル
    case micTest            // マイクテスト（レベルメーター＋デバイス選択）
    case hotkeyTest         // ホットキーテスト（巨大キーが点灯）
    case practiceBasic      // 体験1: 即時入力（例文を読んで入力してみる）
    case practiceHandsfree  // 体験2: ハンズフリー（トグル録音）
    case practiceFormat     // 体験3: 文章整形（フィラーが消える）
    case summary            // まとめ（話して、タイプしないで）

    /// 権限ステップか。
    var isPermission: Bool {
        self == .microphone || self == .accessibility || self == .inputMonitoring
    }

    /// 体験（練習）ステップか。エンジン起動・スキップ導線の判定に使う。
    var isPractice: Bool {
        self == .practiceBasic || self == .practiceHandsfree || self == .practiceFormat
    }

    /// 全画面インタースティシャル（2 ペインにせず中央に大きく見せる区切り画面）か。
    var isInterstitial: Bool {
        self == .welcome || self == .checkIntro || self == .summary
    }

    /// ブレッドクラムの所属フェーズ（0=ようこそ / 1=準備 / 2=体験）。
    var phase: Int {
        switch self {
        case .welcome: return 0
        case .microphone, .accessibility, .inputMonitoring, .login: return 1
        default: return 2
        }
    }

    static func < (lhs: OnboardingStep, rhs: OnboardingStep) -> Bool {
        lhs.rawValue < rhs.rawValue
    }
}

// MARK: - 体験ステップの成功判定（純ロジック・テスト対象）

/// 体験（練習）ステップの成功判定を副作用なしで行う（SwiftUI に依存しないので単体テスト可能）。
/// Windows 版 onboarding_window._has_practice_input と同じ観点。
enum OnboardingPractice {
    /// 練習欄に「文字が入った」と言えるか（前後の空白を除いて 1 文字以上）。
    static func hasInput(_ text: String) -> Bool {
        !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
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
    ///   - didSetupLaunchAtLogin: ログイン時自動起動の登録済みフラグ（＝オンボーディング機能導入前からの既存ユーザーの目印）
    ///   - savedStep: 入力監視の再起動などで中断していた場合の再開ステップ（nil＝中断なし）
    static func launchAction(
        didCompleteOnboarding: Bool,
        didSetupLaunchAtLogin: Bool,
        savedStep: Int?
    ) -> OnboardingLaunchAction {
        // 中断再開が最優先。
        if let savedStep, savedStep >= 0 {
            return .show(fromStep: savedStep)
        }
        if didCompleteOnboarding {
            return .skip
        }
        // 完了フラグは無いが自動起動が登録済み＝機能導入前から使っている既存ユーザー。
        if didSetupLaunchAtLogin {
            return .autoComplete
        }
        // 完全な初回起動（新規インストール）→ 最初から表示する。
        return .show(fromStep: 0)
    }
}

// MARK: - オンボーディングの状態モデル

/// オンボーディングの進行・権限・動作確認・体験の状態を保持し、各サブシステム起動の導線を提供する。
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

    // マイクテスト
    @Published var micLevel: Float = 0       // 0.0–1.0（モニタ中のレベル）
    @Published var micSignalSeen = false     // レベルがしきい値を超えた＝「動いた」
    @Published var selectedDeviceUID: String // 選択中の入力デバイス（"" = システム既定）
    @Published var inputDevices: [AudioInputDevice] = []
    @Published var showDevicePicker = false  // 「マイクを変更」で表示する

    // ホットキーテスト
    @Published var hotkeyHeldSlot: Int?      // 押されている録音キーのスロット（点灯表示に使う）
    @Published var hotkeySignalSeen = false  // 一度でも押された＝「効いた」

    // 体験（練習）ステップの成功状態
    @Published var basicPracticeDone = false
    @Published var handsfreePracticeDone = false
    @Published var formatPracticeDone = false

    /// 完了設定の表示（まとめ）に使う現在設定
    let config: ConfigStore
    /// 「はじめる」/ ウィンドウを閉じたときに呼ぶ（完了扱い＝ホームへ着地）
    let onFinish: () -> Void
    /// 入力監視の反映にプロセス再起動が必要なときに呼ぶ
    let onRestart: () -> Void
    /// 体験ステップに入ったら録音に必要なサブシステム（マイク＋入力監視＋暖機）を起動する（冪等）。
    let startEngineForPractice: () -> Void
    /// 整形体験ステップの間だけ整形を強制 ON にする一時オーバーライドを切り替える（保存しない）。
    let setFormatOverride: (Bool) -> Void
    /// マイクテスト: 録音せずレベルだけを流すモニタを開始する（引数のクロージャへレベルが届く）。
    let startMicMonitor: (@escaping (Float) -> Void) -> Void
    /// マイクテスト: モニタを停止する。
    let stopMicMonitor: () -> Void
    /// マイクテスト: 入力デバイスを切り替える（uid, モニタ中か）。config へも保存する。
    let setMicDevice: (String, Bool) -> Void
    /// ホットキーテスト: テストモードの ON/OFF と、押されたスロット変化のコールバック登録。
    let setHotkeyTest: (Bool, @escaping (Int?) -> Void) -> Void
    /// テスト用の一時状態（マイクモニタ・ホットキーテスト・整形オーバーライド）を全解除する。
    let teardown: () -> Void

    private var pollTimer: Timer?
    /// 練習エンジン起動を一度だけにするフラグ
    private var engineStarted = false

    init(
        startStep: OnboardingStep,
        config: ConfigStore,
        onFinish: @escaping () -> Void,
        onRestart: @escaping () -> Void,
        startEngineForPractice: @escaping () -> Void = {},
        setFormatOverride: @escaping (Bool) -> Void = { _ in },
        startMicMonitor: @escaping (@escaping (Float) -> Void) -> Void = { _ in },
        stopMicMonitor: @escaping () -> Void = {},
        setMicDevice: @escaping (String, Bool) -> Void = { _, _ in },
        setHotkeyTest: @escaping (Bool, @escaping (Int?) -> Void) -> Void = { _, _ in },
        teardown: @escaping () -> Void = {}
    ) {
        self.step = startStep
        self.config = config
        self.onFinish = onFinish
        self.onRestart = onRestart
        self.startEngineForPractice = startEngineForPractice
        self.setFormatOverride = setFormatOverride
        self.startMicMonitor = startMicMonitor
        self.stopMicMonitor = stopMicMonitor
        self.setMicDevice = setMicDevice
        self.setHotkeyTest = setHotkeyTest
        self.teardown = teardown
        self.selectedDeviceUID = config.inputDeviceUID
        refreshCurrentPermission()
        handleStepEntered()
    }

    /// 1 秒間隔のポーリングを開始する（ウィンドウ表示中のみ）。権限ステップの✓更新に使う。
    func startPolling() {
        pollTimer?.invalidate()
        pollTimer = Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { [weak self] _ in
            MainActor.assumeIsolated { self?.refreshCurrentPermission() }
        }
    }

    /// ポーリングを止め、テスト用の一時状態も解除する（ウィンドウクローズ時）。
    func stopPolling() {
        pollTimer?.invalidate()
        pollTimer = nil
        teardown()
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

    func goNext() {
        if let next = OnboardingStep(rawValue: step.rawValue + 1) {
            step = next
            refreshCurrentPermission()
            handleStepEntered()
        }
    }

    func goBack() {
        if let prev = OnboardingStep(rawValue: step.rawValue - 1) {
            step = prev
            refreshCurrentPermission()
            handleStepEntered()
        }
    }

    /// 体験・確認をスキップしてまとめへ飛ぶ（強制しない）。
    func skipToSummary() {
        step = .summary
        handleStepEntered()
    }

    /// マイクテストの入力デバイスを切り替える。
    func selectDevice(_ uid: String) {
        selectedDeviceUID = uid
        micSignalSeen = false
        setMicDevice(uid, step == .micTest)
    }

    /// ステップに入った瞬間の副作用（サブシステム起動・テストモード切替）をまとめて行う。
    private func handleStepEntered() {
        // マイクテスト: モニタ開始 / それ以外: モニタ停止
        if step == .micTest {
            inputDevices = AudioDevices.inputDevices()
            selectedDeviceUID = config.inputDeviceUID
            micLevel = 0
            micSignalSeen = false
            showDevicePicker = false
            startMicMonitor { [weak self] level in
                guard let self else { return }
                self.micLevel = level
                if level > 0.06 { self.micSignalSeen = true }
            }
        } else {
            stopMicMonitor()
        }

        // ホットキーテスト: テストモード ON（押下で録音せず点灯）/ それ以外: OFF
        if step == .hotkeyTest {
            hotkeyHeldSlot = nil
            hotkeySignalSeen = false
            setHotkeyTest(true) { [weak self] slot in
                guard let self else { return }
                self.hotkeyHeldSlot = slot
                if slot != nil { self.hotkeySignalSeen = true }
            }
        } else {
            setHotkeyTest(false) { _ in }
            hotkeyHeldSlot = nil
        }

        // 練習ステップに入ったら録音に必要なサブシステムを起動する（権限は取得済み・冪等）。
        if step.isPractice, !engineStarted {
            engineStarted = true
            startEngineForPractice()
        }
        // 整形体験ステップの間だけ整形を強制 ON。それ以外では必ず解除。
        setFormatOverride(step == .practiceFormat)
    }

    // MARK: - 表示ヘルパ

    /// ログイン済みか（ログイン or 利用権あり）。
    var isLoggedIn: Bool {
        if case .loggedIn = LoginCoordinator.shared.status { return true }
        return false
    }

    /// トグル（ハンズフリー）録音に使う録音キー。既定はサブ(右⌥・toggle)。
    var handsfreeSlot: SlotConfig {
        if config.slot2.mode == .toggle { return config.slot2 }
        if config.slot1.mode == .toggle { return config.slot1 }
        return config.slot2
    }
}

// MARK: - この画面専用の配色（Porcelain 白磁＋淡いパステルウォッシュ・紫は使わない）

private enum OB {
    /// 白磁（左ペイン・ルート）。ほんのり暖色のオフホワイト／ダークは無彩色チャコール。
    static func porcelain(_ s: ColorScheme) -> Color {
        s == .dark ? Color(red: 0.115, green: 0.115, blue: 0.125)
                   : Color(red: 0.988, green: 0.986, blue: 0.984)
    }
    /// 右ペインの淡いパステルウォッシュ（低彩度・青みグレー→暖色。紫は避ける）。
    static func wash(_ s: ColorScheme) -> [Color] {
        s == .dark
            ? [Color(red: 0.16, green: 0.17, blue: 0.20), Color(red: 0.115, green: 0.12, blue: 0.135)]
            : [Color(red: 0.918, green: 0.941, blue: 0.965), Color(red: 0.968, green: 0.949, blue: 0.933)]
    }
    /// 見出し・本文の色（白磁の上で読みやすい濃さ）。
    static func ink(_ s: ColorScheme) -> Color {
        s == .dark ? Color(white: 0.95) : Color(white: 0.10)
    }
    /// アクティブなアクセント（レベルメーター・点灯キー）。青系（システムアクセント）。
    static let signal = Color.accentColor
}

// MARK: - 黒ピル CTA / ゴーストボタン（この画面専用スタイル）

/// 白磁ペインの上で強いコントラストを作る黒ピル primary CTA（Typeless 参照）。
/// アプリのアクセント（青）ではなく黒（ライト）／白（ダーク）。
private struct BlackPillButtonStyle: ButtonStyle {
    @Environment(\.colorScheme) private var scheme
    @Environment(\.isEnabled) private var isEnabled

    func makeBody(configuration: Configuration) -> some View {
        let bg = scheme == .dark ? Color.white : Color.black
        let fg = scheme == .dark ? Color.black : Color.white
        return configuration.label
            .font(.system(size: 14, weight: .semibold))
            .padding(.horizontal, 22)
            .padding(.vertical, 11)
            .foregroundStyle(fg)
            .background(Capsule().fill(bg))
            .overlay(Capsule().strokeBorder(Color.white.opacity(scheme == .dark ? 0 : 0.12), lineWidth: 0.5))
            .shadow(color: .black.opacity(0.14), radius: 5, x: 0, y: 2)
            .opacity(configuration.isPressed ? 0.82 : (isEnabled ? 1 : 0.4))
            .scaleEffect(configuration.isPressed ? 0.98 : 1)
            .animation(.easeOut(duration: 0.1), value: configuration.isPressed)
    }
}

/// 副アクション（「マイクを変更」「あとで」等）のゴーストボタン（枠なし・控えめ）。
private struct GhostButtonStyle: ButtonStyle {
    @Environment(\.isEnabled) private var isEnabled
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: 13, weight: .medium))
            .padding(.horizontal, 12)
            .padding(.vertical, 9)
            .foregroundStyle(.secondary)
            .opacity(configuration.isPressed ? 0.6 : (isEnabled ? 1 : 0.4))
    }
}

extension View {
    fileprivate func blackPill() -> some View { buttonStyle(BlackPillButtonStyle()) }
    fileprivate func ghost() -> some View { buttonStyle(GhostButtonStyle()) }
}

// MARK: - オンボーディング画面（フルウィンドウ・2 ペイン）

/// 初回セットアップの案内画面。ブレッドクラム＋（インタースティシャル or 2 ペイン）の構成。
struct OnboardingView: View {

    @ObservedObject var model: OnboardingModel
    @ObservedObject private var login = LoginCoordinator.shared
    @Environment(\.colorScheme) private var scheme

    private let phases = ["ようこそ", "準備", "体験"]

    var body: some View {
        VStack(spacing: 0) {
            breadcrumb
            content
        }
        .frame(width: 900, height: 600)
        .background(OB.porcelain(scheme))
        .onAppear { model.startPolling() }
        .onDisappear { model.stopPolling() }
    }

    // MARK: ブレッドクラム＋進捗

    private var breadcrumb: some View {
        VStack(spacing: 9) {
            HStack(spacing: 10) {
                ForEach(Array(phases.enumerated()), id: \.offset) { i, name in
                    if i > 0 {
                        Image(systemName: "chevron.right")
                            .font(.system(size: 9, weight: .semibold))
                            .foregroundStyle(.tertiary)
                    }
                    Text(name)
                        .font(.system(size: 12, weight: model.step.phase == i ? .semibold : .regular))
                        .foregroundStyle(model.step.phase == i ? OB.ink(scheme)
                                         : (model.step.phase > i ? Color.secondary : Color.secondary.opacity(0.5)))
                        .overlay(alignment: .bottom) {
                            if model.step.phase == i {
                                Capsule().fill(OB.ink(scheme)).frame(height: 1.5).offset(y: 5)
                            }
                        }
                }
            }
            .padding(.top, 15)
            // 全体進捗の下線
            GeometryReader { geo in
                ZStack(alignment: .leading) {
                    Rectangle().fill(Color.primary.opacity(0.06))
                    Rectangle().fill(OB.ink(scheme).opacity(0.5))
                        .frame(width: geo.size.width * progress)
                        .animation(.easeInOut(duration: 0.25), value: model.step)
                }
            }
            .frame(height: 2)
        }
        .background(OB.porcelain(scheme))
    }

    private var progress: CGFloat {
        let total = CGFloat(OnboardingStep.allCases.count - 1)
        return total > 0 ? CGFloat(model.step.rawValue) / total : 0
    }

    // MARK: コンテンツ振り分け

    @ViewBuilder private var content: some View {
        if model.step.isInterstitial {
            interstitial
        } else if model.step.isPractice {
            practicePane
        } else {
            twoPane
        }
    }

    // MARK: - 2 ペイン（権限・ログイン・マイクテスト・ホットキーテスト）

    private var twoPane: some View {
        HStack(spacing: 0) {
            leftPane
            washPane { rightContent }.frame(width: 360)
        }
    }

    private var leftPane: some View {
        VStack(alignment: .leading, spacing: 0) {
            Button { model.goBack() } label: {
                Label("戻る", systemImage: "chevron.left").font(.system(size: 12, weight: .medium))
            }
            .ghost()
            Spacer(minLength: 8)
            VStack(alignment: .leading, spacing: 14) { leftContent }
            Spacer()
            footer
        }
        .padding(.horizontal, 44)
        .padding(.vertical, 26)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .background(OB.porcelain(scheme))
    }

    @ViewBuilder private var leftContent: some View {
        switch model.step {
        case .microphone:
            heading("マイクを許可")
            bodyText("あなたの声を文字にするために、マイクを使います。録音は録音キーを押している間だけ。話し終わってキーを離すと、すぐに止まります。")
            if model.micGranted { grantedBadge("マイクを許可しました") }
        case .accessibility:
            heading("文字の貼り付けを許可")
            bodyText("文字起こしの結果を、いま開いているアプリのカーソル位置へ貼り付けるために使います（アクセシビリティ）。「許可する」を押し、システム設定で VoiceKey をオンにしてください。")
            if model.axTrusted { grantedBadge("許可しました") }
        case .inputMonitoring:
            heading("録音キーの検知を許可")
            bodyText("録音キーの押し下げを見分けるために使います（入力監視）。押したキーの内容は記録しません。「許可する」を押し、システム設定で VoiceKey をオンにしてください。")
            if model.inputGranted && model.tapCreatable { grantedBadge("許可しました") }
            if model.needsRestart { restartNotice }
        case .login:
            heading("ログイン")
            bodyText("ログインすると、無料体験で文字起こしを試せます。ログインはブラウザで行います。あとで設定画面からでも大丈夫です。")
            loginStatusRow
            if !model.isLoggedIn {
                Button("ブラウザでログイン") { login.beginLogin() }.blackPill()
            }
        case .micTest:
            heading("マイクをテスト")
            bodyText("録音キーはまだ押さなくて大丈夫。ためしに少しだけ声を出してみてください。右のバーが動けば、マイクはちゃんと聞こえています。")
            micTestStatusRow
            micDeviceRow
        case .hotkeyTest:
            heading("録音キーをためす")
            bodyText("下の録音キー \(model.config.slot1.hotkeyLabel) を、ちょっと押してみてください。右のキーが青く光れば、しっかり効いています。ここでは文字は入りません（練習です）。")
            hotkeyTestStatusRow
        default:
            EmptyView()
        }
    }

    @ViewBuilder private var rightContent: some View {
        switch model.step {
        case .microphone:
            permissionMockPane(hero: "mic.fill", cards: [
                ("mic.fill", "録音は押している間だけ", "録音キーを離すと、その瞬間に録音は止まります。"),
                ("hand.raised.fill", "文字にするためだけに", "声のデータは文字起こしにだけ使います。"),
                ("internaldrive", "履歴はこの Mac の中に", "入力した文章の履歴は、お使いの Mac にだけ残ります。"),
            ])
        case .accessibility:
            permissionMockPane(hero: "cursorarrow.rays", cards: [
                ("cursorarrow.rays", "貼り付けにだけ使います", "文字起こしの結果を、カーソル位置に入れるためだけに使います。"),
                ("eye.slash", "画面は読み取りません", "表示中の内容を読み取ったり、どこかへ送ったりしません。"),
            ])
        case .inputMonitoring:
            permissionMockPane(hero: "keyboard", cards: [
                ("keyboard", "見るのは録音キーだけ", "押したキーの内容は記録しません。録音キーの押し下げだけを見ます。"),
                ("bolt.shield", "軽くて速い", "余計な常駐処理はせず、キー入力の反応を邪魔しません。"),
            ])
        case .login:
            loginValuePane
        case .micTest:
            washCenter { MicMeterView(level: model.micLevel, active: model.micSignalSeen) }
        case .hotkeyTest:
            washCenter { GiantKeyView(label: model.config.slot1.hotkeyLabel, lit: model.hotkeyHeldSlot != nil) }
        default:
            EmptyView()
        }
    }

    // MARK: 左ペイン フッター（副＝ゴースト / 主＝黒ピル）

    private var footer: some View {
        HStack(spacing: 12) {
            footerSecondary
            Spacer(minLength: 0)
            footerPrimary
        }
        .padding(.top, 16)
    }

    @ViewBuilder private var footerSecondary: some View {
        if model.step == .micTest || model.step == .hotkeyTest {
            Button("スキップ") { model.skipToSummary() }.ghost()
        }
    }

    @ViewBuilder private var footerPrimary: some View {
        switch model.step {
        case .microphone:
            permissionPrimary(granted: model.micGranted, denied: model.micDenied,
                              pane: "Privacy_Microphone", request: { model.requestMic() })
        case .accessibility:
            permissionPrimary(granted: model.axTrusted, denied: false,
                              pane: "Privacy_Accessibility", request: { model.requestAccessibility() }, showOpen: true)
        case .inputMonitoring:
            permissionPrimary(granted: model.inputGranted && model.tapCreatable, denied: false,
                              pane: "Privacy_ListenEvent", request: { model.requestInputMonitoring() }, showOpen: true)
        case .login:
            if model.isLoggedIn {
                Button("次へ") { model.goNext() }.blackPill().keyboardShortcut(.defaultAction)
            } else {
                Button("あとで") { model.goNext() }.blackPill().keyboardShortcut(.defaultAction)
            }
        case .micTest, .hotkeyTest:
            Button("はい、続ける") { model.goNext() }.blackPill().keyboardShortcut(.defaultAction)
        default:
            EmptyView()
        }
    }

    /// 権限ステップの主ボタン群（許可済み→次へ / 拒否済み→設定を開く / 未許可→許可する）。
    @ViewBuilder private func permissionPrimary(
        granted: Bool, denied: Bool, pane: String, request: @escaping () -> Void, showOpen: Bool = false
    ) -> some View {
        if granted {
            Button("次へ") { model.goNext() }.blackPill().keyboardShortcut(.defaultAction)
        } else if denied {
            Button("システム設定を開く") { model.openSettings(pane: pane) }.blackPill()
        } else {
            HStack(spacing: 10) {
                if showOpen {
                    Button("システム設定を開く") { model.openSettings(pane: pane) }.ghost()
                }
                Button("許可する") { request() }.blackPill()
            }
        }
    }

    // MARK: 左ペイン 部品

    private func heading(_ t: String) -> some View {
        Text(t).font(.system(size: 26, weight: .bold)).foregroundStyle(OB.ink(scheme))
            .fixedSize(horizontal: false, vertical: true)
    }

    private func bodyText(_ t: String) -> some View {
        Text(t).font(.system(size: 14)).foregroundStyle(.secondary)
            .lineSpacing(2).fixedSize(horizontal: false, vertical: true)
    }

    private func grantedBadge(_ t: String) -> some View {
        Label(t, systemImage: "checkmark.circle.fill")
            .font(.system(size: 13, weight: .medium)).foregroundStyle(.green)
    }

    private var restartNotice: some View {
        VStack(alignment: .leading, spacing: 8) {
            Label("反映にはアプリの再起動が必要です", systemImage: "arrow.clockwise.circle")
                .font(.system(size: 13)).foregroundStyle(.orange)
            Button("アプリを再起動") { model.onRestart() }.blackPill()
        }
    }

    @ViewBuilder private var loginStatusRow: some View {
        switch login.status {
        case .loggedIn:
            Label(login.accountEmail.map { "ログイン済み（\($0)）" } ?? "ログイン済み", systemImage: "checkmark.circle.fill")
                .font(.system(size: 13, weight: .medium)).foregroundStyle(.green)
        case .waiting:
            Label("ブラウザでログインを完了してください…", systemImage: "safari")
                .font(.system(size: 13)).foregroundStyle(.secondary)
        case .exchanging:
            Label("ログイン処理中…", systemImage: "arrow.triangle.2.circlepath")
                .font(.system(size: 13)).foregroundStyle(.secondary)
        case .failed(let msg):
            Label(msg, systemImage: "exclamationmark.triangle.fill")
                .font(.system(size: 13)).foregroundStyle(.orange)
        case .idle:
            EmptyView()
        }
    }

    private var micTestStatusRow: some View {
        Group {
            if model.micSignalSeen {
                Label("聞こえています！ マイクはバッチリです", systemImage: "checkmark.circle.fill").foregroundStyle(.green)
            } else {
                Label("話している間に、右のバーは動いていますか？", systemImage: "waveform").foregroundStyle(.secondary)
            }
        }
        .font(.system(size: 13, weight: .medium))
    }

    private var micDeviceRow: some View {
        VStack(alignment: .leading, spacing: 8) {
            if model.showDevicePicker {
                HStack(spacing: 8) {
                    Picker("", selection: Binding(
                        get: { model.selectedDeviceUID },
                        set: { model.selectDevice($0) }
                    )) {
                        Text("システム既定").tag("")
                        ForEach(model.inputDevices) { d in Text(d.name).tag(d.uid) }
                        if !model.selectedDeviceUID.isEmpty,
                           !model.inputDevices.contains(where: { $0.uid == model.selectedDeviceUID }) {
                            Text("（未接続のデバイス）").tag(model.selectedDeviceUID)
                        }
                    }
                    .labelsHidden()
                    .frame(maxWidth: 210)
                    Button { model.inputDevices = AudioDevices.inputDevices() } label: {
                        Image(systemName: "arrow.clockwise")
                    }
                    .ghost()
                    .help("デバイス一覧を更新")
                }
            } else {
                Button("うまく動かない？ マイクを変更する") { model.showDevicePicker = true }.ghost()
            }
        }
    }

    private var hotkeyTestStatusRow: some View {
        Group {
            if model.hotkeySignalSeen {
                Label("効いています！ 録音キーはバッチリです", systemImage: "checkmark.circle.fill").foregroundStyle(.green)
            } else {
                Label("押している間、右のキーは光っていますか？", systemImage: "command").foregroundStyle(.secondary)
            }
        }
        .font(.system(size: 13, weight: .medium))
    }

    // MARK: 右ペイン（ウォッシュ）部品

    private func washPane<C: View>(@ViewBuilder _ c: () -> C) -> some View {
        ZStack {
            LinearGradient(colors: OB.wash(scheme), startPoint: .topLeading, endPoint: .bottomTrailing)
            c()
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .overlay(alignment: .leading) {
            Rectangle().fill(Color.primary.opacity(0.06)).frame(width: 1)
        }
    }

    private func washCenter<C: View>(@ViewBuilder _ c: () -> C) -> some View {
        c().frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    /// 権限ステップの右ペイン（ヒーローグリフ＋信頼カード）。
    private func permissionMockPane(hero: String, cards: [(String, String, String)]) -> some View {
        VStack(alignment: .leading, spacing: 14) {
            Image(systemName: hero)
                .font(.system(size: 38, weight: .regular))
                .foregroundStyle(OB.signal)
                .frame(width: 88, height: 88)
                .glassSurface(cornerRadius: 24)
                .frame(maxWidth: .infinity, alignment: .center)
            ForEach(Array(cards.enumerated()), id: \.offset) { _, c in
                TrustCard(icon: c.0, title: c.1, desc: c.2)
            }
            Spacer(minLength: 0)
        }
        .padding(26)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
    }

    /// ログインの右ペイン（無料体験の価値提案）。
    private var loginValuePane: some View {
        VStack(spacing: 16) {
            Image(nsImage: NSApp.applicationIconImage ?? NSImage())
                .resizable().frame(width: 60, height: 60)
                .clipShape(RoundedRectangle(cornerRadius: 14))
            Text("無料で 200 回、ためせます").font(.system(size: 15, weight: .semibold))
            VStack(alignment: .leading, spacing: 12) {
                valueRow("bolt.fill", "しゃべり終わった瞬間、全文がまとめて入力")
                valueRow("sparkles", "話し言葉を、きれいな文章に整えられます")
                valueRow("macbook.and.iphone", "ログインすれば、別の Mac でも使えます")
            }
            .padding(16)
            .glassSurface(cornerRadius: 14)
        }
        .padding(28)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .center)
    }

    private func valueRow(_ icon: String, _ text: String) -> some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: icon).font(.system(size: 13, weight: .semibold)).foregroundStyle(OB.signal).frame(width: 20)
            Text(text).font(.system(size: 12)).fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 0)
        }
    }

    // MARK: - 練習ペイン（体験 3 種・2 ペイン＋擬似アプリ窓）

    @ViewBuilder private var practicePane: some View {
        switch model.step {
        case .practiceBasic:
            PracticeStepView(
                model: model,
                hotkeyLabel: model.config.slot1.hotkeyLabel,
                title: "さっそく入力してみる",
                instruction: "録音キーを押しながら、上の文を声に出して読んでみてください。キーを離すと、右のメモにその場で文字が入ります。",
                example: "明日の打ち合わせ、15時に変更でお願いします。",
                hint: nil,
                successText: "よくできました！ 声がそのまま文字になりました。",
                appName: "メモ", appIcon: "note.text", appTint: Color(red: 0.98, green: 0.78, blue: 0.30),
                done: Binding(get: { model.basicPracticeDone }, set: { model.basicPracticeDone = $0 }),
                onNext: { model.goNext() }, onSkip: { model.skipToSummary() }
            )
        case .practiceHandsfree:
            PracticeStepView(
                model: model,
                hotkeyLabel: model.handsfreeSlot.hotkeyLabel,
                title: "手を止めずに、長く話す",
                instruction: "\(model.handsfreeSlot.hotkeyLabel) を一度押して話し、話し終えたらもう一度押して確定します。押しっぱなしにしなくても、長い文章を話せます。",
                example: "今日の議事録です。まず先週の宿題を確認して、そのあと新しい企画の話に移ります。",
                hint: "長い議事録やメールのときに便利です。",
                successText: "できました！ 押しっぱなしにしなくても入力できます。",
                appName: "メール", appIcon: "envelope.fill", appTint: Color(red: 0.32, green: 0.56, blue: 0.90),
                done: Binding(get: { model.handsfreePracticeDone }, set: { model.handsfreePracticeDone = $0 }),
                onNext: { model.goNext() }, onSkip: { model.skipToSummary() }
            )
        case .practiceFormat:
            PracticeStepView(
                model: model,
                hotkeyLabel: model.config.slot1.hotkeyLabel,
                title: "話し言葉を、きれいに",
                instruction: "「文章を自動で整える」をオンにして試します。「えーっと」などの言いよどみは、そのまま声に出してみてください。整えたあとの文が入ります。",
                example: "えーっと、明日の会議なんですけど、あー、10時から、やっぱり11時からでお願いします。",
                hint: "整え方は「標準／そのまま／すっきり／箇条書き」の 4 つから選べます（設定でいつでも変更できます）。",
                successText: "言いよどみや言い直しが、自動で消えました。",
                appName: "チャット", appIcon: "bubble.left.and.bubble.right.fill", appTint: Color(red: 0.40, green: 0.78, blue: 0.52),
                done: Binding(get: { model.formatPracticeDone }, set: { model.formatPracticeDone = $0 }),
                onNext: { model.goNext() }, onSkip: { model.skipToSummary() }
            )
        default:
            EmptyView()
        }
    }

    // MARK: - インタースティシャル（ようこそ / 動作確認 / まとめ）

    @ViewBuilder private var interstitial: some View {
        switch model.step {
        case .welcome: welcomeInterstitial
        case .checkIntro: checkIntroInterstitial
        case .summary: summaryInterstitial
        default: EmptyView()
        }
    }

    private func interstitialShell<C: View>(back: Bool, @ViewBuilder _ c: () -> C) -> some View {
        ZStack {
            LinearGradient(colors: OB.wash(scheme), startPoint: .top, endPoint: .bottom)
            c().padding(40)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .overlay(alignment: .topLeading) {
            if back {
                Button { model.goBack() } label: {
                    Label("戻る", systemImage: "chevron.left").font(.system(size: 12, weight: .medium))
                }
                .ghost()
                .padding(20)
            }
        }
    }

    private var welcomeInterstitial: some View {
        interstitialShell(back: false) {
            VStack(spacing: 18) {
                Image(systemName: "waveform")
                    .font(.system(size: 44, weight: .regular)).foregroundStyle(OB.signal)
                    .frame(width: 96, height: 96).glassSurface(cornerRadius: 26)
                Text("話すだけで、文字になる。")
                    .font(.system(size: 34, weight: .bold)).foregroundStyle(OB.ink(scheme))
                Text("録音キーを押して話すだけ。離すと、いま開いているアプリのカーソル位置に文字が入ります。\nまずは数十秒だけ、準備にお付き合いください。")
                    .font(.system(size: 15)).foregroundStyle(.secondary)
                    .multilineTextAlignment(.center).fixedSize(horizontal: false, vertical: true)
                Button("はじめる") { model.goNext() }.blackPill().keyboardShortcut(.defaultAction)
                    .padding(.top, 4)
            }
            .frame(maxWidth: 480)
        }
    }

    private var checkIntroInterstitial: some View {
        interstitialShell(back: true) {
            VStack(spacing: 18) {
                Image(systemName: "checkmark.seal")
                    .font(.system(size: 40, weight: .regular)).foregroundStyle(OB.signal)
                    .frame(width: 92, height: 92).glassSurface(cornerRadius: 24)
                Text("準備ができました。動作を確認しましょう。")
                    .font(.system(size: 28, weight: .bold)).foregroundStyle(OB.ink(scheme))
                    .multilineTextAlignment(.center).fixedSize(horizontal: false, vertical: true)
                Text("マイクと録音キーがちゃんと動くか、かんたんにチェックします。すぐ終わります。")
                    .font(.system(size: 15)).foregroundStyle(.secondary)
                    .multilineTextAlignment(.center).fixedSize(horizontal: false, vertical: true)
                HStack(spacing: 12) {
                    Button("スキップ") { model.skipToSummary() }.ghost()
                    Button("確認する") { model.goNext() }.blackPill().keyboardShortcut(.defaultAction)
                }
                .padding(.top, 4)
            }
            .frame(maxWidth: 480)
        }
    }

    private var summaryInterstitial: some View {
        interstitialShell(back: true) {
            VStack(spacing: 20) {
                Text("話して、タイプしないで。")
                    .font(.system(size: 32, weight: .bold)).foregroundStyle(OB.ink(scheme))
                Text("これだけ覚えれば、もう準備完了です。")
                    .font(.system(size: 15)).foregroundStyle(.secondary)
                HStack(spacing: 14) {
                    SummaryHotkeyCard(title: "録音キー 1（メイン）", slot: model.config.slot1)
                    SummaryHotkeyCard(title: "録音キー 2（サブ）", slot: model.config.slot2)
                }
                Text("設定はいつでも、メニューバーのアイコンから変えられます。")
                    .font(.footnote).foregroundStyle(.secondary)
                Button("VoiceKey をはじめる") { model.onFinish() }.blackPill().keyboardShortcut(.defaultAction)
                    .padding(.top, 4)
            }
            .frame(maxWidth: 560)
        }
    }
}

// MARK: - 信頼カード（権限ステップの右ペイン）

private struct TrustCard: View {
    let icon: String
    let title: String
    let desc: String
    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: icon)
                .font(.system(size: 15, weight: .semibold)).foregroundStyle(OB.signal).frame(width: 22)
            VStack(alignment: .leading, spacing: 2) {
                Text(title).font(.system(size: 13, weight: .semibold))
                Text(desc).font(.system(size: 11)).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 0)
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .glassSurface(cornerRadius: 12)
    }
}

// MARK: - ホットキーチップ（キーキャップ表記）

private struct HotkeyChip: View {
    let label: String
    var body: some View {
        Text(label)
            .font(.system(size: 12, weight: .semibold, design: .rounded))
            .padding(.horizontal, 10).padding(.vertical, 5)
            .background(RoundedRectangle(cornerRadius: 7).fill(Color.primary.opacity(0.07)))
            .overlay(RoundedRectangle(cornerRadius: 7).strokeBorder(Color.primary.opacity(0.12), lineWidth: 0.5))
    }
}

// MARK: - マイクレベルメーター（マイクテスト右ペイン）

/// 録音せずモニタしたレベル（0–1）を縦バーで可視化する。話すとバーが増える。
private struct MicMeterView: View {
    let level: Float
    let active: Bool
    private let bars = 13

    var body: some View {
        VStack(spacing: 14) {
            HStack(alignment: .center, spacing: 6) {
                ForEach(0..<bars, id: \.self) { i in
                    let threshold = Float(i) / Float(bars)
                    let on = level >= threshold + 0.02
                    Capsule()
                        .fill(on ? OB.signal : Color.primary.opacity(0.12))
                        .frame(width: 8, height: barHeight(i))
                        .animation(.easeOut(duration: 0.08), value: level)
                }
            }
            .frame(height: 120)
            .padding(.horizontal, 24).padding(.vertical, 20)
            .glassSurface(cornerRadius: 22)

            Label(active ? "いい感じ！" : "マイクに向かって話してみてください",
                  systemImage: active ? "checkmark.circle.fill" : "mic.fill")
                .font(.system(size: 12, weight: .medium))
                .foregroundStyle(active ? .green : .secondary)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .center)
    }

    /// 中央が高い山型のバー高さ（見た目の遊び）。
    private func barHeight(_ i: Int) -> CGFloat {
        let mid = Double(bars - 1) / 2
        let d = abs(Double(i) - mid) / mid
        return CGFloat(36 + (1 - d) * 70)
    }
}

// MARK: - 巨大キー（ホットキーテスト右ペイン）

/// ホットキーを押している間、点灯する巨大なキーキャップ。
private struct GiantKeyView: View {
    let label: String
    let lit: Bool
    @Environment(\.colorScheme) private var scheme

    var body: some View {
        Text(label)
            .font(.system(size: 24, weight: .semibold, design: .rounded))
            .foregroundStyle(lit ? Color.white : OB.ink(scheme))
            .padding(.horizontal, 30).padding(.vertical, 24)
            .frame(minWidth: 150)
            .background(
                RoundedRectangle(cornerRadius: 18)
                    .fill(lit ? OB.signal : (scheme == .dark ? Color.white.opacity(0.10) : Color.white.opacity(0.85)))
            )
            .overlay(
                RoundedRectangle(cornerRadius: 18).strokeBorder(
                    LinearGradient(colors: [.white.opacity(0.5), .white.opacity(0.05)], startPoint: .top, endPoint: .bottom),
                    lineWidth: 1)
            )
            .shadow(color: lit ? OB.signal.opacity(0.45) : .black.opacity(0.18),
                    radius: lit ? 16 : 8, x: 0, y: 5)
            .scaleEffect(lit ? 0.97 : 1)
            .animation(.spring(response: 0.28, dampingFraction: 0.6), value: lit)
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .center)
    }
}

// MARK: - 擬似アプリ窓（練習の右ペイン）

/// メモ／メール／チャット風の小さなアプリ窓。中身に練習用の入力欄を差し込む。
private struct MockAppWindow<Content: View>: View {
    let appName: String
    let systemIcon: String
    let tint: Color
    @ViewBuilder var content: Content
    @Environment(\.colorScheme) private var scheme

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 6) {
                Circle().fill(Color(red: 0.98, green: 0.42, blue: 0.42)).frame(width: 9, height: 9)
                Circle().fill(Color(red: 0.98, green: 0.74, blue: 0.32)).frame(width: 9, height: 9)
                Circle().fill(Color(red: 0.42, green: 0.80, blue: 0.42)).frame(width: 9, height: 9)
                Spacer(minLength: 6)
                Image(systemName: systemIcon).font(.system(size: 11, weight: .semibold)).foregroundStyle(tint)
                Text(appName).font(.system(size: 11, weight: .semibold)).foregroundStyle(.secondary)
                Spacer(minLength: 6)
                Color.clear.frame(width: 42)
            }
            .padding(.horizontal, 12).padding(.vertical, 9)
            .background(Color.primary.opacity(0.04))
            Divider()
            content.padding(12)
        }
        .background(RoundedRectangle(cornerRadius: 12).fill(scheme == .dark ? Color(white: 0.16) : Color.white))
        .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(Color.primary.opacity(0.10), lineWidth: 0.5))
        .shadow(color: .black.opacity(0.14), radius: 16, x: 0, y: 8)
    }
}

// MARK: - まとめのホットキーカード

private struct SummaryHotkeyCard: View {
    let title: String
    let slot: SlotConfig
    var body: some View {
        VStack(spacing: 8) {
            Text(title).font(.system(size: 12, weight: .medium)).foregroundStyle(.secondary)
            HotkeyChip(label: slot.hotkeyLabel)
            Text("\(slot.backend.label)・\(slot.mode.label)")
                .font(.system(size: 11)).foregroundStyle(.secondary)
        }
        .padding(16)
        .frame(minWidth: 160)
        .glassSurface(cornerRadius: 14)
    }
}

// MARK: - 練習ステップ（2 ペイン・擬似アプリ窓へ実入力）

/// 例文カード＋促し文（左）と、自動フォーカスの入力欄を持つ擬似アプリ窓（右）を 1 枚にまとめた体験ステップ。
///
/// 録音キーを押して話すと、本体の貼り付け（Paster）が前面ウィンドウ＝この入力欄へテキストを入れる。
/// テキストが入った瞬間を onChange で捕まえ、スプリング演出とともに褒めメッセージを出す。
/// 手入力でも成功扱いにする（体験を妨げない）。
private struct PracticeStepView: View {
    @ObservedObject var model: OnboardingModel
    let hotkeyLabel: String
    let title: String
    let instruction: String
    let example: String
    let hint: String?
    let successText: String
    let appName: String
    let appIcon: String
    let appTint: Color
    @Binding var done: Bool
    let onNext: () -> Void
    let onSkip: () -> Void

    @State private var typed = ""
    @FocusState private var focused: Bool
    @Environment(\.colorScheme) private var scheme

    var body: some View {
        HStack(spacing: 0) {
            leftColumn
            rightColumn
        }
        .onChange(of: typed) { _, newValue in
            if !done, OnboardingPractice.hasInput(newValue) {
                withAnimation(.spring(response: 0.45, dampingFraction: 0.6)) { done = true }
            }
        }
        .onAppear { DispatchQueue.main.async { focused = true } }
    }

    private var leftColumn: some View {
        VStack(alignment: .leading, spacing: 0) {
            Button { model.goBack() } label: {
                Label("戻る", systemImage: "chevron.left").font(.system(size: 12, weight: .medium))
            }
            .ghost()
            Spacer(minLength: 8)
            VStack(alignment: .leading, spacing: 14) {
                HStack(spacing: 10) {
                    Text(title).font(.system(size: 26, weight: .bold)).foregroundStyle(OB.ink(scheme))
                    HotkeyChip(label: hotkeyLabel)
                }
                Text(instruction).font(.system(size: 14)).foregroundStyle(.secondary)
                    .lineSpacing(2).fixedSize(horizontal: false, vertical: true)
                HStack(alignment: .top, spacing: 8) {
                    Image(systemName: "text.quote").foregroundStyle(OB.signal)
                    Text(example).font(.system(size: 16, weight: .medium)).italic()
                        .fixedSize(horizontal: false, vertical: true)
                }
                .padding(12).frame(maxWidth: .infinity, alignment: .leading)
                .glassSurface(cornerRadius: 12)
                if let hint {
                    Text(hint).font(.system(size: 12)).foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if done {
                    Label(successText, systemImage: "checkmark.circle.fill")
                        .font(.system(size: 13, weight: .semibold)).foregroundStyle(.green)
                        .fixedSize(horizontal: false, vertical: true)
                        .transition(.scale(scale: 0.7).combined(with: .opacity))
                }
            }
            Spacer()
            HStack(spacing: 12) {
                Button("スキップ", action: onSkip).ghost()
                Spacer()
                Button("次へ", action: onNext).blackPill().keyboardShortcut(.defaultAction)
            }
            .padding(.top, 16)
        }
        .padding(.horizontal, 44).padding(.vertical, 26)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .background(OB.porcelain(scheme))
    }

    private var rightColumn: some View {
        ZStack {
            LinearGradient(colors: OB.wash(scheme), startPoint: .topLeading, endPoint: .bottomTrailing)
            MockAppWindow(appName: appName, systemIcon: appIcon, tint: appTint) {
                ZStack(alignment: .topLeading) {
                    if typed.isEmpty {
                        Text("\(hotkeyLabel) を押しながら、声で入力…")
                            .font(.system(size: 13)).foregroundStyle(.tertiary)
                            .padding(.top, 4).padding(.leading, 5)
                    }
                    TextEditor(text: $typed)
                        .font(.system(size: 14))
                        .scrollContentBackground(.hidden)
                        .frame(height: 150)
                        .focused($focused)
                }
            }
            .frame(width: 300)
        }
        .frame(width: 360)
        .overlay(alignment: .leading) {
            Rectangle().fill(Color.primary.opacity(0.06)).frame(width: 1)
        }
    }
}
