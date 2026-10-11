//
//  PasterSerialTests.swift
//  貼り付けの直列化とクリップボードの全型復元の回帰テスト
//
//  (1) paste() は ⌘V の前に 50ms 待つ。その間に次の貼り付けが割り込むとクリップボードが入れ替わり、
//      先の ⌘V が後の文字を貼る取り違えが起きていた。直列化していれば、先の貼り付けが返る時点の
//      クリップボードは先の文字のまま。
//  (2) 画像やリッチテキストをコピーしていても、貼り付け後に元の内容がそのまま戻る。
//
//  実クリップボードには触らない（専用の名前付きペーストボードを使い、⌘V も送らない）。
//  復元は 1 秒後なので、各テストは 1 秒強かかる。
//

import AppKit
import XCTest

@testable import voicekey

@MainActor
final class PasterSerialTests: XCTestCase {

    private var board: NSPasteboard!

    override func setUp() async throws {
        board = NSPasteboard(name: NSPasteboard.Name("voicekey.test.\(UUID().uuidString)"))
        board.clearContents()
    }

    override func tearDown() async throws {
        board.releaseGlobally()
        board = nil
    }

    /// 復元タスク（1 秒後）が走り終えるまで待つ
    private func waitForRestore() async throws {
        try await Task.sleep(for: .milliseconds(1300))
    }

    // 連続した貼り付けは 1 本ずつ流れる（⌘V を送る瞬間のクリップボードが、その回の文字のまま）
    func testConcurrentPastesAreSerialized() async throws {
        board.setString("ORIGINAL", forType: .string)
        let board = self.board!
        var seenAtKeystroke: [String?] = []
        Paster.keystrokeProbe = { seenAtKeystroke.append($0.string(forType: .string)) }
        defer { Paster.keystrokeProbe = nil }
        let first = Task { @MainActor in
            await Paster.paste("FIRST", pasteboard: board, sendKeystroke: false)
        }
        let second = Task { @MainActor in
            await Paster.paste("SECOND", pasteboard: board, sendKeystroke: false)
        }
        _ = await first.value
        _ = await second.value
        XCTAssertEqual(seenAtKeystroke, ["FIRST", "SECOND"], "先の ⌘V の時点で後の貼り付けが割り込んだ")
        // 最後には元の内容に戻る（自分の挿入テキストを原本と取り違えない）
        try await waitForRestore()
        XCTAssertEqual(board.string(forType: .string), "ORIGINAL")
    }

    // 画像だけをコピーしていても、貼り付け後に画像がそのまま戻る
    func testImageClipboardIsRestored() async throws {
        let png = Data([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A, 0x01, 0x02])
        board.setData(png, forType: .png)
        await Paster.paste("DICTATED", pasteboard: board, sendKeystroke: false)
        XCTAssertEqual(board.string(forType: .string), "DICTATED")
        try await waitForRestore()
        XCTAssertEqual(board.data(forType: .png), png)
        XCTAssertNil(board.string(forType: .string), "文字起こし結果が残っている")
    }

    // リッチテキスト（HTML＋文字列）も両方の型が戻る
    func testRichTextClipboardIsRestored() async throws {
        let html = Data("<b>bold</b>".utf8)
        let item = NSPasteboardItem()
        item.setData(html, forType: .html)
        item.setString("bold", forType: .string)
        board.writeObjects([item])
        await Paster.paste("DICTATED", pasteboard: board, sendKeystroke: false)
        try await waitForRestore()
        XCTAssertEqual(board.data(forType: .html), html)
        XCTAssertEqual(board.string(forType: .string), "bold")
    }

    // 元々空なら、貼り付け後は空に戻る（文字起こし結果を残さない）
    func testEmptyClipboardIsCleared() async throws {
        await Paster.paste("DICTATED", pasteboard: board, sendKeystroke: false)
        try await waitForRestore()
        XCTAssertNil(board.string(forType: .string))
    }
}
