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

    /// 貼り付けごとに増える世代番号。古い復元タスクを無効化する（連続貼り付けの世代分離）
    @MainActor private static var generation = 0
    /// 直近に自分がコピーしたテキスト（復元可否の判定に使う）
    @MainActor private static var injected: String?
    /// 復元すべきユーザーの真のクリップボード内容
    @MainActor private static var savedOriginal: String?

    /// アクティブウィンドウにテキストを貼り付ける。
    /// 待機を含むため async（スレッドはブロックしない）。
    /// 復元状態を直列化するため MainActor 隔離（呼び出し側 AppController も MainActor）
    @MainActor
    static func paste(_ text: String) async {
        guard !text.isEmpty else { return }

        let pasteboard = NSPasteboard.general
        // ユーザーのクリップボード内容を退避（テキストのみ）
        let current = pasteboard.string(forType: .string)

        // 世代を採番し、復元すべき「真のオリジナル」を確定する。
        // 連続貼り付け（前回の復元がまだ終わっていない）でクリップボードが自分の挿入
        // テキストのままなら、それを原本と誤認せず前回保存したオリジナルを引き継ぐ。
        // こうしないと最後の復元で自分の挿入テキストを書き戻してしまう。
        generation += 1
        let gen = generation
        let original: String?
        if let inj = injected, current == inj, savedOriginal != nil {
            original = savedOriginal
        } else {
            original = current
        }
        savedOriginal = original
        injected = text

        pasteboard.clearContents()
        pasteboard.setString(text, forType: .string)
        // 復元前の上書き検出用（クリップボードが書き換わるたびに増える）
        let ourChangeCount = pasteboard.changeCount

        try? await Task.sleep(for: .seconds(pasteDelay))
        postKeystroke(keyV, flags: .maskCommand)
        log.debug("テキストを貼り付けました (\(text.count) 文字)")

        // クリップボード復元は呼び出し側を待たせない（Enter 自動送信・HUD 非表示を即時化する）。
        // 貼り付け先が読み終えてから復元したいので restoreDelay は別タスクで待つ。
        guard let original, !original.isEmpty else { return }
        Task { @MainActor in
            try? await Task.sleep(for: .seconds(restoreDelay))
            // より新しい貼り付けが復元を担当する → 何もしない（世代分離）
            guard generation == gen else { return }
            // 待っている間にユーザーや他アプリが新たにコピーしていたら（changeCount 変化）、
            // それを壊さないよう復元しない
            guard pasteboard.changeCount == ourChangeCount else {
                injected = nil
                savedOriginal = nil
                return
            }
            pasteboard.clearContents()
            pasteboard.setString(original, forType: .string)
            injected = nil
            savedOriginal = nil
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
