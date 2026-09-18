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

/// 貼り付け後にクリップボードをどう戻すかの判定（副作用のない純ロジック＝テスト対象）。
///
/// 実クリップボードを触る前にここで決めることで、「文字起こし結果が
/// クリップボードに残り続ける」「ユーザーのコピーを壊す」の両方を検証可能にする。
enum ClipboardRestorePolicy {

    /// 復元タスクが取るべき行動
    enum Decision: Equatable {
        /// より新しい貼り付けが復元を担当する → 何もしない（世代分離）
        case skip
        /// 待っている間にユーザー・他アプリが新しくコピーした → 触らない
        case leaveUserContent
        /// 退避した原本を書き戻す
        case restore(String)
        /// 戻せる原本が無い（空 or 画像などの非テキスト）→ 自分が入れたテキストを消す
        case clear
    }

    /// - Parameters:
    ///   - currentGeneration: 現在の貼り付け世代
    ///   - taskGeneration: この復元タスクが担当する世代
    ///   - clipboardIsStillOurs: クリップボードが自分の挿入テキストのままか
    ///   - original: 退避したユーザーの原本（テキストのみ。無ければ nil）
    static func decide(currentGeneration: Int,
                       taskGeneration: Int,
                       clipboardIsStillOurs: Bool,
                       original: String?) -> Decision {
        guard currentGeneration == taskGeneration else { return .skip }
        // 判定は changeCount ではなく「中身が自分の挿入テキストか」で行う。
        // changeCount はユーザーがコピーしていなくても他要因（同一文字列の再宣言など）で
        // 増えることがあり、それで復元を諦めると文字起こし結果が残ってしまう
        guard clipboardIsStillOurs else { return .leaveUserContent }
        if let original, !original.isEmpty { return .restore(original) }
        return .clear
    }
}

enum Paster {

    /// クリップボード設定から貼り付けまでの待機（秒）
    private static let pasteDelay: TimeInterval = 0.05
    /// 貼り付け後、クリップボードを復元するまでの待機（秒）。
    ///
    /// 貼り付け先アプリが ⌘V を処理してクリップボードを読み終える前に復元すると、
    /// 復元後の内容＝ユーザーが前にコピーしていたものが貼られる。0.3 秒では
    /// Chrome / Electron / ターミナル等が読み終わらないことがあり、実際に
    /// 「音声の内容が入らず前のコピーが貼られた」不具合が出たため広げた。
    /// 復元が遅れる害は「貼り付け直後 1 秒以内の手動 ⌘V で文字起こし結果が貼られる」だけで、
    /// 取り違えより軽い。
    private static let restoreDelay: TimeInterval = 1.0

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
    ///
    /// - Parameters:
    ///   - text: 貼り付けるテキスト
    ///   - pasteboard: 使うペーストボード。既定は実クリップボード。
    ///                 検証ハーネスが専用ボードを渡し、ユーザーのクリップボードを汚さずに復元を試す
    ///   - sendKeystroke: ⌘V を合成するか。ハーネスでは false にして前面アプリへ文字を入れない
    @MainActor
    static func paste(_ text: String,
                      pasteboard: NSPasteboard = .general,
                      sendKeystroke: Bool = true) async {
        guard !text.isEmpty else { return }
        // 本文は残さない（文字数だけ）。貼り付けは「実行したのに入らない」の切り分けが要るので対で記録する
        ActionLog.shared.write("paster", "貼り付け実行 (\(text.count) 文字)")

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

        try? await Task.sleep(for: .seconds(pasteDelay))
        if sendKeystroke { postKeystroke(keyV, flags: .maskCommand) }
        log.debug("テキストを貼り付けました (\(text.count) 文字)")
        ActionLog.shared.write("paster", "貼り付け完了 (\(text.count) 文字)")

        // クリップボード復元は呼び出し側を待たせない（Enter 自動送信・HUD 非表示を即時化する）。
        // 貼り付け先が読み終えてから復元したいので restoreDelay は別タスクで待つ。
        // App Nap による沈黙は AppDelegate がプロセス全体に張っている
        // beginActivity(.userInitiatedAllowingIdleSystemSleep) で既に防いでいる
        Task { @MainActor in
            try? await Task.sleep(for: .seconds(restoreDelay))
            let stillOurs = pasteboard.string(forType: .string) == text
            let decision = ClipboardRestorePolicy.decide(currentGeneration: generation,
                                                         taskGeneration: gen,
                                                         clipboardIsStillOurs: stillOurs,
                                                         original: original)
            switch decision {
            case .skip:
                return
            case .leaveUserContent:
                ActionLog.shared.write("paster", "クリップボード復元 スキップ（ユーザーが新しくコピー）")
            case .restore(let original):
                pasteboard.clearContents()
                pasteboard.setString(original, forType: .string)
                ActionLog.shared.write("paster", "クリップボード復元 完了")
            case .clear:
                // 戻せる原本が無い（空 or 画像などの非テキスト）。ここで何もしないと
                // 文字起こし結果がクリップボードに残ってしまうため明示的に空にする
                pasteboard.clearContents()
                ActionLog.shared.write("paster", "クリップボード復元 原本なしのため消去")
            }
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
