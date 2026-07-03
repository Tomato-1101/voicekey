//
//  StatsStoreTests.swift
//  実績ストアのアプリ別集計・後方互換読み込みの単体テスト
//
//  実ユーザーの実績ファイルに触れないよう、一時ディレクトリを注入して検証する
//  （StatsStore(directory:) はテスト用の保存先注入）。テストバイナリからは本番の
//  認証セッション（Keychain）に到達しないため未ログイン扱いになり、サーバー同期は走らない。
//

import XCTest
@testable import voicekey

@MainActor
final class StatsStoreTests: XCTestCase {

    /// テスト用の一時ディレクトリを作る
    private func makeTempDir() -> URL {
        let dir = FileManager.default.temporaryDirectory
            .appendingPathComponent("voicekey-stats-test-\(UUID().uuidString)")
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir
    }

    // recordSession で貼り付け先アプリ別に累計される
    func testRecordSessionAggregatesByApp() {
        let dir = makeTempDir()
        defer { try? FileManager.default.removeItem(at: dir) }

        let store = StatsStore(directory: dir)
        store.recordSession(characters: 10, recordingSeconds: 2, appBundleID: "com.apple.Safari", appName: "Safari")
        store.recordSession(characters: 5, recordingSeconds: 1, appBundleID: "com.apple.Safari", appName: "Safari")
        store.recordSession(characters: 8, recordingSeconds: 3, appBundleID: "com.apple.Notes", appName: "Notes")

        let safari = store.data.appUsage["com.apple.Safari"]
        XCTAssertEqual(safari?.sessions, 2)
        XCTAssertEqual(safari?.characters, 15)
        XCTAssertEqual(safari?.seconds, 3)
        XCTAssertEqual(safari?.appName, "Safari")

        let notes = store.data.appUsage["com.apple.Notes"]
        XCTAssertEqual(notes?.sessions, 1)
        XCTAssertEqual(notes?.characters, 8)
    }

    // bundleID を渡さないセッションは全体集計のみ（アプリ別には載らない）
    func testRecordSessionWithoutAppOnlyCountsGlobal() {
        let dir = makeTempDir()
        defer { try? FileManager.default.removeItem(at: dir) }

        let store = StatsStore(directory: dir)
        store.recordSession(characters: 10, recordingSeconds: 2)
        XCTAssertTrue(store.data.appUsage.isEmpty)
        XCTAssertEqual(store.data.totalCharacters, 10)
        XCTAssertEqual(store.data.totalSessions, 1)
    }

    // アプリ名は記録のたびに最新へ更新される（累計は保たれる）
    func testAppNameUpdatesToLatest() {
        let dir = makeTempDir()
        defer { try? FileManager.default.removeItem(at: dir) }

        let store = StatsStore(directory: dir)
        store.recordSession(characters: 3, recordingSeconds: 1, appBundleID: "com.example.app", appName: "旧名")
        store.recordSession(characters: 4, recordingSeconds: 1, appBundleID: "com.example.app", appName: "新名")
        XCTAssertEqual(store.data.appUsage["com.example.app"]?.appName, "新名")
        XCTAssertEqual(store.data.appUsage["com.example.app"]?.characters, 7)
    }

    // 旧 JSON（appUsage フィールド無し）を後方互換で読める
    func testLoadsLegacyStatsWithoutAppUsage() {
        let dir = makeTempDir()
        defer { try? FileManager.default.removeItem(at: dir) }
        let legacy = #"{"totalSessions":3,"totalCharacters":100,"daily":{}}"#
        try! Data(legacy.utf8).write(to: dir.appendingPathComponent("stats.json"))

        let store = StatsStore(directory: dir)
        XCTAssertEqual(store.data.totalSessions, 3)
        XCTAssertEqual(store.data.totalCharacters, 100)
        XCTAssertTrue(store.data.appUsage.isEmpty)  // 欠損は空辞書で開始
    }

    // アプリ別集計は保存され、同じディレクトリで作り直すと復元される
    func testAppUsagePersistsAndReloads() {
        let dir = makeTempDir()
        defer { try? FileManager.default.removeItem(at: dir) }

        let store = StatsStore(directory: dir)
        store.recordSession(characters: 12, recordingSeconds: 2, appBundleID: "com.apple.Terminal", appName: "Terminal")

        let reloaded = StatsStore(directory: dir)
        XCTAssertEqual(reloaded.data.appUsage["com.apple.Terminal"]?.characters, 12)
        XCTAssertEqual(reloaded.data.appUsage["com.apple.Terminal"]?.sessions, 1)
    }
}
