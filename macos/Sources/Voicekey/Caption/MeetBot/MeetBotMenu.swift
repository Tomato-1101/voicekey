/// メニューバーの「Meet 議事録ボット」サブメニュー
///
/// 入口は「会議に参加…」の 1 つだけ。URL を貼って参加させると、あとはボットが会議に入り
/// 字幕を議事録へ書き続ける。状態（入室待ち・記録中）はここに出す。
import AppKit
import Foundation

/// ボットの操作メニュー
@available(macOS 26.0, *)
@MainActor
final class MeetBotMenuController: NSObject, NSMenuDelegate {

    private weak var controller: AppController?

    /// メニューバーに差し込む親項目
    let menuItem: NSMenuItem

    init(controller: AppController) {
        self.controller = controller
        self.menuItem = NSMenuItem(title: "Meet 議事録ボット", action: nil, keyEquivalent: "")
        super.init()

        let submenu = NSMenu()
        submenu.delegate = self
        menuItem.submenu = submenu
    }

    // MARK: - NSMenuDelegate

    /// 開くたびに状態を作り直す
    func menuNeedsUpdate(_ menu: NSMenu) {
        menu.removeAllItems()
        guard MeetBotService.isAvailable else {
            let item = NSMenuItem(title: "Google Chrome が必要です", action: nil, keyEquivalent: "")
            item.isEnabled = false
            menu.addItem(item)
            return
        }
        guard let bot = controller?.meetBot else { return }

        let state = NSMenuItem(title: bot.state.menuTitle, action: nil, keyEquivalent: "")
        state.isEnabled = false
        menu.addItem(state)
        menu.addItem(.separator())

        if bot.state.isActive {
            let leave = NSMenuItem(title: "会議から退出", action: #selector(leaveMeeting), keyEquivalent: "")
            leave.target = self
            menu.addItem(leave)
        } else {
            let join = NSMenuItem(title: "会議に参加…", action: #selector(joinMeeting), keyEquivalent: "")
            join.target = self
            menu.addItem(join)
        }

        menu.addItem(.separator())
        let login = NSMenuItem(
            title: "ボット用ブラウザで Google にログイン…", action: #selector(openLogin), keyEquivalent: ""
        )
        login.target = self
        login.isEnabled = !bot.state.isActive
        menu.addItem(login)

        let openFolder = NSMenuItem(title: "議事録フォルダを開く", action: #selector(openTranscripts), keyEquivalent: "")
        openFolder.target = self
        menu.addItem(openFolder)
    }

    // MARK: - アクション

    /// URL を尋ねて会議に参加させる
    @objc private func joinMeeting() {
        guard let bot = controller?.meetBot else { return }
        guard let url = Self.askMeetingURL() else { return }
        bot.join(urlString: url)
    }

    @objc private func leaveMeeting() {
        controller?.meetBot.leave()
    }

    @objc private func openLogin() {
        controller?.meetBot.showLoginWindow()
    }

    @objc private func openTranscripts() {
        let directory = TranscriptRecorder.directory
        try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        NSWorkspace.shared.open(directory)
    }

    // MARK: - 入力ダイアログ

    /// 会議 URL を尋ねる
    ///
    /// クリップボードに Meet の URL が入っていれば初期値に入れる（会議 URL は直前にコピーして
    /// いることがほとんどなので、貼り付ける手間を省く）。
    ///
    /// - Returns: 入力された URL（キャンセルなら nil）
    private static func askMeetingURL() -> String? {
        let alert = NSAlert()
        alert.messageText = "会議に参加"
        alert.informativeText = "Google Meet の URL を入れてください。ボットが裏で会議に入り、"
            + "字幕を議事録として保存します。"
        alert.addButton(withTitle: "参加")
        alert.addButton(withTitle: "キャンセル")

        let field = NSTextField(frame: NSRect(x: 0, y: 0, width: 320, height: 24))
        field.placeholderString = "https://meet.google.com/abc-defg-hij"
        if let clipboard = NSPasteboard.general.string(forType: .string),
           MeetBotService.normalizedMeetURL(clipboard) != nil {
            field.stringValue = clipboard.trimmingCharacters(in: .whitespacesAndNewlines)
        }
        alert.accessoryView = field
        // 開いた直後に入力できるようにフォーカスを当てる
        alert.window.initialFirstResponder = field

        guard alert.runModal() == .alertFirstButtonReturn else { return nil }
        let value = field.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        return value.isEmpty ? nil : value
    }
}
