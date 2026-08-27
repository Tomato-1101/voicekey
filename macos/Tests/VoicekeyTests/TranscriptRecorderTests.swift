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
}
