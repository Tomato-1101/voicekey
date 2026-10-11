//
//  CorruptFileQuarantineTests.swift
//  壊れた JSON ストアを上書きで消さずに退避することの回帰テスト
//
//  以前は「読めなければ空で開始」したあと次の保存で元ファイルを上書きし、救える履歴まで消していた。
//  読めなかったファイルは `<名前>.corrupt-<日時>` に中身そのまま残ることを固定する。
//

import XCTest

@testable import voicekey

@MainActor
final class CorruptFileQuarantineTests: XCTestCase {

    private func makeTempDir() -> URL {
        let dir = FileManager.default.temporaryDirectory
            .appendingPathComponent("voicekey-corrupt-\(UUID().uuidString)", isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir
    }

    private func corruptCopies(in dir: URL, prefix: String) throws -> [URL] {
        try FileManager.default.contentsOfDirectory(at: dir, includingPropertiesForKeys: nil)
            .filter { $0.lastPathComponent.hasPrefix(prefix + ".corrupt-") }
    }

    // 壊れた履歴は空で開始しつつ、元の中身を退避先に残す
    func testCorruptHistoryIsQuarantinedNotOverwritten() throws {
        let dir = makeTempDir(); defer { try? FileManager.default.removeItem(at: dir) }
        let broken = Data(#"[{"id":"x","text":"途中で切れた"#.utf8)
        try broken.write(to: dir.appendingPathComponent("history.json"))

        let store = HistoryStore(directory: dir)
        XCTAssertTrue(store.items.isEmpty)
        let copies = try corruptCopies(in: dir, prefix: "history.json")
        XCTAssertEqual(copies.count, 1)
        XCTAssertEqual(try Data(contentsOf: copies[0]), broken)
    }

    // 壊れた API 使用量も同様に退避する
    func testCorruptApiUsageIsQuarantined() throws {
        let dir = makeTempDir(); defer { try? FileManager.default.removeItem(at: dir) }
        try Data("not json".utf8).write(to: dir.appendingPathComponent("api-usage.json"))
        _ = ApiUsageStore(directory: dir)
        XCTAssertEqual(try corruptCopies(in: dir, prefix: "api-usage.json").count, 1)
    }

    // 正常なファイル・ファイル無しは退避しない
    func testHealthyOrMissingFileIsNotQuarantined() throws {
        let dir = makeTempDir(); defer { try? FileManager.default.removeItem(at: dir) }
        _ = HistoryStore(directory: dir)
        try Data("[]".utf8).write(to: dir.appendingPathComponent("history.json"))
        _ = HistoryStore(directory: dir)
        XCTAssertTrue(try corruptCopies(in: dir, prefix: "history.json").isEmpty)
    }

    // 同じ秒に 2 回退避しても前の退避を潰さない
    func testSameSecondQuarantineKeepsBoth() throws {
        let dir = makeTempDir(); defer { try? FileManager.default.removeItem(at: dir) }
        let url = dir.appendingPathComponent("stats.json")
        let now = Date(timeIntervalSince1970: 1_800_000_000)
        try Data("a".utf8).write(to: url)
        let first = CorruptFileQuarantine.quarantine(url, now: now)
        try Data("b".utf8).write(to: url)
        let second = CorruptFileQuarantine.quarantine(url, now: now)
        XCTAssertNotNil(first); XCTAssertNotNil(second)
        XCTAssertNotEqual(first, second)
        XCTAssertEqual(try Data(contentsOf: first!), Data("a".utf8))
        XCTAssertEqual(try Data(contentsOf: second!), Data("b".utf8))
        XCTAssertFalse(FileManager.default.fileExists(atPath: url.path))
    }

    // MARK: - 退避できなかったとき

    /// ファイル移動を失敗させて本体を実行し、終わったら元に戻す
    private func withFailingMove(_ body: () throws -> Void) rethrows {
        let original = CorruptFileQuarantine.moveItem
        CorruptFileQuarantine.moveItem = { _, _ in throw CocoaError(.fileWriteNoPermission) }
        defer { CorruptFileQuarantine.moveItem = original }
        try body()
    }

    // 退避できなければ、追加・記録しても壊れた元ファイルを空の内容で上書きしない
    func testFailedQuarantineNeverOverwritesOriginal() throws {
        let dir = makeTempDir(); defer { try? FileManager.default.removeItem(at: dir) }
        let broken = Data("{broken".utf8)
        let history = dir.appendingPathComponent("history.json")
        let stats = dir.appendingPathComponent("stats.json")
        let usage = dir.appendingPathComponent("api-usage.json")
        for url in [history, stats, usage] { try broken.write(to: url) }

        try withFailingMove {
            let historyStore = HistoryStore(directory: dir)
            historyStore.add("新しい発話")
            let statsStore = StatsStore(directory: dir)
            statsStore.recordSession(characters: 5, recordingSeconds: 1)
            let usageStore = ApiUsageStore(directory: dir)
            usageStore.recordTokens(provider: .groq, model: "m", purpose: .formatting, inputTokens: 1, outputTokens: 1)
            usageStore.flush()
        }
        for url in [history, stats, usage] {
            XCTAssertEqual(try Data(contentsOf: url), broken, url.lastPathComponent)
        }
    }

    // 退避した・できなかったことを、呼び名とパス付きで 1 回ずつ知らせる
    func testQuarantineNotifiesOnce() throws {
        final class Box: @unchecked Sendable { var messages: [String] = [] }
        let box = Box()
        CorruptFileQuarantine.setNoticeHandler { box.messages.append($0) }
        box.messages = []  // 他のテストで溜まっていた分は捨てる

        let dir = makeTempDir(); defer { try? FileManager.default.removeItem(at: dir) }
        let url = dir.appendingPathComponent("history.json")
        try Data("x".utf8).write(to: url)
        let moved = try XCTUnwrap(CorruptFileQuarantine.quarantine(url, label: "履歴ファイル"))
        XCTAssertEqual(box.messages.count, 1)
        XCTAssertTrue(box.messages[0].hasPrefix("履歴ファイルが壊れていたので退避しました: "))
        XCTAssertTrue(box.messages[0].hasSuffix(moved.lastPathComponent))

        try Data("y".utf8).write(to: url)
        withFailingMove { XCTAssertNil(CorruptFileQuarantine.quarantine(url, label: "履歴ファイル")) }
        XCTAssertEqual(box.messages.count, 2)
        XCTAssertTrue(box.messages[1].contains("上書きせずに残しています"))
    }
}
