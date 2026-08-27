/// Meet ボットに渡す URL の扱いの回帰テスト
///
/// 誤った URL でブラウザを起動しない（＝会議でない場所へボットを送り込まない）ことを守る。
import XCTest

@testable import voicekey

@available(macOS 26.0, *)
@MainActor
final class MeetBotURLTests: XCTestCase {

    /// scheme が無くても Meet の URL として受ける（コピペしやすさのため）
    func testAcceptsMeetURLWithoutScheme() {
        let url = MeetBotService.normalizedMeetURL("meet.google.com/abc-defg-hij")
        XCTAssertEqual(url?.absoluteString, "https://meet.google.com/abc-defg-hij")
    }

    /// 前後の空白は落とす
    func testTrimsWhitespace() {
        let url = MeetBotService.normalizedMeetURL("  https://meet.google.com/abc-defg-hij \n")
        XCTAssertEqual(url?.absoluteString, "https://meet.google.com/abc-defg-hij")
    }

    /// Meet 以外のホストは通さない
    func testRejectsOtherHosts() {
        XCTAssertNil(MeetBotService.normalizedMeetURL("https://zoom.us/j/123456"))
        XCTAssertNil(MeetBotService.normalizedMeetURL("https://example.com/meet.google.com"))
        XCTAssertNil(MeetBotService.normalizedMeetURL(""))
    }

    /// 会議コードを取り出せる（議事録の見出しに使う）
    func testExtractsMeetingCode() throws {
        let url = try XCTUnwrap(MeetBotService.normalizedMeetURL("https://meet.google.com/abc-defg-hij"))
        XCTAssertEqual(MeetBotService.meetingCode(from: url), "abc-defg-hij")
    }

    /// クエリ付き（カレンダーからコピーした形）でもコードだけ取れる
    func testExtractsMeetingCodeWithQuery() throws {
        let url = try XCTUnwrap(
            MeetBotService.normalizedMeetURL("https://meet.google.com/abc-defg-hij?authuser=0")
        )
        XCTAssertEqual(MeetBotService.meetingCode(from: url), "abc-defg-hij")
    }
}
