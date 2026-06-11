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
        statusBar = StatusItemController(controller: controller)
        controller.startup()
        registerLaunchAtLoginIfFirstRun()

        // デバッグ用: 起動直後に設定ウィンドウを開く（一回限り、読み取り後に削除）
        // 使い方: defaults write com.voicekey.app VOICEKEY_OPEN_SETTINGS -string 3 → 起動
        let debugTab = UserDefaults.standard.string(forKey: "VOICEKEY_OPEN_SETTINGS")
            ?? ProcessInfo.processInfo.environment["VOICEKEY_OPEN_SETTINGS"]
        if let debugTab {
            UserDefaults.standard.removeObject(forKey: "VOICEKEY_OPEN_SETTINGS")
            log.info("デバッグ: 設定ウィンドウを自動表示します (tab=\(debugTab, privacy: .public))")
            statusBar?.showSettings(initialTab: Int(debugTab) ?? 0)
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

    func applicationWillTerminate(_ notification: Notification) {
        controller?.shutdown()
    }
}

/// メニューバーのステータスアイテムとメニュー、設定ウィンドウを管理する。
///
/// アイコンは `AppController.state` の変化を購読して更新する
/// （idle: テンプレート / 録音中: 赤・紫 / 文字起こし中: オレンジ）。
@MainActor
final class StatusItemController: NSObject {

    private let statusItem: NSStatusItem
    private let stateMenuItem: NSMenuItem
    private weak var controller: AppController?
    private var stateObservation: AnyCancellable?
    private var settingsWindow: NSWindow?

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

    @objc private func openSettings() {
        showSettings(initialTab: 0)
    }

    /// 設定ウィンドウを表示する
    func showSettings(initialTab: Int) {
        log.info("設定ウィンドウを表示します (tab=\(initialTab), 既存=\(self.settingsWindow != nil))")
        if settingsWindow == nil, let controller {
            let hosting = NSHostingController(
                rootView: SettingsView(
                    config: controller.config,
                    history: controller.history,
                    initialTab: initialTab
                )
            )
            let window = NSWindow(contentViewController: hosting)
            window.title = "voicekey 設定"
            window.styleMask = [.titled, .closable]
            window.isReleasedWhenClosed = false
            window.center()
            settingsWindow = window
        }
        // アクセサリアプリはそのままだと前面に出ないため明示的にアクティブ化する
        NSApp.activate(ignoringOtherApps: true)
        settingsWindow?.makeKeyAndOrderFront(nil)
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
        case .recording(let autoEnter):
            return symbol("mic.fill", color: autoEnter ? .systemPurple : .systemRed)
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
