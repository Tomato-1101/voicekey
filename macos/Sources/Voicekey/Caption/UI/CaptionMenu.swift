/// メニューバーの「ライブ字幕」サブメニュー
///
/// voicekey 既存のメニュー（`VoicekeyApp.swift` の StatusItemController）に 1 項目だけ足し、
/// 中身はここで組み立てる。**開くたびに作り直す**ので状態表示が常に最新になる。
///
/// subglass では操作ピルがメニューの持ち主だったが、voicekey ではピルが録音 HUD の役目を
/// 持っているため、字幕の操作入口はメニューバーとホットキー（⌥⌘S）に集約する。
import AppKit
import Foundation
import OSLog

/// 「ライブ字幕」サブメニューの組み立てとアクション
@available(macOS 26.0, *)
@MainActor
final class CaptionMenuController: NSObject, NSMenuDelegate {

    private let logger = makeCaptionLogger("CaptionMenu")
    /// 字幕サービスの取得元。**メニューを開くまでサービスを作らない**（起動を遅くしないため）
    private weak var controller: AppController?

    /// メニューバーに差し込む親項目
    let menuItem: NSMenuItem

    /// - Parameter controller: 字幕サービスを持つ中央コントローラ
    init(controller: AppController) {
        self.controller = controller
        self.menuItem = NSMenuItem(title: "ライブ字幕", action: nil, keyEquivalent: "")
        super.init()

        let submenu = NSMenu()
        submenu.delegate = self
        menuItem.submenu = submenu
    }

    // MARK: - NSMenuDelegate

    /// 開くたびに中身を作り直す（状態・トグルの表示を最新にするため）
    func menuNeedsUpdate(_ menu: NSMenu) {
        menu.removeAllItems()
        guard let service = controller?.caption else {
            menu.addItem(disabledItem("字幕を利用できません"))
            return
        }

        // 1. 主アクション
        let toggle = NSMenuItem(
            title: service.state.isActive ? "字幕を停止" : "字幕を開始",
            action: #selector(toggleCaption), keyEquivalent: "s"
        )
        toggle.keyEquivalentModifierMask = [.option, .command]
        toggle.target = self
        menu.addItem(toggle)
        menu.addItem(.separator())

        // 2. 状態（読み取り専用）
        menu.addItem(disabledItem(service.state.menuTitle))
        menu.addItem(disabledItem(translationStatusLine(service)))
        menu.addItem(disabledItem(captureTargetLine(service)))
        menu.addItem(.separator())

        // 3. モードと言語（翻訳するか、そのまま文字起こしするか）
        let modeItem = NSMenuItem(title: "モード", action: nil, keyEquivalent: "")
        let modeMenu = NSMenu()
        for mode in CaptionMode.allCases {
            let item = NSMenuItem(title: mode.displayName, action: #selector(selectMode(_:)), keyEquivalent: "")
            item.target = self
            item.representedObject = mode.rawValue
            item.state = service.mode == mode ? .on : .off
            modeMenu.addItem(item)
        }
        modeItem.submenu = modeMenu
        menu.addItem(modeItem)

        if service.mode == .transcribe {
            let languageItem = NSMenuItem(title: "認識する言語", action: nil, keyEquivalent: "")
            let languageMenu = NSMenu()
            for language in CaptionLanguage.allCases {
                let item = NSMenuItem(
                    title: language.displayName, action: #selector(selectLanguage(_:)), keyEquivalent: ""
                )
                item.target = self
                item.representedObject = language.rawValue
                item.state = service.language == language ? .on : .off
                languageMenu.addItem(item)
            }
            languageItem.submenu = languageMenu
            menu.addItem(languageItem)
        }

        menu.addItem(checkItem("文字起こしを保存（議事録）", #selector(toggleSaveTranscript), service.savesTranscript))
        let openTranscripts = NSMenuItem(
            title: "議事録フォルダを開く", action: #selector(openTranscriptFolder), keyEquivalent: ""
        )
        openTranscripts.target = self
        menu.addItem(openTranscripts)
        menu.addItem(.separator())

        // 4. トグル
        menu.addItem(checkItem("最前面のアプリだけを翻訳", #selector(toggleFrontmostOnly), service.capturesFrontmostOnly))
        menu.addItem(checkItem("起動時に字幕を自動開始", #selector(toggleAutoStart), CaptionSettings.startsOnLaunch))
        menu.addItem(checkItem("訳文を読み上げる", #selector(toggleSpeech), service.speaksTranslation))
        menu.addItem(checkItem("英語の原文も表示", #selector(toggleSource), service.showsSourceText))
        menu.addItem(.separator())

        // 5. 翻訳エンジン
        let engineItem = NSMenuItem(title: "翻訳エンジン", action: nil, keyEquivalent: "")
        let engineMenu = NSMenu()
        for engine in TranslationEngine.allCases {
            let item = NSMenuItem(title: engine.displayName, action: #selector(selectEngine(_:)), keyEquivalent: "")
            item.target = self
            item.representedObject = engine.rawValue
            item.state = service.translationEngine == engine ? .on : .off
            engineMenu.addItem(item)
        }
        engineItem.submenu = engineMenu
        menu.addItem(engineItem)

        let reset = NSMenuItem(title: "字幕の大きさをリセット", action: #selector(resetSize), keyEquivalent: "")
        reset.target = self
        menu.addItem(reset)
    }

    // MARK: - 表示用の行

    /// 翻訳の構成と準備状況を 1 行で
    private func translationStatusLine(_ service: CaptionService) -> String {
        // 文字起こしモードでは翻訳器を一切使わないので、翻訳の構成ではなく認識言語を出す
        if service.mode == .transcribe {
            return "文字起こし: \(service.language.displayName)（端末内・Apple）"
        }
        if CaptionService.usesMockTranslator {
            return "翻訳: モック（検証用・APIは呼ばれません）"
        }
        switch service.translationEngine {
        case .apple:
            switch service.appleReadiness {
            case .ready: return "翻訳: Apple（オンデバイス・キー不要）"
            default: return "翻訳: Apple — \(service.appleReadiness.message)"
            }
        case .gemini:
            guard let found = APIKeyStore.load(.gemini) else {
                return "翻訳: Gemini — APIキー未設定のため Apple 翻訳で代替中"
            }
            return "翻訳: Gemini \(CaptionSettings.geminiModelID)（\(found.source.displayName)・\(APIKeyStore.masked(found.key))）"
        case .groq:
            guard let found = APIKeyStore.load(.groq) else {
                return "翻訳: Groq — APIキー未設定のため Apple 翻訳で代替中"
            }
            return "翻訳: Groq \(CaptionSettings.groqModelID)（\(found.source.displayName)・\(APIKeyStore.masked(found.key))）"
        }
    }

    /// いま何の音を拾っているかを 1 行で
    ///
    /// 「最前面のアプリだけ」は便利な反面、対象を取り違えると無音の原因が分からなくなる。
    /// いま実際に何を対象にしているかを必ず出す。
    private func captureTargetLine(_ service: CaptionService) -> String {
        guard service.capturesFrontmostOnly else { return "対象: すべてのアプリ" }
        guard service.state.isActive else { return "対象: 最前面のアプリ（開始時に確定）" }
        guard let name = service.captureTargetName else {
            return "対象: 最前面のアプリ — まだ音が出ていません"
        }
        return "対象: \(name)"
    }

    /// 選択できない情報行
    private func disabledItem(_ title: String) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: nil, keyEquivalent: "")
        item.isEnabled = false
        return item
    }

    /// チェックマーク付きのトグル項目
    private func checkItem(_ title: String, _ action: Selector, _ isOn: Bool) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: action, keyEquivalent: "")
        item.target = self
        item.state = isOn ? .on : .off
        return item
    }

    // MARK: - アクション

    /// 字幕の開始／停止（ホットキーからも呼ぶ）
    @objc func toggleCaption() {
        guard let service = controller?.caption else { return }
        if service.state.isActive {
            service.stop()
        } else {
            service.start()
        }
    }

    /// 「最前面のアプリだけを翻訳」を切り替える
    ///
    /// 動作中に切り替えたときは、認識をきれいに作り直すために字幕を張り替える。
    @objc private func toggleFrontmostOnly() {
        guard let service = controller?.caption else { return }
        let wasActive = service.state.isActive
        service.capturesFrontmostOnly.toggle()
        guard wasActive else { return }
        service.stop()
        service.start()
    }

    @objc private func toggleAutoStart() {
        CaptionSettings.startsOnLaunch.toggle()
        controller?.config.captionAutoStart = CaptionSettings.startsOnLaunch
        logger.notice("起動時の字幕自動開始: \(CaptionSettings.startsOnLaunch ? "ON" : "OFF", privacy: .public)")
    }

    @objc private func toggleSpeech() {
        controller?.caption.speaksTranslation.toggle()
    }

    @objc private func toggleSource() {
        controller?.caption.showsSourceText.toggle()
    }

    /// 字幕モードを選ぶ（動作中なら CaptionService 側が認識セッションを張り直す）
    @objc private func selectMode(_ sender: NSMenuItem) {
        guard let raw = sender.representedObject as? String,
              let mode = CaptionMode(rawValue: raw) else { return }
        controller?.caption.mode = mode
        controller?.config.captionMode = mode
    }

    /// 文字起こしの認識言語を選ぶ
    @objc private func selectLanguage(_ sender: NSMenuItem) {
        guard let raw = sender.representedObject as? String,
              let language = CaptionLanguage(rawValue: raw) else { return }
        controller?.caption.language = language
        controller?.config.captionLanguage = language
    }

    @objc private func toggleSaveTranscript() {
        guard let service = controller?.caption else { return }
        service.savesTranscript.toggle()
        controller?.config.captionSaveTranscript = service.savesTranscript
    }

    /// 議事録の保存先を Finder で開く（無ければ作ってから開く）
    @objc private func openTranscriptFolder() {
        let directory = CaptionService.transcriptDirectory
        try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        NSWorkspace.shared.open(directory)
    }

    @objc private func selectEngine(_ sender: NSMenuItem) {
        guard let raw = sender.representedObject as? String,
              let engine = TranslationEngine(rawValue: raw) else { return }
        controller?.caption.translationEngine = engine
        controller?.config.captionEngine = engine
    }

    @objc private func resetSize() {
        controller?.caption.resetHUDSize()
    }
}
