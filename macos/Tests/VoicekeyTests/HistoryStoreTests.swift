//
//  HistoryStoreTests.swift
//  履歴ストアの旧形式互換読み込み・200 件上限・メタデータ保持の単体テスト
//
//  実ユーザーの履歴ファイルに触れないよう、一時ディレクトリを注入して検証する
//  （HistoryStore(directory:) はテスト用の保存先注入）。
//

import XCTest
@testable import voicekey

@MainActor
final class HistoryStoreTests: XCTestCase {

    /// テスト用の一時ディレクトリを作る
    private func makeTempDir() -> URL {
        let dir = FileManager.default.temporaryDirectory
            .appendingPathComponent("voicekey-history-test-\(UUID().uuidString)")
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir
    }

    /// 一時ディレクトリに history.json を書く
    private func write(_ json: String, to dir: URL) {
        try! Data(json.utf8).write(to: dir.appendingPathComponent("history.json"))
    }

    // 旧形式（id/text/date のみ・メタデータ無し）を読み、欠損フィールドを補完する
    func testLoadsLegacyEntryFormat() {
        let dir = makeTempDir()
        defer { try? FileManager.default.removeItem(at: dir) }
        write(#"[{"id":"11111111-1111-1111-1111-111111111111","text":"こんにちは","date":"2026-07-01T00:00:00Z"}]"#, to: dir)

        let store = HistoryStore(directory: dir)
        XCTAssertEqual(store.items.count, 1)
        XCTAssertEqual(store.items[0].text, "こんにちは")
        XCTAssertNil(store.items[0].appBundleID)   // 旧データはアプリ情報を持たない
        XCTAssertNil(store.items[0].appName)
        XCTAssertEqual(store.items[0].characters, 5)  // text の文字数で補完される
    }

    // さらに古い「文字列配列」形式も読める（Windows 版由来の互換）
    func testLoadsLegacyStringArrayFormat() {
        let dir = makeTempDir()
        defer { try? FileManager.default.removeItem(at: dir) }
        write(#"["one","two","three"]"#, to: dir)

        let store = HistoryStore(directory: dir)
        XCTAssertEqual(store.items.count, 3)
        XCTAssertEqual(store.items[0].text, "one")
        XCTAssertEqual(store.items[0].characters, 3)
        XCTAssertNil(store.items[0].appBundleID)
    }

    // 新形式（メタデータ付き）を完全に読める
    func testLoadsNewFormatWithMetadata() {
        let dir = makeTempDir()
        defer { try? FileManager.default.removeItem(at: dir) }
        write(#"[{"id":"11111111-1111-1111-1111-111111111111","text":"hi","date":"2026-07-01T00:00:00Z","appBundleID":"com.apple.Safari","appName":"Safari","characters":2}]"#, to: dir)

        let store = HistoryStore(directory: dir)
        XCTAssertEqual(store.items[0].appBundleID, "com.apple.Safari")
        XCTAssertEqual(store.items[0].appName, "Safari")
        XCTAssertEqual(store.items[0].characters, 2)
    }

    // 壊れた JSON はクラッシュせず空で開始する
    func testCorruptFileStartsEmpty() {
        let dir = makeTempDir()
        defer { try? FileManager.default.removeItem(at: dir) }
        write("{ これは壊れた JSON", to: dir)

        let store = HistoryStore(directory: dir)
        XCTAssertTrue(store.items.isEmpty)
    }

    // 保存ファイルが無くてもクラッシュせず空で開始する
    func testMissingFileStartsEmpty() {
        let dir = makeTempDir()
        defer { try? FileManager.default.removeItem(at: dir) }

        let store = HistoryStore(directory: dir)
        XCTAssertTrue(store.items.isEmpty)
    }

    // add は 200 件を超えたら古いものを捨てる（新しい順を保つ）
    func testAddCapsAt200() {
        let dir = makeTempDir()
        defer { try? FileManager.default.removeItem(at: dir) }

        let store = HistoryStore(directory: dir)
        for i in 0..<205 { store.add("テキスト\(i)") }
        XCTAssertEqual(store.items.count, HistoryStore.maxItems)
        XCTAssertEqual(HistoryStore.maxItems, 200)
        XCTAssertEqual(store.items.first?.text, "テキスト204")  // 最後に追加したものが先頭
        XCTAssertEqual(store.items.last?.text, "テキスト5")     // 0〜4 は押し出された
    }

    // 200 件を超える保存ファイルは load で 200 件に切り詰める
    func testLoadCapsAt200() {
        let dir = makeTempDir()
        defer { try? FileManager.default.removeItem(at: dir) }
        let arr = (0..<250).map { "\"s\($0)\"" }
        write("[" + arr.joined(separator: ",") + "]", to: dir)

        let store = HistoryStore(directory: dir)
        XCTAssertEqual(store.items.count, 200)
    }

    // add のメタデータ（貼り付け先アプリ）が保持される
    func testAddStoresMetadata() {
        let dir = makeTempDir()
        defer { try? FileManager.default.removeItem(at: dir) }

        let store = HistoryStore(directory: dir)
        store.add("hello", appBundleID: "com.apple.dt.Xcode", appName: "Xcode")
        XCTAssertEqual(store.items.first?.appBundleID, "com.apple.dt.Xcode")
        XCTAssertEqual(store.items.first?.appName, "Xcode")
        XCTAssertEqual(store.items.first?.characters, 5)
    }

    // 空・空白のみのテキストは追加されない
    func testAddIgnoresBlank() {
        let dir = makeTempDir()
        defer { try? FileManager.default.removeItem(at: dir) }

        let store = HistoryStore(directory: dir)
        store.add("   \n ")
        XCTAssertTrue(store.items.isEmpty)
    }
}
