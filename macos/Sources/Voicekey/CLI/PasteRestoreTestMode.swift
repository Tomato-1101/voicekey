//
//  PasteRestoreTestMode.swift
//  貼り付け後のクリップボード復元の検証ハーネス（CLI モード）
//
//  ユーザー指摘（2026-09-19）2 件の回帰を、実ペーストボード操作つきで確かめる。
//    (1) 音声の内容ではなく、前にコピーしていた内容が貼られる
//    (2) 文字起こし結果がクリップボードに残る
//
//  使い方:
//    dist/voicekey.app/Contents/MacOS/voicekey --paste-restore-test
//  最終行の [VERDICT] status=ok を判定に使う。
//
//  ユーザーの実クリップボード（NSPasteboard.general）には触らず専用ボードを使い、
//  ⌘V も送らないので前面アプリに文字は入らない（検証しているのは退避と復元の経路）。
//

import AppKit
import Foundation

@MainActor
enum PasteRestoreTestMode {

    /// Paster の restoreDelay（1.0 秒）より確実に長く待つ
    private static let waitAfterPaste: TimeInterval = 1.6

    /// 引数を見てハーネスを実行する
    /// - Returns: ハーネスとして実行した（＝通常のアプリ起動をしない）なら true
    static func runIfRequested() -> Bool {
        guard CommandLine.arguments.contains("--paste-restore-test") else { return false }
        Task {
            let failures = await runCases()
            for line in failures { print("[FAIL] \(line)") }
            print("[VERDICT] status=\(failures.isEmpty ? "ok" : "failed") failures=\(failures.count)")
            fflush(stdout)
            exit(failures.isEmpty ? 0 : 1)
        }
        while true { RunLoop.main.run(until: Date().addingTimeInterval(1.0)) }
    }

    /// 4 ケースを順に流し、失敗理由を返す（空なら全 PASS）
    private static func runCases() async -> [String] {
        let board = NSPasteboard(name: NSPasteboard.Name("com.voicekey.paste-restore-test"))
        var failures: [String] = []

        // CASE1 通常: 貼り付け中は文字起こし結果、復元後はユーザーの原本
        board.clearContents()
        board.setString("USER-ORIGINAL", forType: .string)
        await Paster.paste("DICTATED-1", pasteboard: board, sendKeystroke: false)
        let duringPaste = board.string(forType: .string)
        print("[CASE1] 貼り付け直後=\(duringPaste ?? "nil")")
        if duringPaste != "DICTATED-1" {
            failures.append("CASE1 貼り付け中にクリップボードが文字起こし結果になっていない")
        }
        try? await Task.sleep(for: .seconds(waitAfterPaste))
        let restored = board.string(forType: .string)
        print("[CASE1] 復元後=\(restored ?? "nil")")
        if restored != "USER-ORIGINAL" {
            failures.append("CASE1 原本に戻らなかった: \(restored ?? "nil")")
        }

        // CASE2 原本なし（空 or 非テキスト）: 文字起こし結果を残さず空にする
        board.clearContents()
        await Paster.paste("DICTATED-2", pasteboard: board, sendKeystroke: false)
        try? await Task.sleep(for: .seconds(waitAfterPaste))
        let afterEmpty = board.string(forType: .string) ?? ""
        print("[CASE2] 復元後=\(afterEmpty.isEmpty ? "(空)" : afterEmpty)")
        if !afterEmpty.isEmpty {
            failures.append("CASE2 文字起こし結果がクリップボードに残った: \(afterEmpty)")
        }

        // CASE3 復元待ちの間にユーザーがコピー: その内容を壊さない
        board.clearContents()
        board.setString("USER-ORIGINAL-3", forType: .string)
        await Paster.paste("DICTATED-3", pasteboard: board, sendKeystroke: false)
        board.clearContents()
        board.setString("USER-COPIED-LATER", forType: .string)
        try? await Task.sleep(for: .seconds(waitAfterPaste))
        let afterUserCopy = board.string(forType: .string)
        print("[CASE3] 復元後=\(afterUserCopy ?? "nil")")
        if afterUserCopy != "USER-COPIED-LATER" {
            failures.append("CASE3 ユーザーのコピーを壊した: \(afterUserCopy ?? "nil")")
        }

        // CASE4 連続貼り付け: 1 回目の挿入テキストではなく真の原本へ戻る
        board.clearContents()
        board.setString("USER-ORIGINAL-4", forType: .string)
        await Paster.paste("FIRST", pasteboard: board, sendKeystroke: false)
        await Paster.paste("SECOND", pasteboard: board, sendKeystroke: false)
        try? await Task.sleep(for: .seconds(waitAfterPaste))
        let afterConsecutive = board.string(forType: .string)
        print("[CASE4] 復元後=\(afterConsecutive ?? "nil")")
        if afterConsecutive != "USER-ORIGINAL-4" {
            failures.append("CASE4 連続貼り付け後に原本へ戻らなかった: \(afterConsecutive ?? "nil")")
        }

        board.clearContents()
        return failures
    }
}
