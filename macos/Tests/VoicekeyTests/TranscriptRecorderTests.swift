/// 議事録（文字起こしのローカル保存）の回帰テスト
///
/// 追記型なので「落ちても書いた分は残る」「会議が変われば別ファイル」が要件。
/// 実ユーザーの ~/Documents を汚さないよう、保存先は一時ディレクトリへ差し替える。
import XCTest

@testable import voicekey

final class TranscriptRecorderTests: XCTestCase {

    private var directory: URL!

    override func setUpWithError() throws {
        directory = FileManager.default.temporaryDirectory
            .appendingPathComponent("voicekey-transcript-test-\(UUID().uuidString)", isDirectory: true)
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: directory)
    }

    /// 確定文がヘッダ付きの Markdown に追記される
    func testRecordsLinesIntoMarkdown() throws {
        let recorder = TranscriptRecorder(directory: directory)
        let start = Date()
        recorder.record(text: "今日の議題は三つあります", context: "Google Chrome", language: "日本語", now: start)
        recorder.record(text: "一つ目は予算です", context: "Google Chrome", language: "日本語", now: start.addingTimeInterval(5))

        let url = try XCTUnwrap(recorder.currentFileURL)
        let content = try String(contentsOf: url, encoding: .utf8)
        XCTAssertTrue(content.hasPrefix("# 議事録 "), "ヘッダで始まること: \(content)")
        XCTAssertTrue(content.contains("記録元: Google Chrome"))
        XCTAssertTrue(content.contains("言語: 日本語"))
        XCTAssertTrue(content.contains("今日の議題は三つあります"))
        XCTAssertTrue(content.contains("一つ目は予算です"))
    }

    /// 話者名つき（Google Meet ボット）の行は太字の話者ラベルが付く
    func testRecordsSpeakerName() throws {
        let recorder = TranscriptRecorder(directory: directory)
        recorder.record(text: "よろしくお願いします", speaker: "山田", context: "Google Meet", language: "日本語")

        let url = try XCTUnwrap(recorder.currentFileURL)
        let content = try String(contentsOf: url, encoding: .utf8)
        XCTAssertTrue(content.contains("**山田**: よろしくお願いします"), content)
    }

    /// 5 分以上あいたら別の会議として新しいファイルになる
    func testSplitsFileAfterLongGap() throws {
        let recorder = TranscriptRecorder(directory: directory)
        let start = Date()
        recorder.record(text: "午前の打ち合わせ", context: nil, language: "日本語", now: start)
        let first = try XCTUnwrap(recorder.currentFileURL)

        recorder.record(text: "午後の打ち合わせ", context: nil, language: "日本語", now: start.addingTimeInterval(6 * 60))
        let second = try XCTUnwrap(recorder.currentFileURL)

        XCTAssertNotEqual(first, second, "5 分以上あいたら別ファイルにすること")
        XCTAssertTrue(try String(contentsOf: first, encoding: .utf8).contains("午前の打ち合わせ"))
        XCTAssertFalse(try String(contentsOf: first, encoding: .utf8).contains("午後の打ち合わせ"))
    }

    /// 短い間隔なら同じファイルに続けて書く
    func testKeepsFileWithinSession() throws {
        let recorder = TranscriptRecorder(directory: directory)
        let start = Date()
        recorder.record(text: "前半", context: nil, language: "日本語", now: start)
        let first = try XCTUnwrap(recorder.currentFileURL)
        recorder.record(text: "後半", context: nil, language: "日本語", now: start.addingTimeInterval(120))
        XCTAssertEqual(first, recorder.currentFileURL)
    }

    /// 同じ確定文が続けて届いても二重に書かない（認識器の再送・DOM の再読込対策）
    func testIgnoresImmediateDuplicate() throws {
        let recorder = TranscriptRecorder(directory: directory)
        let start = Date()
        recorder.record(text: "重複する文", context: nil, language: "日本語", now: start)
        recorder.record(text: "重複する文", context: nil, language: "日本語", now: start.addingTimeInterval(1))

        let url = try XCTUnwrap(recorder.currentFileURL)
        let content = try String(contentsOf: url, encoding: .utf8)
        let occurrences = content.components(separatedBy: "重複する文").count - 1
        XCTAssertEqual(occurrences, 1, content)
    }

    /// 空文字・空白だけの確定は記録しない（ファイルも作らない）
    func testIgnoresEmptyText() {
        let recorder = TranscriptRecorder(directory: directory)
        recorder.record(text: "   ", context: nil, language: "日本語")
        XCTAssertNil(recorder.currentFileURL)
    }

    /// セッションを閉じたら次は新しいファイルになる
    func testEndSessionStartsNewFile() throws {
        let recorder = TranscriptRecorder(directory: directory)
        recorder.record(text: "一回目", context: nil, language: "日本語")
        let first = try XCTUnwrap(recorder.currentFileURL)
        recorder.endSession()
        XCTAssertNil(recorder.currentFileURL)

        // 同じ分内でも別ファイルになるよう、時刻を進めて記録する
        recorder.record(text: "二回目", context: nil, language: "日本語", now: Date().addingTimeInterval(90))
        let second = try XCTUnwrap(recorder.currentFileURL)
        XCTAssertNotEqual(first, second)
    }

    /// 会議（Meet ボット）の記録は、途中で 5 分以上黙っても 1 ファイルのまま
    func testMeetingKeepsOneFileAcrossLongSilence() throws {
        let recorder = TranscriptRecorder(directory: directory)
        let start = Date()
        recorder.beginMeeting(code: "abc-defg-hij", context: "Google Meet", language: "日本語", now: start)
        recorder.record(text: "冒頭の発言", context: nil, language: "日本語", now: start.addingTimeInterval(10))
        let first = try XCTUnwrap(recorder.currentFileURL)
        recorder.record(text: "20 分後の発言", context: nil, language: "日本語", now: start.addingTimeInterval(20 * 60))
        XCTAssertEqual(first, recorder.currentFileURL, "会議中は時間があいても分けないこと")

        let content = try String(contentsOf: first, encoding: .utf8)
        XCTAssertTrue(content.contains("冒頭の発言"))
        XCTAssertTrue(content.contains("20 分後の発言"))
        XCTAssertTrue(content.contains("記録元: Google Meet"))
        XCTAssertEqual(recorder.lineCount, 2)
    }

    /// 会議のファイル名は「日付_時分秒-会議コード.md」。参加した時点でファイルができる
    func testMeetingFileNameHasSecondsAndCode() throws {
        let recorder = TranscriptRecorder(directory: directory)
        var components = DateComponents()
        (components.year, components.month, components.day) = (2026, 10, 11)
        (components.hour, components.minute, components.second) = (14, 5, 7)
        let date = try XCTUnwrap(Calendar.current.date(from: components))

        recorder.beginMeeting(code: "abc-defg-hij", context: "Google Meet", language: "日本語", now: date)
        let url = try XCTUnwrap(recorder.currentFileURL)
        XCTAssertEqual(url.lastPathComponent, "2026-10-11_140507-abc-defg-hij.md")
        XCTAssertTrue(FileManager.default.fileExists(atPath: url.path), "参加しただけでファイルができること")
        XCTAssertEqual(recorder.lineCount, 0)
    }

    /// 会議コードにファイル名に使えない文字が混ざっても安全な名前にする
    func testFileBaseNameSanitizesCode() {
        let date = Date(timeIntervalSince1970: 0)
        let name = TranscriptRecorder.fileBaseName(at: date, code: "../abc/def?x=1")
        XCTAssertFalse(name.contains("/"))
        XCTAssertFalse(name.contains("?"))
        XCTAssertTrue(name.hasSuffix("-abcdefx1"), name)
        XCTAssertFalse(TranscriptRecorder.fileBaseName(at: date, code: nil).contains("-nil"))
    }

    /// 同じ秒に始まった記録が 2 つあっても、互いのファイルに混ざらない
    func testSameSecondGetsSeparateFiles() throws {
        let start = Date()
        let first = TranscriptRecorder(directory: directory)
        first.record(text: "一つ目の記録", context: nil, language: "日本語", now: start)
        let second = TranscriptRecorder(directory: directory)
        second.record(text: "二つ目の記録", context: nil, language: "日本語", now: start)

        // 2 つの記録係は別キューなので、どちらが先に名前を取るかは決まらない
        let firstURL = try XCTUnwrap(first.currentFileURL)
        let secondURL = try XCTUnwrap(second.currentFileURL)
        XCTAssertNotEqual(firstURL, secondURL)
        let names = Set([firstURL.lastPathComponent, secondURL.lastPathComponent])
        XCTAssertEqual(names.filter { $0.hasSuffix("-2.md") }.count, 1, "\(names)")
        XCTAssertFalse(try String(contentsOf: firstURL, encoding: .utf8).contains("二つ目の記録"))
        XCTAssertFalse(try String(contentsOf: secondURL, encoding: .utf8).contains("一つ目の記録"))
    }

    /// 保存先に書けないときは失敗として伝わり、「書けた」ことにしない
    func testWriteFailureIsReported() throws {
        // 保存先と同じ名前の「ファイル」を置いて、フォルダを作れなくする
        try Data().write(to: directory)
        let recorder = TranscriptRecorder(directory: directory)
        let outcomes = OutcomeBox()
        recorder.onWrite = { outcome, _ in outcomes.append(outcome) }

        recorder.record(text: "保存できない発言", context: nil, language: "日本語")
        recorder.waitUntilWritten()

        XCTAssertNil(recorder.currentFileURL, "失敗したファイルを現在のファイルにしないこと")
        XCTAssertEqual(recorder.lineCount, 0)
        XCTAssertNotNil(recorder.lastErrorMessage)
        guard case .failed = outcomes.values.last else {
            return XCTFail("失敗が通知されること: \(outcomes.values)")
        }
    }

    /// 書けたときは行数つきで通知される
    func testWriteSuccessIsReportedWithLineCount() throws {
        let recorder = TranscriptRecorder(directory: directory)
        let outcomes = OutcomeBox()
        recorder.onWrite = { outcome, _ in outcomes.append(outcome) }
        let start = Date()
        recorder.record(text: "一行目", context: nil, language: "日本語", now: start)
        recorder.record(text: "二行目", context: nil, language: "日本語", now: start.addingTimeInterval(1))
        recorder.waitUntilWritten()

        let url = try XCTUnwrap(recorder.currentFileURL)
        XCTAssertEqual(outcomes.values.last, .written(url, lines: 2))
        XCTAssertNil(recorder.lastErrorMessage)
    }

    /// 一番新しい議事録を探せる（無ければ nil）
    func testLatestFile() throws {
        XCTAssertNil(TranscriptRecorder.latestFile(in: directory))
        let recorder = TranscriptRecorder(directory: directory)
        let start = Date()
        recorder.record(text: "古い記録", context: nil, language: "日本語", now: start)
        let old = try XCTUnwrap(recorder.currentFileURL)
        recorder.endSession()
        recorder.record(text: "新しい記録", context: nil, language: "日本語", now: start.addingTimeInterval(60))
        let new = try XCTUnwrap(recorder.currentFileURL)
        // 更新日時で判定するので、古い方を明示的に過去へずらしておく
        try FileManager.default.setAttributes(
            [.modificationDate: Date(timeIntervalSinceNow: -3600)], ofItemAtPath: old.path
        )
        XCTAssertEqual(TranscriptRecorder.latestFile(in: directory)?.lastPathComponent, new.lastPathComponent)
    }
}

/// 記録キューから届く通知を集める入れ物（テスト用・スレッドをまたぐのでロックで守る）
private final class OutcomeBox: @unchecked Sendable {
    private let lock = NSLock()
    private var stored: [TranscriptRecorder.WriteOutcome] = []

    func append(_ outcome: TranscriptRecorder.WriteOutcome) {
        lock.lock()
        stored.append(outcome)
        lock.unlock()
    }

    var values: [TranscriptRecorder.WriteOutcome] {
        lock.lock()
        defer { lock.unlock() }
        return stored
    }
}
