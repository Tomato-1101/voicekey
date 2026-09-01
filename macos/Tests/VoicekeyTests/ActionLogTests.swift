//
//  ActionLogTests.swift
//  行動ログ（ActionLog）の削除判定・書き込み・ローテートの単体テスト
//
//  実ユーザーのログ（~/Library/Logs/voicekey）には触れないよう、
//  一時ディレクトリを注入した専用インスタンスで検証する。
//  ネットワーク・Keychain には一切触れない。
//

import XCTest
@testable import voicekey

final class ActionLogTests: XCTestCase {

    /// テスト用の一時ディレクトリを作る
    private func makeTempDir() -> URL {
        let dir = FileManager.default.temporaryDirectory
            .appendingPathComponent("voicekey-actionlog-test-\(UUID().uuidString)")
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir
    }

    /// yyyy-MM-dd のファイル名用日付文字列（ActionLog と同じ規則）
    private func dayText(_ date: Date) -> String {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "yyyy-MM-dd"
        return f.string(from: date)
    }

    private func day(offset: Int, from today: Date) -> String {
        dayText(Calendar.current.date(byAdding: .day, value: offset, to: today)!)
    }

    // MARK: - 削除対象の判定（純関数）

    // 保持日数を超えた日付のファイルだけを削除対象にする
    func testFilesToDeleteDropsOnlyExpiredDays() {
        let today = Date()
        let names = [
            "voicekey-\(day(offset: 0, from: today)).log",    // 今日
            "voicekey-\(day(offset: -13, from: today)).log",  // 保持内
            "voicekey-\(day(offset: -14, from: today)).log",  // ちょうど 14 日前は残す
            "voicekey-\(day(offset: -15, from: today)).log",  // 15 日前は削除
            "voicekey-\(day(offset: -400, from: today)).log", // 大昔は削除
        ]
        let deleted = ActionLog.filesToDelete(names: names, today: today)
        XCTAssertEqual(
            Set(deleted),
            [
                "voicekey-\(day(offset: -15, from: today)).log",
                "voicekey-\(day(offset: -400, from: today)).log",
            ]
        )
    }

    // 連番ローテートしたファイル（voicekey-YYYY-MM-DD.2.log）も日付で判定する
    func testFilesToDeleteHandlesSequencedFiles() {
        let today = Date()
        let old = day(offset: -30, from: today)
        let names = [
            "voicekey-\(old).log",
            "voicekey-\(old).2.log",
            "voicekey-\(day(offset: -1, from: today)).3.log",
        ]
        let deleted = ActionLog.filesToDelete(names: names, today: today)
        XCTAssertEqual(Set(deleted), ["voicekey-\(old).log", "voicekey-\(old).2.log"])
    }

    // 自分のログ以外のファイルは絶対に削除対象にしない
    func testFilesToDeleteIgnoresForeignFiles() {
        let today = Date()
        let names = [
            "app.log",                       // Python 版のログ
            "voicekey-2000-01-01.txt",       // 拡張子違い
            "voicekey.log",                  // 日付なし
            "other-2000-01-01.log",          // 別アプリ
            "voicekey-not-a-date.log",       // 日付として解釈できない
        ]
        XCTAssertEqual(ActionLog.filesToDelete(names: names, today: today), [])
    }

    // 未来日付（時計を戻した等）のファイルは消さない
    func testFilesToDeleteKeepsFutureDates() {
        let today = Date()
        let names = ["voicekey-\(day(offset: 3, from: today)).log"]
        XCTAssertEqual(ActionLog.filesToDelete(names: names, today: today), [])
    }

    // MARK: - 実際の書き込み

    // 1 行 = "HH:mm:ss.SSS [category] message" の形式で当日のファイルへ追記される
    func testWritesFormattedLineToTodayFile() throws {
        let dir = makeTempDir()
        let log = ActionLog(directory: dir)
        log.write("audio", "録音開始要求")
        log.write("app", "ホットキー押下 slot=1")
        log.flush()

        let url = dir.appendingPathComponent("voicekey-\(dayText(Date())).log")
        let lines = try String(contentsOf: url, encoding: .utf8)
            .split(separator: "\n", omittingEmptySubsequences: true)
        XCTAssertEqual(lines.count, 2)
        XCTAssertNotNil(
            lines[0].range(of: #"^\d{2}:\d{2}:\d{2}\.\d{3} \[audio\] 録音開始要求$"#, options: .regularExpression),
            "実際の行: \(lines[0])"
        )
        XCTAssertTrue(lines[1].hasSuffix("[app] ホットキー押下 slot=1"))
    }

    // 同じファイルへ追記する（インスタンスを作り直しても前の行を消さない）
    func testAppendsAcrossInstances() throws {
        let dir = makeTempDir()
        let first = ActionLog(directory: dir)
        first.write("app", "1 回目")
        first.flush()

        let second = ActionLog(directory: dir)
        second.write("app", "2 回目")
        second.flush()

        let url = dir.appendingPathComponent("voicekey-\(dayText(Date())).log")
        let text = try String(contentsOf: url, encoding: .utf8)
        XCTAssertTrue(text.contains("1 回目"))
        XCTAssertTrue(text.contains("2 回目"))
    }

    // 上限を超えたら同じ日でも連番ファイルへ切り替える
    func testRotatesToSequencedFileWhenTooLarge() throws {
        let dir = makeTempDir()
        // 1 行が 40 バイト前後になるので、上限 100 バイトなら数行で連番へ送られる
        let log = ActionLog(directory: dir, maxBytes: 100)
        for i in 1...10 {
            log.write("app", "テスト行 \(i)")
        }
        log.flush()

        let today = dayText(Date())
        let names = try FileManager.default.contentsOfDirectory(atPath: dir.path).sorted()
        XCTAssertTrue(names.contains("voicekey-\(today).log"))
        XCTAssertTrue(
            names.contains("voicekey-\(today).2.log"),
            "連番ファイルが作られていない: \(names)"
        )
        // どのファイルも上限をわずかに超える程度で収まっている（無限に太らない）
        for name in names {
            let attributes = try FileManager.default.attributesOfItem(
                atPath: dir.appendingPathComponent(name).path)
            XCTAssertLessThan((attributes[.size] as? Int) ?? 0, 200, "\(name) が大きすぎる")
        }
    }

    // 起動時に保持期間を過ぎたログを削除し、期間内のものは残す
    func testPurgesExpiredFilesOnInit() throws {
        let dir = makeTempDir()
        let today = Date()
        let expired = dir.appendingPathComponent("voicekey-\(day(offset: -60, from: today)).log")
        let recent = dir.appendingPathComponent("voicekey-\(day(offset: -1, from: today)).log")
        let foreign = dir.appendingPathComponent("app.log")
        for url in [expired, recent, foreign] {
            try Data("x\n".utf8).write(to: url)
        }

        let log = ActionLog(directory: dir)
        log.flush()  // init の掃除が終わるまで待つ

        let fm = FileManager.default
        XCTAssertFalse(fm.fileExists(atPath: expired.path), "期限切れのログが残っている")
        XCTAssertTrue(fm.fileExists(atPath: recent.path), "期間内のログを消してしまった")
        XCTAssertTrue(fm.fileExists(atPath: foreign.path), "自分以外のファイルを消してしまった")
    }

    // 保存先ディレクトリが無ければ作る
    func testCreatesDirectoryIfMissing() throws {
        let parent = makeTempDir()
        let dir = parent.appendingPathComponent("logs/nested")
        let log = ActionLog(directory: dir)
        log.write("main", "アプリ起動")
        log.flush()

        let url = dir.appendingPathComponent("voicekey-\(dayText(Date())).log")
        XCTAssertTrue(FileManager.default.fileExists(atPath: url.path))
    }
}
