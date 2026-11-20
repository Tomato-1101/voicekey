//
//  ScreenshotAttachTestMode.swift
//  スクリーンショット添付の貼り付け経路の検証ハーネス（CLI モード）
//
//  撮った画像と文を、前面アプリの種類に合わせた順番・キーで貼り、クリップボードを最後に 1 回だけ
//  原本へ戻すことを、実ペーストボード操作つきで確かめる。
//    CASE1 GUI アプリ: 画像 → 画像 → 文（全部 ⌘V）・画像は PNG と TIFF の両方・原本へ 1 回で戻る
//    CASE2 ターミナル: 文 → 画像(⌃V) → 文 → 画像(⌃V) と印で区切って差し込む・原本へ戻る
//    CASE3 記号の自動削除（既定）を通しても [画像N] の印が残る
//    CASE4 記号の自動削除で文が空・画像 0 枚なら「貼るものなし」と判定する
//  画面収録の許可は CGPreflightScreenCaptureAccess の結果をログに出すだけ（要求はしない）。
//
//  使い方:
//    open -n dist/voicekey.app --args --screenshot-attach-test --log-file <path>
//  最終行の [VERDICT] status=ok を判定に使う。
//
//  範囲選択の UI（screencapture -i）は起動しない（画像は手元で作った見本）。ユーザーの実クリップボード
//  （NSPasteboard.general）には触らず専用ボードを使い、⌘V / ⌃V も送らないので前面アプリに何も入らない。
//

import AppKit
import CoreGraphics
import Foundation

@MainActor
enum ScreenshotAttachTestMode {

    /// 連続貼り付けの待ち（画像 2 枚で最大 1.6 秒）＋ Paster の restoreDelay（1.0 秒）より長く待つ
    private static let waitAfterPaste: TimeInterval = 3.0
    private static var logHandle: FileHandle?

    /// 引数を見てハーネスを実行する
    /// - Returns: ハーネスとして実行した（＝通常のアプリ起動をしない）なら true
    static func runIfRequested() -> Bool {
        let arguments = CommandLine.arguments
        guard arguments.contains("--screenshot-attach-test") else { return false }
        if let index = arguments.firstIndex(of: "--log-file"), index + 1 < arguments.count {
            let path = arguments[index + 1]
            FileManager.default.createFile(atPath: path, contents: nil)
            logHandle = FileHandle(forWritingAtPath: path)
        }
        Task {
            let failures = await runCases()
            for line in failures { log("[FAIL] \(line)") }
            log("[VERDICT] status=\(failures.isEmpty ? "ok" : "failed") failures=\(failures.count)")
            fflush(stdout)
            try? logHandle?.close()
            exit(failures.isEmpty ? 0 : 1)
        }
        while true { RunLoop.main.run(until: Date().addingTimeInterval(1.0)) }
    }

    private static func log(_ line: String) {
        print(line)
        logHandle?.write(Data((line + "\n").utf8))
    }

    /// 1 手ぶんの観測（キーを送る瞬間のクリップボード）
    private struct Observed {
        let kind: String
        let controlV: Bool
        let hasPNG: Bool
        let hasTIFF: Bool
        let text: String?
    }

    private static func runCases() async -> [String] {
        log("[PERMISSION] 画面収録の許可=\(CGPreflightScreenCaptureAccess() ? "あり" : "なし")（判定のみ・要求はしない）")
        let board = NSPasteboard(name: NSPasteboard.Name("com.voicekey.screenshot-attach-test"))
        var failures: [String] = []
        var observed: [Observed] = []
        Paster.sequenceProbe = { pasteboard, controlV in
            let png = pasteboard.data(forType: .png)
            observed.append(Observed(
                kind: png != nil ? "image" : "text",
                controlV: controlV,
                hasPNG: png != nil,
                hasTIFF: pasteboard.data(forType: .tiff) != nil,
                text: pasteboard.string(forType: .string)))
        }
        defer { Paster.sequenceProbe = nil }

        let session = makeSession()
        let text = "ここを見てください。[画像1]次にこちら。[画像2]以上です。"

        // CASE1 GUI アプリ
        board.clearContents()
        board.setString("USER-ORIGINAL-1", forType: .string)
        observed.removeAll()
        let ok1 = await Paster.pasteSequence(session.pasteItems(text: text, terminal: false),
                                             fallbackText: text, pasteboard: board, sendKeystroke: false)
        log("[CASE1] 手=\(observed.map { "\($0.kind)\($0.controlV ? "(⌃V)" : "(⌘V)")" }.joined(separator: ","))")
        if !ok1 { failures.append("CASE1 pasteSequence が失敗を返した") }
        if observed.map(\.kind) != ["image", "image", "text"] {
            failures.append("CASE1 GUI の順番が 画像→画像→文 でない")
        }
        if observed.filter({ $0.kind == "image" }).count != 2 { failures.append("CASE1 添付した画像が 2 枚でない") }
        if observed.contains(where: \.controlV) { failures.append("CASE1 GUI で ⌃V を使った") }
        if observed.contains(where: { $0.kind == "image" && !($0.hasPNG && $0.hasTIFF) }) {
            failures.append("CASE1 画像に PNG と TIFF の両方が載っていない")
        }
        if observed.last?.text != text { failures.append("CASE1 最後の手が [画像N] 入りの文でない") }
        try? await Task.sleep(for: .seconds(waitAfterPaste))
        let restored1 = board.string(forType: .string)
        log("[CASE1] 復元後=\(restored1 ?? "nil")")
        if restored1 != "USER-ORIGINAL-1" { failures.append("CASE1 原本に戻らなかった: \(restored1 ?? "nil")") }

        // CASE2 ターミナル
        board.clearContents()
        board.setString("USER-ORIGINAL-2", forType: .string)
        observed.removeAll()
        let ok2 = await Paster.pasteSequence(session.pasteItems(text: text, terminal: true),
                                             fallbackText: text, pasteboard: board, sendKeystroke: false)
        log("[CASE2] 手=\(observed.map { "\($0.kind)\($0.controlV ? "(⌃V)" : "(⌘V)")" }.joined(separator: ","))")
        if !ok2 { failures.append("CASE2 pasteSequence が失敗を返した") }
        if observed.map(\.kind) != ["text", "image", "text", "image", "text"] {
            failures.append("CASE2 ターミナルの順番が 文→画像→文→画像→文 でない")
        }
        if observed.contains(where: { $0.kind == "image" && !$0.controlV }) {
            failures.append("CASE2 ターミナルの画像を ⌃V で貼っていない")
        }
        if observed.contains(where: { $0.kind == "text" && $0.controlV }) {
            failures.append("CASE2 ターミナルの文を ⌃V で貼った")
        }
        if observed.contains(where: { ($0.text ?? "").contains("[画像") }) {
            failures.append("CASE2 ターミナルに [画像N] の印を貼った")
        }
        try? await Task.sleep(for: .seconds(waitAfterPaste))
        let restored2 = board.string(forType: .string)
        log("[CASE2] 復元後=\(restored2 ?? "nil")")
        if restored2 != "USER-ORIGINAL-2" { failures.append("CASE2 原本に戻らなかった: \(restored2 ?? "nil")") }

        // CASE3 記号の自動削除（既定の残す記号）を通しても印が残る
        let filtered = ScreenshotAttach.preservingMarkers(in: "見て！[画像1]これです【重要】", active: true) {
            SymbolFilter.filter($0)
        }
        log("[CASE3] 記号削除後=\(filtered)")
        if !filtered.contains("[画像1]") { failures.append("CASE3 記号の自動削除で印が消えた") }
        if filtered.contains("！") { failures.append("CASE3 印の外の記号が消えていない") }

        // CASE4 記号の自動削除で空になった文・画像 0 枚は貼らない
        let emptied = SymbolFilter.filter("！")
        if !ScreenshotAttach.isNothingToPaste(text: emptied, imageCount: 0) {
            failures.append("CASE4 空の文・画像 0 枚を貼るものなしと判定しない")
        }
        if ScreenshotAttach.isNothingToPaste(text: emptied, imageCount: 1) {
            failures.append("CASE4 画像があるのに貼るものなしと判定した")
        }
        log("[CASE4] 記号削除後=\"\(emptied)\"")

        board.clearContents()
        return failures
    }

    /// 見本の画像 2 枚を撮り終えた記録（範囲選択の UI は起動しない）
    private static func makeSession() -> ScreenshotSession {
        let session = ScreenshotSession()
        for (index, color) in [NSColor.systemRed, NSColor.systemBlue].enumerated() {
            let image = NSImage(size: NSSize(width: 8, height: 8))
            image.lockFocus()
            color.setFill()
            NSRect(x: 0, y: 0, width: 8, height: 8).fill()
            image.unlockFocus()
            let tiff = image.tiffRepresentation
            let png = tiff.flatMap { NSBitmapImageRep(data: $0)?.representation(using: .png, properties: [:]) } ?? Data()
            let shot = session.arm(ScreenshotAttach.ShotMark(elapsed: Double(index + 1), liveText: ""))
            session.finish(shot, capture: ScreenshotSession.Capture(png: png, tiff: tiff))
        }
        return session
    }
}
