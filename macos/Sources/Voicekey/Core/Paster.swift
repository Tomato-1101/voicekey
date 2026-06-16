//
//  Paster.swift
//  文字起こし結果のテキスト挿入（クリップボード + Cmd+V 合成イベント）
//
//  日本語などのマルチバイト文字を確実に入力するため、キーストローク合成では
//  なくクリップボード経由で貼り付ける。ユーザーが元々コピーしていたテキストは
//  貼り付け後に復元する（テキストのみ。画像等は復元できない）。
//

import AppKit
import CoreGraphics
import Foundation
import os.log

private let log = Logger(subsystem: "com.voicekey.app", category: "paster")

enum Paster {

    /// クリップボード設定から貼り付けまでの待機（秒）
    private static let pasteDelay: TimeInterval = 0.05
    /// 貼り付け後、クリップボードを復元するまでの待機（秒）。
    /// 貼り付け先アプリが読み終える前に復元すると古い内容が貼られるため
    private static let restoreDelay: TimeInterval = 0.3

    /// V キーのキーコード（kVK_ANSI_V）
    private static let keyV: CGKeyCode = 9
    /// Return キーのキーコード（kVK_Return）
    private static let keyReturn: CGKeyCode = 36

    /// アクティブウィンドウにテキストを貼り付ける。
    /// 待機を含むため async（スレッドはブロックしない）
    static func paste(_ text: String) async {
        guard !text.isEmpty else { return }

        let pasteboard = NSPasteboard.general
        // ユーザーのクリップボード内容を退避（テキストのみ）
        let saved = pasteboard.string(forType: .string)

        pasteboard.clearContents()
        pasteboard.setString(text, forType: .string)
        // 復元前の上書き検出用（クリップボードが書き換わるたびに増える）
        let ourChangeCount = pasteboard.changeCount

        try? await Task.sleep(for: .seconds(pasteDelay))
        postKeystroke(keyV, flags: .maskCommand)
        log.debug("テキストを貼り付けました (\(text.count) 文字)")

        // クリップボード復元は呼び出し側を待たせない（Enter 自動送信・HUD 非表示を即時化する）。
        // 貼り付け先が読み終えてから復元したいので restoreDelay は別タスクで待つ。
        // 待っている間にユーザーや他アプリが新たにコピーしていたら（changeCount 変化）、
        // それを壊さないよう復元しない
        if let saved, !saved.isEmpty {
            Task {
                try? await Task.sleep(for: .seconds(restoreDelay))
                if pasteboard.changeCount == ourChangeCount {
                    pasteboard.clearContents()
                    pasteboard.setString(saved, forType: .string)
                }
            }
        }
    }

    /// Enter キーを 1 回送信する（ダブルタップ自動送信用）
    static func pressEnter() {
        postKeystroke(keyReturn, flags: [])
    }

    /// 合成キーストロークを送出する（アクセシビリティ権限が必要）
    private static func postKeystroke(_ keyCode: CGKeyCode, flags: CGEventFlags) {
        guard let source = CGEventSource(stateID: .combinedSessionState) else {
            log.error("CGEventSource の作成に失敗")
            return
        }
        let down = CGEvent(keyboardEventSource: source, virtualKey: keyCode, keyDown: true)
        let up = CGEvent(keyboardEventSource: source, virtualKey: keyCode, keyDown: false)
        down?.flags = flags
        up?.flags = flags
        down?.post(tap: .cghidEventTap)
        up?.post(tap: .cghidEventTap)
    }
}
