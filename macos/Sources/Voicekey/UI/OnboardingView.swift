//
//  OnboardingView.swift
//  初回起動オンボーディング（Phase 5）
//
//  初回起動でアプリがバックグラウンドに黙って常駐すると「何これ？」となるため、
//  起動時にセットアップウィンドウを自動表示し、権限（マイク／アクセシビリティ／
//  入力監視）を順に案内・取得してからアプリ本体を開始する。
//  文言は新語彙（録音キー／文字起こしモード／即時入力／
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
///
/// 権限・ログインの後ろに「実際に使って気持ちよくなる」体験ステップ（練習）を足す。
/// 体験は強制ではなく、各ステップから「スキップ」で完了ステップへ飛べる。
enum OnboardingStep: Int, CaseIterable, Comparable {
    case welcome = 0        // ようこそ（概要）
    case microphone         // マイク権限
    case accessibility      // アクセシビリティ権限
    case inputMonitoring    // 入力監視権限
    case login              // ログイン（あとで可）
    case practiceBasic      // 体験1: 即時入力（例文を読んで入力してみる）
    case practiceHandsfree  // 体験2: ハンズフリー（トグル録音）
    case practiceFormat     // 体験3: 文章整形（フィラーが消える）
    case featureTour        // 機能ツアー（紹介のみ・カード）
    case done               // 完了（使い方）

    /// 体験（練習）ステップか。エンジン起動・スキップ導線の判定に使う。
    var isPractice: Bool {
        self == .practiceBasic || self == .practiceHandsfree || self == .practiceFormat
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
    /// 録音→文字起こし→貼り付けで練習欄のテキストが変化したら成功とみなす。
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

    // 体験（練習）ステップの成功状態（成功演出を一度だけ出す・進捗表示に使う）
    @Published var basicPracticeDone = false
    @Published var handsfreePracticeDone = false
    @Published var formatPracticeDone = false

    /// 完了設定の表示（完了ステップ）に使う現在設定
    let config: ConfigStore
    /// 「使い始める」/ ウィンドウを閉じたときに呼ぶ（完了扱い）
    let onFinish: () -> Void
    /// 入力監視の反映にプロセス再起動が必要なときに呼ぶ
    let onRestart: () -> Void
    /// 体験ステップに入ったら録音エンジン（ホットキー監視）を起動する（冪等・権限取得後の初回のみ実効）。
    /// これで「オンボーディング中に実際に録音キーを押して文字を入れる」体験が成立する。
    let startEngine: () -> Void
    /// 整形体験ステップの間だけ整形を強制 ON にする一時オーバーライドを切り替える（保存しない）。
    let setFormatOverride: (Bool) -> Void

    private var pollTimer: Timer?
    /// エンジン起動を一度だけにするフラグ
    private var engineStarted = false

    init(
        startStep: OnboardingStep,
        config: ConfigStore,
        onFinish: @escaping () -> Void,
        onRestart: @escaping () -> Void,
        startEngine: @escaping () -> Void = {},
        setFormatOverride: @escaping (Bool) -> Void = { _ in }
    ) {
        self.step = startStep
        self.config = config
        self.onFinish = onFinish
        self.onRestart = onRestart
        self.startEngine = startEngine
        self.setFormatOverride = setFormatOverride
        // 現在ステップの権限状況を即時に一度取り込む（再開時に✓を最初から出すため）
        refreshCurrentPermission()
        // 再開位置がいきなり体験ステップの場合にも配線を合わせる
        handleStepEntered()
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
            handleStepEntered()
        }
    }

    /// 前のステップへ戻る。
    func goBack() {
        if let prev = OnboardingStep(rawValue: step.rawValue - 1) {
            step = prev
            refreshCurrentPermission()
            handleStepEntered()
        }
    }

    /// 体験をスキップして完了ステップへ飛ぶ（体験を強制しない）。
    func skipToDone() {
        step = .done
        handleStepEntered()
    }

    /// 体験ステップの成功を記録する（練習欄にテキストが入ったとき View から呼ぶ）。
    func markPracticeSucceeded() {
        switch step {
        case .practiceBasic: basicPracticeDone = true
        case .practiceHandsfree: handsfreePracticeDone = true
        case .practiceFormat: formatPracticeDone = true
        default: break
        }
    }

    /// ステップに入った瞬間の副作用（エンジン起動・整形オーバーライドの切替）をまとめて行う。
    private func handleStepEntered() {
        // 体験ステップに入ったら録音エンジンを起動する（権限は前段で取得済み・冪等）。
        // これで練習欄に実際に音声入力できるようになる。
        if step.isPractice, !engineStarted {
            engineStarted = true
            startEngine()
        }
        // 整形体験ステップの間だけ整形を強制 ON（フィラー除去を体験させる）。それ以外では必ず解除。
        setFormatOverride(step == .practiceFormat)
    }
}

// MARK: - オンボーディング画面

/// 初回セットアップの案内画面（権限・ログイン → 体験 → 完了の 10 ステップ）。
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
        case .practiceBasic: practiceBasicStep
        case .practiceHandsfree: practiceHandsfreeStep
        case .practiceFormat: practiceFormatStep
        case .featureTour: featureTourStep
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

    // MARK: 体験ステップ（実際に録音キーを押して入力してみる）

    /// ログイン必須の案内（未ログイン時のみ体験ステップに出す）。文字起こしはサーバー経由のため。
    @ViewBuilder
    private var loginNoticeIfNeeded: some View {
        if !isLoggedIn {
            VStack(alignment: .leading, spacing: 8) {
                Label("体験するにはログインが必要です（文字起こしはサーバー経由）", systemImage: "exclamationmark.circle")
                    .font(.callout)
                    .foregroundStyle(.orange)
                Button("ブラウザでログイン") { login.beginLogin() }
                    .glassProminentButton()
            }
            .padding(12)
            .frame(maxWidth: .infinity, alignment: .leading)
            .glassSurface(cornerRadius: 10)
        }
    }

    /// トグル（ハンズフリー）録音に使う録音キー。既定はサブ(右⌥・toggle)。
    /// ユーザーがモードを入れ替えていても toggle のスロットを優先して案内する。
    private var handsfreeSlot: SlotConfig {
        if model.config.slot2.mode == .toggle { return model.config.slot2 }
        if model.config.slot1.mode == .toggle { return model.config.slot1 }
        return model.config.slot2
    }

    // 体験1: 即時入力（押しながら話す）
    private var practiceBasicStep: some View {
        VStack(alignment: .leading, spacing: 16) {
            stepHeader(icon: "waveform", title: "さっそく使ってみましょう")
            Text("録音キーを押しながら、下の文を声に出して読んでみてください。しゃべり終わってキーを離すと、その場に文字が入ります。")
                .font(.body)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            loginNoticeIfNeeded
            PracticeCard(
                exampleText: "明日の打ち合わせ、15時に変更でお願いします。",
                promptText: "⬇ \(model.config.slot1.hotkeyLabel) を押しながら、上の文を読んでみてください",
                successText: "よくできました！ 声がそのまま文字になりました",
                hint: nil,
                done: $model.basicPracticeDone
            )
        }
    }

    // 体験2: ハンズフリー（トグル録音）
    private var practiceHandsfreeStep: some View {
        VStack(alignment: .leading, spacing: 16) {
            stepHeader(icon: "hand.raised.fill", title: "ハンズフリーで話す")
            Text("キーを押して離すと録音が続きます。手を止めずに長い文章を話せます。もう一度押すと確定して入力されます。")
                .font(.body)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            loginNoticeIfNeeded
            PracticeCard(
                exampleText: "これはハンズフリーのテストです。手を離したまま、このまま何文か続けて話してみます。",
                promptText: "⬇ \(handsfreeSlot.hotkeyLabel) を一度押して話し、話し終えたらもう一度押して確定してください",
                successText: "できました！ 押しっぱなしにしなくても入力できます",
                hint: "長い議事録のときに便利です。",
                done: $model.handsfreePracticeDone
            )
        }
    }

    // 体験3: 文章整形（フィラーが自動で消える）
    private var practiceFormatStep: some View {
        VStack(alignment: .leading, spacing: 16) {
            stepHeader(icon: "sparkles", title: "話した言葉を、きれいな文章に")
            Text("「文章を自動で整える」をオンにして試します。「えーっと」などのフィラーや言い直しはそのまま声に出してみてください。")
                .font(.body)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            loginNoticeIfNeeded
            PracticeCard(
                exampleText: "えーっと、明日の会議なんですけど、あー、10時から、やっぱり11時からでお願いします",
                promptText: "⬇ \(model.config.slot1.hotkeyLabel) を押しながら、上の文をそのまま読んでみてください",
                successText: "フィラーや言い直しが自動で消えました",
                hint: "整え方は「標準／そのまま／すっきり／箇条書き」の 4 種から選べます（設定でいつでも変更できます）。",
                done: $model.formatPracticeDone
            )
        }
    }

    // 機能ツアー（紹介のみ・カード）
    private var featureTourStep: some View {
        VStack(alignment: .leading, spacing: 16) {
            stepHeader(icon: "star.fill", title: "できることの紹介")
            Text("よく使う機能です。あとで設定やメニューバーのアイコンから使えます。")
                .font(.body)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            VStack(spacing: 10) {
                tourCard(icon: "command", title: "2 つ目の録音キー",
                         body: "録音キー 2 には別のモードを割り当てられます（例: ハンズフリー）。")
                tourCard(icon: "clock.arrow.circlepath", title: "履歴",
                         body: "入力したテキストを最大 200 件まで自動保存。クリックでもう一度コピーできます。")
                tourCard(icon: "chart.bar.fill", title: "統計",
                         body: "どのアプリでどれだけ入力したか、節約できた時間が見られます。")
                tourCard(icon: "waveform", title: "録音中の表示（HUD）",
                         body: "画面下の小さなピルが録音中だけ現れ、状態がひと目で分かります。")
            }
        }
    }

    /// 機能ツアー 1 枚分の紹介カード（アイコン＋見出し＋説明）。
    private func tourCard(icon: String, title: String, body: String) -> some View {
        HStack(alignment: .top, spacing: 12) {
            Image(systemName: icon)
                .font(.title3)
                .foregroundStyle(.tint)
                .frame(width: 28)
            VStack(alignment: .leading, spacing: 3) {
                Text(title).font(.subheadline).bold()
                Text(body)
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer()
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .glassSurface(cornerRadius: 10)
    }

    // 完了（使い方カード）
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
            // 体験・機能ツアーは強制しない: いつでも完了ステップへ飛べる「スキップ」を常設
            if model.step.isPractice || model.step == .featureTour {
                Button("スキップ") { model.skipToDone() }
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

        case .practiceBasic, .practiceHandsfree, .practiceFormat, .featureTour:
            // 体験は任意なので「次へ」で常に進める（成功しなくても先に進める＝強制しない）
            Button("次へ") { model.goNext() }
                .glassProminentButton()
                .keyboardShortcut(.defaultAction)

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

    /// ステップのヒーローアイコン。「円の上に円＋アクセントグロー影」はダサい・ネオンだと却下されたため、
    /// 円形コンテナとグロー影を廃止。角丸スクエアの無色ガラスタイル（glassSurface）に
    /// アクセント色の素の SF シンボルを載せ、ガラスの文法だけで顔を作る。
    private func heroIcon(_ icon: String) -> some View {
        Image(systemName: icon)
            .font(.system(size: 24, weight: .semibold))
            .foregroundStyle(Color.accentColor)
            .frame(width: 52, height: 52)
            .glassSurface(cornerRadius: 14)
    }
}

// MARK: - 練習カード（体験ステップ共通）

/// 「例文カード」＋「促し文」＋「自動フォーカスの練習入力欄」＋「成功演出」を 1 枚にまとめた部品。
///
/// 録音キーを押して話すと、本体の貼り付け（Paster）が前面ウィンドウの
/// 練習入力欄（TextEditor）へテキストを入れる。テキストが入った瞬間を onChange で捕まえ、
/// スプリング演出とともに褒めメッセージを出す。手入力でも成功扱いにする（体験を妨げない）。
private struct PracticeCard: View {
    /// 読み上げてもらう例文
    let exampleText: String
    /// 「このキーを押しながら話して」の促し文（キー名は呼び出し側で実設定から埋める）
    let promptText: String
    /// 成功時に出す褒めメッセージ
    let successText: String
    /// 補足のヒント（ハンズフリー手順・整形プリセット紹介など。nil で非表示）
    let hint: String?
    /// 成功状態（OnboardingModel の該当フラグにバインドする）
    @Binding var done: Bool

    /// 練習入力欄の中身（ここへ本体の貼り付けが入る）
    @State private var typed: String = ""
    @FocusState private var focused: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            // 読む例文（引用符付きで「これを読む」と分かるように）
            HStack(alignment: .top, spacing: 8) {
                Image(systemName: "text.quote")
                    .foregroundStyle(.tint)
                Text(exampleText)
                    .font(.system(size: 16, weight: .medium))
                    .fixedSize(horizontal: false, vertical: true)
            }
            .padding(12)
            .frame(maxWidth: .infinity, alignment: .leading)
            .glassSurface(cornerRadius: 10)

            // 促し文
            Text(promptText)
                .font(.callout.weight(.semibold))
                .foregroundStyle(.primary)
                .fixedSize(horizontal: false, vertical: true)

            if let hint {
                Text(hint)
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }

            // 練習入力欄（自動フォーカス。前面＝このウィンドウなので貼り付けはここに入る）
            TextEditor(text: $typed)
                .font(.system(size: 15))
                .scrollContentBackground(.hidden)
                .frame(height: 84)
                .padding(8)
                .glassSurface(cornerRadius: 10)
                .focused($focused)
                .onChange(of: typed) { _, newValue in
                    // 空→非空になった瞬間だけ成功演出を出す（連続入力で何度も出さない）
                    if !done, OnboardingPractice.hasInput(newValue) {
                        withAnimation(.spring(response: 0.45, dampingFraction: 0.6)) {
                            done = true
                        }
                    }
                }
                .onAppear {
                    // 表示直後にフォーカスすると効かないことがあるため次のループで当てる
                    DispatchQueue.main.async { focused = true }
                }

            // 成功演出（チェックマークのスプリング＋褒めメッセージ）
            if done {
                HStack(spacing: 8) {
                    Image(systemName: "checkmark.circle.fill")
                        .font(.title3)
                        .foregroundStyle(.green)
                    Text(successText)
                        .font(.callout.weight(.semibold))
                        .foregroundStyle(.green)
                        .fixedSize(horizontal: false, vertical: true)
                }
                .padding(.top, 2)
                .transition(.scale(scale: 0.6).combined(with: .opacity))
            }
        }
    }
}
