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
}
