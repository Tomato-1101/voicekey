//
//  VoicekeyApp.swift
//  アプリエントリポイント（メニューバー常駐）
//
//  SwiftUI MenuBarExtra は使わない:
//  - ラベルの NSImage が正しく描画されない（青い円になる）
//  - アイテム位置の永続化が壊れるとノッチ下に隠れて見えなくなる
//  という問題があったため、AppKit NSStatusItem を直接管理する。
//

import AppKit
import Combine
import ServiceManagement
import SwiftUI
import os.log

private let log = Logger(subsystem: "com.voicekey.app", category: "main")

/// オンボーディング状態の UserDefaults キー（初回起動セットアップ・Phase 5）。
private enum OnboardingKeys {
    /// オンボーディングを完了（またはスキップ）したか
    static let didComplete = "didCompleteOnboarding"
    /// 入力監視の再起動などで中断した位置（再起動後にそこから再開する）
    static let savedStep = "onboardingStep"
}

@main
@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {

    /// NSApplication.delegate は弱参照のため、自前で強参照を保持する
    private static var sharedDelegate: AppDelegate?

    static func main() {
        let app = NSApplication.shared
        let delegate = AppDelegate()
        sharedDelegate = delegate
        app.delegate = delegate
        app.run()
    }

    private var controller: AppController?
    private var statusBar: StatusItemController?
    /// App Nap 無効化トークン（プロセス生存中ずっと保持する）
    private var activityToken: NSObjectProtocol?

    func applicationWillFinishLaunching(_ notification: Notification) {
        // メニューバー常駐アプリとして Dock / Cmd+Tab から隠す
        // （バンドル時は Info.plist の LSUIElement でも指定するが、
        //  swift run での開発実行でも同じ挙動になるようここでも設定する）
        NSApp.setActivationPolicy(.accessory)

        // voicekey:// の deep link（ブラウザ経由ログインのコールバック）を受け取る。
        // アクセサリアプリでは application(_:open:) が呼ばれないことがあるため、
        // 確実な Apple Event（kInternetEventClass/kAEGetURL）で受ける。
        // 起動完了前に登録する必要がある（起動時に届く URL を取りこぼさないため）。
        NSAppleEventManager.shared().setEventHandler(
            self,
            andSelector: #selector(handleURLEvent(_:withReplyEvent:)),
            forEventClass: AEEventClass(kInternetEventClass),
            andEventID: AEEventID(kAEGetURL)
        )
    }

    /// 受信した voicekey:// URL をログイン司令塔へ渡す。
    @objc private func handleURLEvent(_ event: NSAppleEventDescriptor, withReplyEvent: NSAppleEventDescriptor) {
        guard let str = event.paramDescriptor(forKeyword: AEKeyword(keyDirectObject))?.stringValue,
              let url = URL(string: str) else { return }
        LoginCoordinator.shared.handleDeepLink(url)
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        // App Nap を無効化する。ウィンドウを 1 つも持たないメニューバーアプリは
        // ナップ対象になり、イベントタップのコールバックが遅延 → OS にタイムアウト
        // 無効化されてホットキーが効かなくなるため（システムスリープは妨げない）
        activityToken = ProcessInfo.processInfo.beginActivity(
            options: .userInitiatedAllowingIdleSystemSleep,
            reason: "グローバルホットキー監視"
        )

        let controller = AppController()
        self.controller = controller
        let statusBar = StatusItemController(controller: controller)
        self.statusBar = statusBar

        // Dock 常時表示が ON なら起動時から Dock にアイコンを出す（既定 OFF は従来どおり動的表示）
        if controller.config.dockIconAlwaysVisible {
            NSApp.setActivationPolicy(.regular)
        }

        // --- 初回起動オンボーディング（Phase 5）の分岐 ---
        // 未完了なら startup() を「呼ばず」にセットアップを表示し、完了/クローズ時に startup()
        // を呼ぶ（説明前にいきなりマイクダイアログが出る事故を防ぐ）。
        let defaults = UserDefaults.standard
        // 既存ユーザー判定に使う自動起動フラグは、登録で true になる「前」に読む
        let didComplete = defaults.bool(forKey: OnboardingKeys.didComplete)
        let didSetupLaunch = defaults.bool(forKey: "didSetupLaunchAtLogin")
        let savedStep = defaults.object(forKey: OnboardingKeys.savedStep) as? Int
        // デバッグ再表示: VOICEKEY_OPEN_ONBOARDING（env / defaults）があれば強制表示（読み取り後に削除）
        let forceOnboarding = (defaults.string(forKey: "VOICEKEY_OPEN_ONBOARDING")
            ?? ProcessInfo.processInfo.environment["VOICEKEY_OPEN_ONBOARDING"]) != nil
        if forceOnboarding {
            defaults.removeObject(forKey: "VOICEKEY_OPEN_ONBOARDING")
        }

        let action = OnboardingDecider.launchAction(
            didCompleteOnboarding: didComplete,
            didSetupLaunchAtLogin: didSetupLaunch,
            savedStep: savedStep
        )
        var startNow = true
        var onboardingStep: Int?
        switch action {
        case .skip:
            startNow = true
        case .autoComplete:
            // 既存ユーザー: 完了フラグを補完して以後は出さない（本体はそのまま起動）
            defaults.set(true, forKey: OnboardingKeys.didComplete)
            startNow = true
        case .show(let step):
            // 表示を決めた時点で「初回は消費済み」扱いにして完了フラグを先に保存する。
            // なぜここで保存するか: ビルド検証の pgrep kill 等でクローズ処理（markOnboardingComplete）
            // が走らないまま強制終了されると、フラグ未保存のまま毎回オンボーディングが再表示される穴
            // が開くため。savedStep による再開は OnboardingDecider が didComplete より優先するので壊れない。
            defaults.set(true, forKey: OnboardingKeys.didComplete)
            // 完了/クローズ時に startup() を呼ぶ（ここでは開始しない）
            startNow = false
            onboardingStep = step
        }
        // デバッグ強制表示: 完了済みでも重ねて開く（本体は通常どおり起動する）
        if forceOnboarding, onboardingStep == nil {
            onboardingStep = 0
        }

        if startNow {
            controller.startup()
        }
        registerLaunchAtLoginIfFirstRun()
        if let onboardingStep {
            statusBar.showOnboarding(fromStep: onboardingStep)
        }

        // デバッグ用: 起動直後に設定ウィンドウを開く（一回限り、読み取り後に削除）
        // 使い方: defaults write com.voicekey.app VOICEKEY_OPEN_SETTINGS -string 3 → 起動
        let debugTab = UserDefaults.standard.string(forKey: "VOICEKEY_OPEN_SETTINGS")
            ?? ProcessInfo.processInfo.environment["VOICEKEY_OPEN_SETTINGS"]
        if let debugTab {
            UserDefaults.standard.removeObject(forKey: "VOICEKEY_OPEN_SETTINGS")
            log.info("デバッグ: 設定ウィンドウを自動表示します (tab=\(debugTab, privacy: .public))")
            statusBar.showSettings(initialTab: Int(debugTab) ?? 0)
        }
    }

    /// 初回起動時にログイン時自動起動を登録する。
    /// 一度だけ実行するため、設定画面のトグルでオフにすれば再登録されない
    private func registerLaunchAtLoginIfFirstRun() {
        let key = "didSetupLaunchAtLogin"
        guard !UserDefaults.standard.bool(forKey: key) else { return }
        UserDefaults.standard.set(true, forKey: key)
        guard SMAppService.mainApp.status != .enabled else { return }
        do {
            try SMAppService.mainApp.register()
            log.info("ログイン時自動起動を登録しました")
        } catch {
            // swift run など未バンドル実行では失敗するが、動作には影響しない
            log.error("ログイン時自動起動の登録に失敗: \(error.localizedDescription)")
        }
    }

    /// Dock アイコンや Finder からアプリを再度開いたときの挙動。
    /// メニューバー常駐アプリは通常ウィンドウを持たないため、標準では何も起きない。
    /// ユーザーの「開き直したい」意図に応えて、オンボーディング表示中ならそれを前面へ、
    /// それ以外は設定ウィンドウを開く（行き止まりを作らない）。
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows: Bool) -> Bool {
        statusBar?.handleReopen()
        return true
    }

    func applicationWillTerminate(_ notification: Notification) {
        controller?.shutdown()
    }
}

/// メニューバーのステータスアイテムとメニュー、設定ウィンドウを管理する。
///
/// アイコンは `AppController.state` の変化を購読して更新する
/// （idle: テンプレート / 録音中: 赤・紫 / 文字起こし中: オレンジ）。
@MainActor
final class StatusItemController: NSObject, NSWindowDelegate {

    private let statusItem: NSStatusItem
    private let stateMenuItem: NSMenuItem
    private weak var controller: AppController?
    private var stateObservation: AnyCancellable?
    private var settingsWindow: NSWindow?
    private var feedbackWindow: NSWindow?
    /// 初回セットアップ（オンボーディング）ウィンドウ
    private var onboardingWindow: NSWindow?
    /// オンボーディングを完了/再起動処理済みか（クローズでの二重処理を防ぐ）
    private var onboardingFinished = false

    init(controller: AppController) {
        self.controller = controller

        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        // ドラッグでメニューバーから取り外せないようにする（誤操作で消えるのを防ぐ）
        statusItem.behavior = []
        stateMenuItem = NSMenuItem(title: controller.state.label, action: nil, keyEquivalent: "")

        super.init()

        if let button = statusItem.button {
            button.image = StatusIcon.image(for: controller.state)
            button.toolTip = "voicekey"
        }

        let menu = NSMenu()
        menu.addItem(stateMenuItem)  // 状態表示（action なし = 自動で無効表示）
        menu.addItem(.separator())

        let settings = NSMenuItem(title: "設定…", action: #selector(openSettings), keyEquivalent: ",")
        settings.target = self
        menu.addItem(settings)

        // 配布ビルドのみ: Sparkle の手動アップデート確認
        if UpdaterController.shared.isAvailable {
            let update = NSMenuItem(
                title: "アップデートを確認…",
                action: #selector(checkForUpdates),
                keyEquivalent: ""
            )
            update.target = self
            menu.addItem(update)
        }

        let feedback = NSMenuItem(
            title: "フィードバックを送る…",
            action: #selector(sendFeedback),
            keyEquivalent: ""
        )
        feedback.target = self
        menu.addItem(feedback)

        // 初回セットアップをいつでも開き直せるようにする（Phase 5）
        let guide = NSMenuItem(
            title: "セットアップガイド…",
            action: #selector(openOnboarding),
            keyEquivalent: ""
        )
        guide.target = self
        menu.addItem(guide)
        menu.addItem(.separator())

        let quit = NSMenuItem(title: "voicekey を終了", action: #selector(quitApp), keyEquivalent: "q")
        quit.target = self
        menu.addItem(quit)

        statusItem.menu = menu

        // 状態変化でアイコンと情報行を更新。
        // @Published はプロパティ更新「前」に新値を流すため、closure 引数の値を使う
        stateObservation = controller.$state.sink { [weak self] state in
            self?.statusItem.button?.image = StatusIcon.image(for: state)
            self?.stateMenuItem.title = state.label
        }
    }

    // MARK: - ウィンドウ配置・Dock 表示の共通ヘルパ

    /// ウィンドウを画面の「真の中央」へ置く。
    /// NSWindow.center() は Apple 仕様で中央よりやや上に置くため、visibleFrame
    /// （メニューバー・Dock を除いた領域）から原点を手計算する（Hud の positionPanel と同方針）。
    private func centerOnScreen(_ window: NSWindow) {
        // NSHostingController のウィンドウは SwiftUI の初回レイアウトが走るまで frame が
        // サイズ 0 のままなので、fittingSize で内容サイズを先に確定させる
        // （0 のまま中央を計算すると「画面中心が左下角」になり右上へずれて表示される）
        if window.frame.width < 50, let contentView = window.contentView {
            window.setContentSize(contentView.fittingSize)
        }
        guard let screen = window.screen ?? NSScreen.main else { return }
        let visible = screen.visibleFrame
        let size = window.frame.size
        let x = visible.minX + (visible.width - size.width) / 2
        let y = visible.minY + (visible.height - size.height) / 2
        window.setFrameOrigin(NSPoint(x: x, y: y))
    }

    /// ユーザー向けウィンドウ（設定 / オンボーディング / フィードバック）が 1 つも
    /// 可視で残らないなら Dock アイコンを引っ込める（.accessory に戻す）。
    /// windowWillClose は isVisible が false になる「前」に呼ばれるため、いま閉じるウィンドウ
    /// 自身は除外して判定する。設定ウィンドウはキャッシュ再利用され参照が残るので、参照の有無
    /// ではなく isVisible で判定する。
    private func restoreAccessoryPolicyIfNoUserWindows(closing: NSWindow?) {
        // Dock 常時表示 ON のときは、ウィンドウを閉じても Dock アイコンを引っ込めない
        if controller?.config.dockIconAlwaysVisible == true { return }
        let userWindows = [settingsWindow, feedbackWindow, onboardingWindow]
        let anyRemainingVisible = userWindows.contains { win in
            guard let win, win !== closing else { return false }
            return win.isVisible
        }
        if !anyRemainingVisible {
            NSApp.setActivationPolicy(.accessory)
        }
    }

    /// Dock / Finder からの再オープン: オンボーディング表示中ならそれを前面へ、
    /// それ以外は設定ウィンドウ（一般タブ）を開く。
    func handleReopen() {
        if let onboardingWindow, onboardingWindow.isVisible {
            NSApp.setActivationPolicy(.regular)
            NSApp.activate(ignoringOtherApps: true)
            onboardingWindow.makeKeyAndOrderFront(nil)
        } else {
            showSettings(initialTab: 0)
        }
    }

    @objc private func openSettings() {
        showSettings(initialTab: 0)
    }

    @objc private func checkForUpdates() {
        UpdaterController.shared.checkForUpdates()
    }

    /// フィードバック入力フォームを開く（本文を自社サーバーへ送信する）。
    /// ログイン済みならアカウントに紐づき、未ログインでも匿名で送れる。
    @objc private func sendFeedback() {
        showFeedback()
    }

    /// フィードバックウィンドウを表示する（開くたびに新規生成して状態をリセットする）
    private func showFeedback() {
        // 既存ウィンドウがあれば閉じてから作り直す（前回の送信済み状態を残さない）
        feedbackWindow?.close()
        let hosting = NSHostingController(
            rootView: FeedbackView(onClose: { [weak self] in
                self?.feedbackWindow?.close()
                self?.feedbackWindow = nil
            })
        )
        let window = NSWindow(contentViewController: hosting)
        window.title = "フィードバック"
        window.styleMask = [.titled, .closable]
        window.isReleasedWhenClosed = false
        GlassWindow.applyFrostedChrome(to: window)  // すりガラス化（Part B）
        window.delegate = self  // クローズ時に Dock アイコンを引っ込めるため
        feedbackWindow = window
        // Dock にアイコンを出し前面へ（アクセサリのままだと前面に出ない）
        NSApp.setActivationPolicy(.regular)
        NSApp.activate(ignoringOtherApps: true)
        window.makeKeyAndOrderFront(nil)
        // 中央配置は表示後に行う（表示前は frame がサイズ 0 のことがあり右上へずれる）
        centerOnScreen(window)
    }

    /// メニューからいつでもオンボーディングを開き直す（Phase 5）。
    @objc private func openOnboarding() {
        showOnboarding(fromStep: 0)
    }

    /// 初回セットアップ（オンボーディング）ウィンドウを表示する。
    /// FeedbackView と同じ NSWindow + NSHostingController パターン。
    func showOnboarding(fromStep: Int) {
        guard let controller else { return }
        // 既存ウィンドウがあれば作り直す（前回の進行状態を残さない）
        onboardingWindow?.close()
        onboardingFinished = false

        let model = OnboardingModel(
            startStep: OnboardingStep(rawValue: fromStep) ?? .welcome,
            config: controller.config,
            onFinish: { [weak self] in self?.finishOnboarding() },
            onRestart: { [weak self] in self?.restartForInputMonitoring() }
        )
        let hosting = NSHostingController(rootView: OnboardingView(model: model))
        let window = NSWindow(contentViewController: hosting)
        window.title = "VoiceKey へようこそ"
        window.styleMask = [.titled, .closable]
        window.isReleasedWhenClosed = false
        GlassWindow.applyFrostedChrome(to: window)  // すりガラス化（Part B）
        window.delegate = self  // クローズ（×）のスキップ扱い＋Dock アイコンを引っ込めるため
        onboardingWindow = window
        // Dock にアイコンを出し前面へ（アクセサリのままだと前面に出ない）
        NSApp.setActivationPolicy(.regular)
        NSApp.activate(ignoringOtherApps: true)
        window.makeKeyAndOrderFront(nil)
        // 中央配置は表示後に行う（表示前は frame がサイズ 0 のことがあり右上へずれる）
        centerOnScreen(window)
    }

    /// 「使い始める」= 完了。フラグを立てて本体を開始し、ウィンドウを閉じる。
    private func finishOnboarding() {
        guard !onboardingFinished else { return }
        onboardingFinished = true
        markOnboardingComplete()
        // 権限案内は済んでいるので NSAlert を二重に出さない
        controller?.startup(showPermissionAlert: false)
        onboardingWindow?.close()  // windowWillClose は onboardingFinished=true で二重処理しない
    }

    /// 入力監視の反映にプロセス再起動が必要なとき: 現在ステップを保存して再起動する。
    private func restartForInputMonitoring() {
        // 再起動後に入力監視ステップから再開する（完了フラグは立てない）
        UserDefaults.standard.set(OnboardingStep.inputMonitoring.rawValue, forKey: OnboardingKeys.savedStep)
        // 再起動なのでクローズをスキップ扱いにしない
        onboardingFinished = true
        relaunchApp()
    }

    /// アプリを再起動する（新インスタンスを起動して自分は終了）。
    private func relaunchApp() {
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/usr/bin/open")
        proc.arguments = ["-n", Bundle.main.bundlePath]
        try? proc.run()
        NSApp.terminate(nil)
    }

    /// 完了フラグを立て、中断ステップを消す。
    private func markOnboardingComplete() {
        let d = UserDefaults.standard
        d.set(true, forKey: OnboardingKeys.didComplete)
        d.removeObject(forKey: OnboardingKeys.savedStep)
    }

    /// ウィンドウのクローズ処理。設定 / オンボーディング / フィードバックの 3 ウィンドウ共通。
    /// - オンボーディングを × で閉じた場合はスキップ扱い（完了フラグを立てて毎回は出さない・本体は起動）。
    /// - どのウィンドウであっても、閉じた後に可視ウィンドウが 1 つも残らなければ Dock アイコンを引っ込める。
    func windowWillClose(_ notification: Notification) {
        let closing = notification.object as? NSWindow

        // オンボーディング固有処理（× で閉じたらスキップ扱い）は従来どおり
        if closing === onboardingWindow {
            onboardingWindow = nil
            if !onboardingFinished {  // 「使い始める」/ 再起動で処理済みなら何もしない
                onboardingFinished = true
                markOnboardingComplete()
                controller?.startup(showPermissionAlert: false)
            }
        }

        // ユーザー向けウィンドウがどれも可視でなくなったら Dock アイコンを引っ込める
        restoreAccessoryPolicyIfNoUserWindows(closing: closing)
    }

    /// 設定ウィンドウを表示する
    func showSettings(initialTab: Int) {
        log.info("設定ウィンドウを表示します (tab=\(initialTab), 既存=\(self.settingsWindow != nil))")
        var isNewWindow = false
        if settingsWindow == nil, let controller {
            let hosting = NSHostingController(
                rootView: SettingsView(
                    config: controller.config,
                    history: controller.history,
                    stats: controller.stats,
                    initialTab: initialTab
                )
            )
            let window = NSWindow(contentViewController: hosting)
            window.title = "voicekey 設定"
            window.styleMask = [.titled, .closable]
            window.isReleasedWhenClosed = false
            GlassWindow.applyFrostedChrome(to: window)  // すりガラス化（Part B）
            window.delegate = self  // クローズ時に Dock アイコンを引っ込めるため
            settingsWindow = window
            isNewWindow = true
        }
        // Dock にアイコンを出し前面へ（アクセサリのままだと前面に出ない）
        NSApp.setActivationPolicy(.regular)
        NSApp.activate(ignoringOtherApps: true)
        settingsWindow?.makeKeyAndOrderFront(nil)
        // 中央配置は表示後に行う（NSHostingController のウィンドウは初回表示まで frame が
        // サイズ 0 のことがあり、表示前に計算すると右上へずれる）。再表示時は前回位置を尊重する
        if isNewWindow, let settingsWindow { centerOnScreen(settingsWindow) }
    }

    @objc private func quitApp() {
        // applicationWillTerminate でホットキー監視等が停止される
        NSApp.terminate(nil)
    }
}

/// メニューバーアイコンの生成（状態で色が変わる）
enum StatusIcon {

    /// 状態に応じたメニューバーアイコンを返す
    static func image(for state: AppState) -> NSImage {
        switch state {
        case .idle:
            // テンプレート画像: ライト/ダークメニューバーに自動追従
            return symbol("mic.fill", color: nil)
        case .recording(let autoEnter, let handsFree):
            return symbol("mic.fill", color: handsFree ? .systemTeal : (autoEnter ? .systemPurple : .systemRed))
        case .transcribing:
            return symbol("waveform", color: .systemOrange)
        }
    }

    private static func symbol(_ name: String, color: NSColor?) -> NSImage {
        let config = NSImage.SymbolConfiguration(pointSize: 14, weight: .medium)
        guard var image = NSImage(
            systemSymbolName: name,
            accessibilityDescription: "voicekey"
        )?.withSymbolConfiguration(config) else {
            return NSImage()
        }
        if let color {
            // 色付き = 非テンプレート（録音中などの状態を色で示す）
            image = tinted(image, color: color)
            image.isTemplate = false
        } else {
            image.isTemplate = true
        }
        return image
    }

    private static func tinted(_ image: NSImage, color: NSColor) -> NSImage {
        NSImage(size: image.size, flipped: false) { rect in
            image.draw(in: rect)
            color.set()
            rect.fill(using: .sourceAtop)
            return true
        }
    }
}
