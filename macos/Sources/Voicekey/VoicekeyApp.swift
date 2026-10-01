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

/// WindowServer を使えない CI / sandbox でも、実 URLSession で POST→GET→マージを通す検証経路。
/// トークンと URL は環境変数から読むだけで、値は出力しない。
@MainActor
private enum HistorySyncCLITestMode {
    static func runIfRequested() -> Bool {
        guard CommandLine.arguments.contains("--history-sync-cli-test") else { return false }
        guard let url = ProcessInfo.processInfo.environment["VOICEKEY_HISTORY_SYNC_TEST_URL"],
              let token = ProcessInfo.processInfo.environment["VOICEKEY_SYNC_TOKEN"],
              let directoryPath = ProcessInfo.processInfo.environment["VOICEKEY_HISTORY_SYNC_TEST_DIR"] else {
            print("[HISTORY-SYNC-TEST] status=error reason=missing_environment")
            return true
        }

        let directory = URL(fileURLWithPath: directoryPath, isDirectory: true)
        try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let history = HistoryStore(directory: directory)
        let sync = HistorySync(
            history: history,
            configuration: { .init(enabled: true, url: url) },
            directory: directory,
            tokenProvider: { token },
            tokenWriter: { _ in true },
            tokenDeleter: { true },
            automaticScheduling: false
        )
        history.onAdd = { [weak sync] in sync?.enqueue($0) }
        sync.applyConfig()
        sync.waitForIdleForTesting()
        history.add("Mac から同期した履歴", appName: "History Sync Test")
        sync.waitForIdleForTesting()
        sync.runCycleForTesting()
        RunLoop.main.run(until: Date().addingTimeInterval(0.05))

        let windows = history.allItems.filter { $0.device == "windows" }
        let label = windows.first.map { "[Windows] \($0.text)" } ?? "missing"
        print("[HISTORY-SYNC-TEST] status=\(windows.isEmpty ? "error" : "ok") received=\(windows.count) display=\(label)")
        sync.shutdown()
        return true
    }
}

@main
@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {

    /// NSApplication.delegate は弱参照のため、自前で強参照を保持する
    private static var sharedDelegate: AppDelegate?

    static func main() {
        if HistorySyncCLITestMode.runIfRequested() { return }
        // ライブ字幕の検証ハーネス（GUI を出さずに計測して終了する）。
        // 通常起動には一切影響しない（引数が無ければ素通り）。
        if CaptionTestMode.runIfRequested() { return }
        // 音声入力側（ローカル文字起こし・翻訳して入力）の検証ハーネス。同じく素通り。
        if DictationTestMode.runIfRequested() { return }
        // 貼り付け後のクリップボード復元の検証ハーネス。同じく素通り。
        if PasteRestoreTestMode.runIfRequested() { return }
        // 録音開始の待ち時間と集約デバイスの増減を測るハーネス（AUHAL 化の回帰判定）。同じく素通り。
        if AudioEngineCostTestMode.runIfRequested() { return }
        // メインウィンドウ（ホーム＋全設定タブ）を画面に出さずに PNG へ書き出す撮影ハーネス。同じく素通り。
        if UISnapshotTestMode.runIfRequested() { return }

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
    /// ライブ字幕の ⌥⌘S ホットキー（macOS 26 以降のみ。型を直接持てないので AnyObject で保持）
    private var captionHotKey: AnyObject?
    /// 外観（ライト/ダーク）の変化の監視。アプリアイコン画像の差し替えに使う
    private var appearanceObservation: NSKeyValueObservation?

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

    /// いまの外観に合ったアイコンを `NSApp.applicationIconImage` に入れる
    private func applyAppIcon() {
        NSApp.applicationIconImage = Brand.appIcon(dark: Brand.isDark(NSApp.effectiveAppearance))
    }

    /// 受信した voicekey:// URL をログイン司令塔へ渡す。
    @objc private func handleURLEvent(_ event: NSAppleEventDescriptor, withReplyEvent: NSAppleEventDescriptor) {
        guard let str = event.paramDescriptor(forKeyword: AEKeyword(keyDirectObject))?.stringValue,
              let url = URL(string: str) else { return }
        LoginCoordinator.shared.handleDeepLink(url)
    }

    /// メインメニュー（アプリ＋編集＋ウィンドウ）をコードで設置する。
    /// 標準編集キー（⌘V 等）はメニュー項目のキー割当を経由してレスポンダチェーンの
    /// cut:/copy:/paste:/selectAll: に届く仕組みのため、常駐アプリでもメニュー自体は必須。
    private func installMainMenu() {
        let main = NSMenu()

        // アプリメニュー（先頭に必須の器。⌘Q はウィンドウ前面時のみ効く＝常駐の邪魔はしない）
        let appItem = NSMenuItem()
        main.addItem(appItem)
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "voicekey を終了", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu

        // 編集メニュー（表示はされないがキー割当のフォールバック先になる）
        let editItem = NSMenuItem()
        main.addItem(editItem)
        let edit = NSMenu(title: "編集")
        edit.addItem(withTitle: "取り消す", action: Selector(("undo:")), keyEquivalent: "z")
        let redo = NSMenuItem(title: "やり直す", action: Selector(("redo:")), keyEquivalent: "z")
        redo.keyEquivalentModifierMask = [.command, .shift]
        edit.addItem(redo)
        edit.addItem(.separator())
        edit.addItem(withTitle: "カット", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        edit.addItem(withTitle: "コピー", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        edit.addItem(withTitle: "ペースト", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        edit.addItem(withTitle: "すべてを選択", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editItem.submenu = edit

        // ウィンドウメニュー。メインウィンドウを最小化・リサイズできるようにしたので、⌘M / ⌘W の
        // キー割当の受け皿として要る（編集メニューと同じく、メニューが無いとキーが効かない）。
        // windowsMenu に登録すると、開いているウィンドウの一覧も AppKit が自動で足す。
        let windowItem = NSMenuItem()
        main.addItem(windowItem)
        let windowMenu = NSMenu(title: "ウィンドウ")
        windowMenu.addItem(withTitle: "最小化", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
        windowMenu.addItem(withTitle: "拡大/縮小", action: #selector(NSWindow.performZoom(_:)), keyEquivalent: "")
        windowMenu.addItem(.separator())
        windowMenu.addItem(withTitle: "閉じる", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        windowItem.submenu = windowMenu
        NSApp.windowsMenu = windowMenu

        NSApp.mainMenu = main
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        // 行動ログの最初の 1 行。ここから次の起動行までが 1 セッション分の記録になる
        let version = Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "?"
        ActionLog.shared.write("main", "アプリ起動 (version=\(version))")

        // 外観は OS に追従させる（ライト/ダーク双方に自然に馴染む）。以前 personal を .aqua で
        // ライト固定していたが、ダーク環境で HUD の浮遊ガラス（Liquid Glass）まで白くなり
        // 「前のデザインが白くなった」と指摘されたため撤去した（ユーザー指示・2026-07-17）。

        // メインメニューを設置する。メニューバー常駐（accessory）アプリはメインメニューが無く、
        // 編集メニューのキー割当が存在しないため ⌘V/⌘C/⌘X/⌘A が全ウィンドウでビープになる
        // （セットアップガイドの入力欄・内蔵ターミナルへの貼り付け不可の実機報告・2026-07-05）。
        // メニューは画面に出ないが、キーイベントのフォールバック先として機能する。
        installMainMenu()

        // NSAlert・Dock などに出るアプリアイコンを外観に合わせる（ライト=ボーン／ダーク=カーボン）。
        // KVO の通知はメインスレッドで届く（effectiveAppearance は AppKit がメインで更新する）
        applyAppIcon()
        appearanceObservation = NSApp.observe(\.effectiveAppearance) { [weak self] _, _ in
            MainActor.assumeIsolated { self?.applyAppIcon() }
        }

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
            // ライブ字幕はオンボーディング中には触らない（権限ダイアログの直列化を壊さないため）
            setUpCaption(controller: controller)
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

        // デバッグ用: 起動直後にホーム画面を開く（一回限り、読み取り後に削除）
        // 使い方: defaults write com.voicekey.app VOICEKEY_OPEN_HOME 1 → 起動
        let openHome = UserDefaults.standard.string(forKey: "VOICEKEY_OPEN_HOME")
            ?? ProcessInfo.processInfo.environment["VOICEKEY_OPEN_HOME"]
        if openHome != nil {
            UserDefaults.standard.removeObject(forKey: "VOICEKEY_OPEN_HOME")
            log.info("デバッグ: ホーム画面を自動表示します")
            statusBar.showHome()
        }

        // デバッグ用: 起動直後にサイドノッチの履歴パネルを開く（一回限り、読み取り後に削除）
        // 使い方: defaults write com.voicekey.app VOICEKEY_OPEN_NOTCH 1 → 起動
        let openNotch = UserDefaults.standard.string(forKey: "VOICEKEY_OPEN_NOTCH")
            ?? ProcessInfo.processInfo.environment["VOICEKEY_OPEN_NOTCH"]
        if openNotch != nil {
            UserDefaults.standard.removeObject(forKey: "VOICEKEY_OPEN_NOTCH")
            log.info("デバッグ: サイドノッチの履歴パネルを自動表示します")
            statusBar.openSideNotchHistoryForDebug()
        }
    }

    /// ライブ字幕（personal・macOS 26 以降）のホットキー登録と自動開始。
    ///
    /// - ホットキーは Carbon の RegisterEventHotKey（アクセシビリティ許可が要らない＝
    ///   承認プロンプトを増やさない）。既存の CGEventTap には相乗りしない（責務を混ぜない）。
    /// - 自動開始は「設定 ON かつ 2 回目以降の起動」のときだけ。初回はシステム音声録音の
    ///   TCC ダイアログが出るため、voicekey が作り込んだ初回プロンプトの直列化に割り込ませない
    ///   （初回はメニューの「字幕を開始」から明示的に始めてもらう）。
    private func setUpCaption(controller: AppController) {
        guard #available(macOS 26.0, *) else {
            log.info("ライブ字幕は macOS 26 以降でのみ利用できます")
            return
        }
        guard !CaptionSettings.isDisabledByEnvironment else {
            log.notice("VOICEKEY_CAPTION_DISABLE=1 のためライブ字幕を無効化しました")
            return
        }

        let hotKey = CaptionHotKey { [weak controller] in
            guard let controller else { return }
            let caption = controller.caption
            if caption.state.isActive { caption.stop() } else { caption.start() }
        }
        if hotKey.register() {
            captionHotKey = hotKey
        } else {
            // 他アプリが ⌥⌘S を先に取っている場合（例: subglass の並行稼働中）は登録できない。
            // 字幕自体はメニューから操作できるので、致命ではない。
            log.error("ライブ字幕のホットキー ⌥⌘S を登録できませんでした（他アプリが使用中の可能性）")
        }

        guard CaptionSettings.shouldAutoStartNow else { return }
        // 起動直後の重い処理を避けるため、メインループが落ち着いてから開始する
        DispatchQueue.main.async {
            log.notice("設定に従ってライブ字幕を自動開始します")
            controller.caption.start()
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
    /// それ以外はメインウィンドウを前回の画面のまま前面へ出す（行き止まりを作らない・Phase B）。
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows: Bool) -> Bool {
        statusBar?.handleReopen()
        return true
    }

    func applicationWillTerminate(_ notification: Notification) {
        ActionLog.shared.write("main", "アプリ終了")
        controller?.shutdown()
        // 終了行を書き切ってからプロセスを終える（ここだけは待ってよい）
        ActionLog.shared.flush()
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
    /// 直近にログへ出したアイコン状態（同じ状態の再通知でログを重ねないため）
    private var lastLoggedIconState: AppState?
    /// ダッシュボード + 設定を統合したメインウィンドウ（v3.1・別ウィンドウの設定は廃止）
    private var mainWindow: NSWindow?
    /// メインウィンドウの表示モデル（メニュー等から dashboard / settings を差し込む）
    private var mainWindowModel: MainWindowModel?
    private var feedbackWindow: NSWindow?
    /// 画面端のサイドノッチ（履歴スリット→クリックで履歴パネル・Phase C）
    private var sideNotch: SideNotchController?
    /// オンボーディングを完了/再起動処理済みか（クローズでの二重処理を防ぐ）
    private var onboardingFinished = false
    /// 「ライブ字幕」サブメニュー（macOS 26 以降のみ。型を直接持てないので AnyObject で保持）
    private var captionMenu: AnyObject?
    /// 「Meet 議事録ボット」サブメニュー（macOS 26 限定型なので AnyObject で保持）
    private var meetBotMenu: AnyObject?

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

        // ホーム（実績・履歴・アプリ別使用率をまとめたメイン画面・Phase B）
        let home = NSMenuItem(title: "ホーム", action: #selector(openHome), keyEquivalent: "")
        home.target = self
        menu.addItem(home)

        let settings = NSMenuItem(title: "設定…", action: #selector(openSettings), keyEquivalent: ",")
        settings.target = self
        menu.addItem(settings)

        // キー未設定・無効で文字起こしが失敗したら、入力先（設定 › API キー＝タブ 5）を開いて案内する。
        // 製品版（配布ビルド）は API キータブを持たないので結線しない
        if !EmbeddedKeys.isDist {
            controller.onNeedsApiKey = { [weak self] in self?.showSettings(initialTab: 5) }
        }

        // アップデートの手動確認は設定「バージョン情報」タブのボタンに集約した（Phase B）。
        // 新バージョン検知はサイレントに行い、ホーム左上の更新ピルだけで通知する。

        // ライブ字幕（personal・macOS 26 以降）。中身はサブメニューを開くたびに作り直す
        if #available(macOS 26.0, *), !CaptionSettings.isDisabledByEnvironment {
            let caption = CaptionMenuController(controller: controller)
            menu.addItem(caption.menuItem)
            captionMenu = caption
        }

        // Google Meet 議事録ボット（Chrome が入っているときだけ出す）
        if #available(macOS 26.0, *), MeetBotService.isAvailable {
            let bot = MeetBotMenuController(controller: controller)
            menu.addItem(bot.menuItem)
            meetBotMenu = bot
        }

        let feedback = NSMenuItem(
            title: "フィードバックを送る…",
            action: #selector(sendFeedback),
            keyEquivalent: ""
        )
        feedback.target = self
        menu.addItem(feedback)

        // セットアップガイドはメニューからの再表示を撤去し、起動時の自動判定のみで表示する
        // （ユーザー指示: 「起動時にだけ表示するようにして」）。
        menu.addItem(.separator())

        let quit = NSMenuItem(title: "voicekey を終了", action: #selector(quitApp), keyEquivalent: "q")
        quit.target = self
        menu.addItem(quit)

        statusItem.menu = menu

        // 状態変化でアイコンと情報行を更新。
        // @Published はプロパティ更新「前」に新値を流すため、closure 引数の値を使う
        stateObservation = controller.$state.sink { [weak self] state in
            // アイコンの遷移を残す（「アイコンが戻らない」の切り分け用。変化したときだけ）
            if self?.lastLoggedIconState != state {
                self?.lastLoggedIconState = state
                ActionLog.shared.write(
                    "ui",
                    "[アイコン] gen=\(DiagnosticText.gen(self?.controller?.currentGeneration ?? 0)) 状態=\(state.label)")
            }
            self?.statusItem.button?.image = StatusIcon.image(for: state)
            self?.stateMenuItem.title = state.label
            // サイドノッチの点灯を録音状態に連動（既存 emitState/HUD 連動に相乗り）
            self?.sideNotch?.setRecording(state.isRecording)
        }

        // 画面端のサイドノッチ（履歴スリット）を常駐させる。「ホームを開く」は統合メインウィンドウへ。
        sideNotch = SideNotchController(
            history: controller.history,
            config: controller.config,
            onOpenHome: { [weak self] in self?.showHome() },
            onRequestFetch: { [weak controller] in controller?.historySync.requestFetch() }
        )
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
        let userWindows = [mainWindow, feedbackWindow]
        let anyRemainingVisible = userWindows.contains { win in
            guard let win, win !== closing else { return false }
            // 最小化中のウィンドウは isVisible が false だが、Dock から戻す必要があるので残っている扱いにする
            // （ここで .accessory に戻すと Dock ごと消え、最小化したウィンドウに戻れなくなる）
            return win.isVisible || win.isMiniaturized
        }
        if !anyRemainingVisible {
            NSApp.setActivationPolicy(.accessory)
        }
    }

    /// Dock / Finder からの再オープン: オンボーディング表示中ならそれを前面へ、
    /// それ以外はメインウィンドウを前面へ出す（最小化中なら元に戻す）。
    /// 画面（ホーム／設定のどのタブ）は変えない。以前は無条件にホームへ戻していたため、
    /// 設定を開いたまま最小化して Dock から戻すと毎回ホームに飛ばされていた。
    func handleReopen() {
        if mainWindowModel?.onboarding != nil {
            presentMainWindow(onboarding: true)
        } else {
            let model = currentMainWindowModel()
            log.info("メインウィンドウを前回の画面で前面へ出します (settings=\(model.showingSettings), tab=\(model.settingsTab), 既存=\(self.mainWindow != nil))")
            presentMainWindow(onboarding: false)
        }
    }

    @objc private func openHome() {
        showHome()
    }

    /// 前回選んでいた設定タブで開く（初回は一般タブ）。毎回「一般」に戻されると、
    /// 同じタブを何度も触るときに選び直しが要るため。
    @objc private func openSettings() {
        let lastTab = currentMainWindowModel().settingsTab
        showSettings(initialTab: MainWindowScreen.sanitizedTab(lastTab, validTabs: MainWindowView.settingsTabIDs))
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
            // .tint（トグル・選択・.foregroundStyle(.tint)）をシステムのアクセントではなくブランドの灯りにする
            .tint(Brand.signal)
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

    /// 初回セットアップ（オンボーディング）をメインウィンドウ内にフルウィンドウで表示する。
    /// 独立ウィンドウは廃止し、完了でダッシュボード（ホーム）に着地する。
    func showOnboarding(fromStep: Int) {
        guard let controller else { return }
        onboardingFinished = false

        let mwModel = currentMainWindowModel()
        mwModel.onboarding = OnboardingModel(
            startStep: OnboardingStep(rawValue: fromStep) ?? .welcome,
            config: controller.config,
            onFinish: { [weak self] in self?.finishOnboarding() },
            onRestart: { [weak self] in self?.restartForInputMonitoring() },
            // 練習ステップで録音に必要なサブシステム（マイク＋入力監視＋暖機）を起動する（冪等）
            startEngineForPractice: { [weak controller] in controller?.startEngineForPractice() },
            // 整形体験ステップの間だけ整形を強制 ON（保存しない一時オーバーライド）
            setFormatOverride: { [weak controller] on in controller?.practiceFormatOverride = on },
            // マイクテスト: 録音せずレベルだけを流すモニタ
            startMicMonitor: { [weak controller] onLevel in controller?.startMicMonitor(onLevel) },
            stopMicMonitor: { [weak controller] in controller?.stopMicMonitor() },
            setMicDevice: { [weak controller] uid, mon in controller?.setMicTestDevice(uid, monitoring: mon) },
            // ホットキーテスト: 押下で録音せず点灯だけ通知するモード
            setHotkeyTest: { [weak controller] on, cb in
                controller?.hotkeyTestActive = on
                controller?.onHotkeyHeldChanged = on ? cb : nil
            },
            teardown: { [weak controller] in controller?.teardownOnboardingTestModes() }
        )
        // メインウィンドウをオンボーディングサイズで前面表示する（フルウィンドウ・テイクオーバー）
        presentMainWindow(onboarding: true, center: true)
    }

    /// オンボーディングのウィンドウサイズ（2 ペインが収まる横長）。
    private var onboardingWindowSize: NSSize { NSSize(width: 900, height: 600) }

    /// 「はじめる」= 完了。フラグを立て、オンボーディングを閉じてダッシュボードへ着地し、本体を開始する。
    private func finishOnboarding() {
        guard !onboardingFinished else { return }
        onboardingFinished = true
        markOnboardingComplete()
        // テスト用の一時状態（マイクモニタ・ホットキーテスト・整形オーバーライド）を解除
        controller?.teardownOnboardingTestModes()
        // オンボーディングを閉じてダッシュボード面へ切替、通常サイズ（記憶フレーム or 既定サイズ）へ戻す
        mainWindowModel?.onboarding = nil
        mainWindowModel?.showingSettings = false
        presentMainWindow(onboarding: false)
        // 権限案内は済んでいるので NSAlert を二重に出さない
        controller?.startup(showPermissionAlert: false)
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
        ActionLog.shared.write(
            "ui",
            "[ウィンドウ] gen=\(DiagnosticText.gen(controller?.currentGeneration ?? 0)) 閉じる "
                + "対象=\(closing === mainWindow ? "メイン" : DiagnosticText.clip(closing?.title ?? "-", 40))")

        // メインウィンドウをオンボーディング中に × で閉じたらスキップ扱い（完了フラグ＋本体起動）
        if closing === mainWindow, mainWindowModel?.onboarding != nil, !onboardingFinished {
            onboardingFinished = true
            markOnboardingComplete()
            controller?.teardownOnboardingTestModes()
            mainWindowModel?.onboarding = nil
            controller?.startup(showPermissionAlert: false)
        }

        // ユーザー向けウィンドウがどれも可視でなくなったら Dock アイコンを引っ込める
        restoreAccessoryPolicyIfNoUserWindows(closing: closing)
    }

    /// メインウィンドウ（ダッシュボード + 設定）の既定サイズ＝最小サイズ。
    private var mainWindowSize: NSSize { NSSize(width: 760, height: 600) }

    /// メインウィンドウのフレーム（位置・サイズ）を記憶する名前。
    /// UserDefaults の "NSWindow Frame <名前>" に AppKit が保存する。
    private static let mainWindowAutosaveName = "VoicekeyMainWindow"

    /// メインウィンドウの表示モデル（無ければ前回の画面で復元して作る）。
    private func currentMainWindowModel() -> MainWindowModel {
        if let mainWindowModel { return mainWindowModel }
        let model = MainWindowModel.restored()
        mainWindowModel = model
        return model
    }

    /// メインウィンドウを生成（無ければ）して返す。生成直後かどうかも返す。
    private func ensureMainWindow() -> (window: NSWindow, isNew: Bool)? {
        if let mainWindow { return (mainWindow, false) }
        guard let controller else { return nil }
        let model = currentMainWindowModel()
        let hosting = NSHostingController(
            rootView: MainWindowView(
                config: controller.config,
                history: controller.history,
                historySync: controller.historySync,
                stats: controller.stats,
                updater: UpdaterController.shared,
                model: model,
                controller: controller,
                onShowOnboarding: { [weak self] in self?.showOnboarding(fromStep: 0) }
            )
            // .tint（トグル・選択・.foregroundStyle(.tint)）をシステムのアクセントではなくブランドの灯りにする
            .tint(Brand.signal)
        )
        // ウィンドウの大きさは SwiftUI の内容から自動で決めさせず、下の contentMinSize / contentMaxSize と
        // presentMainWindow で明示的に管理する。自動追従のままだと、オンボーディング（900×600 固定）から
        // 通常へ戻した直後に古い制約が残って記憶フレームの復元が負けたり、ユーザーが広げた大きさが
        // 内容の切替で戻されたりするため。
        hosting.sizingOptions = []
        let window = NSWindow(contentViewController: hosting)
        window.title = "voicekey"
        window.styleMask = [.titled, .closable, .miniaturizable, .resizable]
        window.contentMinSize = mainWindowSize
        window.isReleasedWhenClosed = false
        GlassWindow.applyFrostedChrome(to: window)  // すりガラス化
        window.delegate = self  // クローズ時に Dock アイコンを引っ込めるため
        mainWindow = window
        return (window, true)
    }

    /// メインウィンドウを前面に出す（無ければ生成。最小化中なら元に戻す）。
    /// - Parameters:
    ///   - onboarding: true ならオンボーディング用の 900×600 固定。false なら通常
    ///     （760×600 以上でリサイズ可・位置とサイズを記憶）。
    ///   - center: オンボーディングのときだけ画面中央へ置き直す（通常時の位置は記憶フレームに任せる）。
    private func presentMainWindow(onboarding: Bool, center: Bool = false) {
        guard let (window, isNew) = ensureMainWindow() else { return }
        var shouldCenter = false
        if onboarding {
            // 900×600 を通常時のフレームとして記憶させないよう、自動保存を外してからサイズを付け替える
            // （通常に戻すときに、オンボーディング前の記憶フレームへ復元する）
            window.setFrameAutosaveName("")
            window.contentMinSize = onboardingWindowSize
            window.contentMaxSize = onboardingWindowSize
            window.setContentSize(onboardingWindowSize)
            shouldCenter = isNew || center
        } else {
            window.contentMinSize = mainWindowSize
            window.contentMaxSize = NSSize(width: CGFloat.greatestFiniteMagnitude, height: CGFloat.greatestFiniteMagnitude)
            // 自動保存が外れている＝生成直後かオンボーディング明け。このときだけ記憶フレームを復元する。
            // 表示中・閉じた後の再表示では、ユーザーが動かした位置と大きさをそのまま使う（上書きしない）。
            if window.frameAutosaveName.isEmpty {
                if !window.setFrameUsingName(Self.mainWindowAutosaveName) {
                    // 記憶が無い初回だけ既定サイズで画面中央に出す
                    window.setContentSize(mainWindowSize)
                    shouldCenter = true
                }
                window.setFrameAutosaveName(Self.mainWindowAutosaveName)
            }
        }
        // Dock にアイコンを出し前面へ（アクセサリのままだと前面に出ない）
        NSApp.setActivationPolicy(.regular)
        NSApp.activate(ignoringOtherApps: true)
        if window.isMiniaturized { window.deminiaturize(nil) }
        window.makeKeyAndOrderFront(nil)
        if shouldCenter { centerOnScreen(window) }
    }

    /// メインウィンドウ（ダッシュボード or 設定）を表示する。設定は別ウィンドウを作らず、
    /// 同じウィンドウ内でモード切替する（v3.1）。
    /// - Parameter settingsTab: nil ならダッシュボード、値があればその設定タブを開く。
    func showMainWindow(settingsTab: Int?) {
        let model = currentMainWindowModel()
        // オンボーディング表示中は設定/ホームを重ねず、オンボーディングを前面化する
        if model.onboarding != nil {
            presentMainWindow(onboarding: true)
            return
        }
        if let settingsTab {
            model.settingsTab = settingsTab
            model.showingSettings = true
        } else {
            model.showingSettings = false
        }
        log.info("メインウィンドウを表示します (settings=\(model.showingSettings), tab=\(model.settingsTab), 既存=\(self.mainWindow != nil))")
        ActionLog.shared.write(
            "ui",
            "[ウィンドウ] gen=\(DiagnosticText.gen(controller?.currentGeneration ?? 0)) メインウィンドウ表示 "
                + "設定=\(model.showingSettings) tab=\(model.settingsTab) 既存=\(mainWindow != nil)")
        presentMainWindow(onboarding: false)
    }

    /// 設定を開く（メインウィンドウを設定モードで開く）。従来の呼び出し名を維持する。
    func showSettings(initialTab: Int) {
        showMainWindow(settingsTab: initialTab)
    }

    /// ホーム（メインウィンドウのダッシュボード面）を開く。
    func showHome() {
        showMainWindow(settingsTab: nil)
    }

    /// デバッグ用: サイドノッチの履歴パネルを開く（スクリーンショット確認用）。
    func openSideNotchHistoryForDebug() {
        sideNotch?.openHistory()
    }

    @objc private func quitApp() {
        // applicationWillTerminate でホットキー監視等が停止される
        NSApp.terminate(nil)
    }
}

/// メニューバーアイコンの生成（ロゴ「17 LEGEND」のキーを描き、灯りの形と色で状態を示す）
///
/// 18×18pt のキャンバス（y 下向き）に、キー枠・刻印バー・灯りを描く。
/// 待機はテンプレート画像にして OS にメニューバーの明暗を任せる。色付きの状態は
/// テンプレートにできないので、枠と刻印は描画時の外観で解決される `labelColor` で描く
/// （drawingHandler は描かれるたびにその時の外観で呼ばれるため、明暗の切替に追従する）。
enum StatusIcon {

    /// キャンバスの大きさ（pt）
    static let canvas = NSSize(width: 18, height: 18)
    /// キー枠
    private static let frameRect = NSRect(x: 1.875, y: 1.875, width: 14.25, height: 14.25)
    private static let frameRadius: CGFloat = 4.125
    private static let frameLineWidth: CGFloat = 1.35
    /// 刻印バー
    private static let legendRect = NSRect(x: 4.5, y: 11.4, width: 4.875, height: 1.35)
    private static let legendRadius: CGFloat = 0.675
    /// 灯りの中心
    private static let lightCenter = NSPoint(x: 11.7, y: 6.3)

    /// 状態に応じたメニューバーアイコンを返す
    static func image(for state: AppState) -> NSImage {
        let image: NSImage
        switch state {
        case .idle:
            // テンプレート画像: ライト/ダークメニューバーに自動追従（色は OS が決めるので黒で描く）
            image = draw { _ in
                drawKey(color: .black, legendColor: .black)
                ring(radius: 1.5, lineWidth: 1.05, color: .black)
            }
            image.isTemplate = true
        case .recording(let autoEnter, let handsFree):
            image = draw { _ in
                // 自動送信は刻印バーも灯す（＝離したら Enter が押される合図）
                drawKey(color: .labelColor, legendColor: autoEnter ? Brand.NS.signal : .labelColor)
                if handsFree {
                    // ハンズフリーは点の外側に光輪
                    ring(radius: 3.2, lineWidth: 0.9, color: Brand.NS.signal.withAlphaComponent(0.45))
                }
                dot(radius: 2.1, color: Brand.NS.signal)
            }
            image.isTemplate = false
        case .transcribing:
            image = draw { _ in
                drawKey(color: .labelColor, legendColor: .labelColor)
                ring(radius: 1.6, lineWidth: 1.2, color: Brand.NS.signal)
            }
            image.isTemplate = false
        }
        image.accessibilityDescription = "voicekey"
        return image
    }

    private static func draw(_ body: @escaping (NSRect) -> Void) -> NSImage {
        NSImage(size: canvas, flipped: true) { rect in
            body(rect)
            return true
        }
    }

    /// キー枠（線）と刻印バー（塗り）
    private static func drawKey(color: NSColor, legendColor: NSColor) {
        let frame = NSBezierPath(roundedRect: frameRect, xRadius: frameRadius, yRadius: frameRadius)
        frame.lineWidth = frameLineWidth
        color.setStroke()
        frame.stroke()
        legendColor.setFill()
        NSBezierPath(roundedRect: legendRect, xRadius: legendRadius, yRadius: legendRadius).fill()
    }

    /// 灯りの輪（線）
    private static func ring(radius: CGFloat, lineWidth: CGFloat, color: NSColor) {
        let path = NSBezierPath(ovalIn: circleRect(radius: radius))
        path.lineWidth = lineWidth
        color.setStroke()
        path.stroke()
    }

    /// 灯りの点（塗り）
    private static func dot(radius: CGFloat, color: NSColor) {
        color.setFill()
        NSBezierPath(ovalIn: circleRect(radius: radius)).fill()
    }

    private static func circleRect(radius: CGFloat) -> NSRect {
        NSRect(x: lightCenter.x - radius, y: lightCenter.y - radius, width: radius * 2, height: radius * 2)
    }
}
