/// Meet ボットの参加スクリプトが「押してよいボタン」だけを押すかの回帰テスト
///
/// 本人と同じ Google アカウントでボットを入れると、Meet は「今すぐ参加」を出さず
/// 「このデバイスに切り替える」（本人の通話を奪う）と「その他の参加方法」だけを出す（2026-11-18 実測）。
/// 実会議は使えないので、JavaScriptCore に最小の偽 DOM を作り、実際のスクリプトを走らせて
/// どのボタンが押されたかを確かめる。
import JavaScriptCore
import XCTest

@testable import voicekey

@available(macOS 26.0, *)
@MainActor
final class MeetBotJoinScriptTests: XCTestCase {

    /// 見えているボタンだけの偽ページで参加スクリプトを走らせ、押されたボタンの表示文字を返す
    private func run(buttons labels: [String]) throws -> (clicked: String?, pressed: [String]) {
        let context = try XCTUnwrap(JSContext())
        context.exceptionHandler = { _, error in
            XCTFail("JS の例外: \(error?.toString() ?? "?")")
        }
        context.setObject(labels, forKeyedSubscript: "fakeLabels" as NSString)
        context.evaluateScript("""
            var pressed = [];
            var fakeButtons = fakeLabels.map(function (text) {
                return {
                    offsetParent: {},
                    innerText: text,
                    getAttribute: function () { return null; },
                    click: function () { pressed.push(text); }
                };
            });
            var document = {
                querySelectorAll: function (selector) {
                    return selector.indexOf('button') >= 0 ? fakeButtons : [];
                }
            };
            var window = {};
            """)
        let script = MeetBotService.joinScript(displayName: "voicekey")
        let result = try XCTUnwrap(context.evaluateScript(script))
        let clicked = result.forProperty("clicked")
        let pressed = context.objectForKeyedSubscript("pressed").toArray() as? [String] ?? []
        return (clicked?.isNull == false ? clicked?.toString() : nil, pressed)
    }

    /// 同じアカウントのロビー: 切り替えは押さず「その他の参加方法」を開く
    func testSameAccountLobbyOpensOtherWays() throws {
        let r = try run(buttons: ["このデバイスに切り替える", "その他の参加方法"])
        XCTAssertEqual(r.pressed, ["その他の参加方法"])
    }

    /// 開いた後: 「このデバイスでも参加」を押し、切り替え・コンパニオンには触らない
    func testSameAccountJoinsHereToo() throws {
        let r = try run(buttons: ["このデバイスに切り替える", "このデバイスでも参加", "コンパニオン モードを使用"])
        XCTAssertEqual(r.pressed, ["このデバイスでも参加"])
    }

    /// 英語 UI でも同じ
    func testSameAccountEnglish() throws {
        XCTAssertEqual(try run(buttons: ["Switch here", "Other ways to join"]).pressed, ["Other ways to join"])
        XCTAssertEqual(try run(buttons: ["Switch here", "Join here too", "Use Companion mode"]).pressed, ["Join here too"])
    }

    /// 別アカウント・ゲストのいつものロビーは従来どおり「今すぐ参加」
    func testNormalLobbyJoinsNow() throws {
        XCTAssertEqual(try run(buttons: ["その他の参加方法", "今すぐ参加"]).pressed, ["今すぐ参加"])
        XCTAssertEqual(try run(buttons: ["参加をリクエスト"]).pressed, ["参加をリクエスト"])
    }

    /// 押せるものが無ければ何も押さない
    func testNothingToPress() throws {
        let r = try run(buttons: ["このデバイスに切り替える", "画面を共有"])
        XCTAssertEqual(r.pressed, [])
        XCTAssertNil(r.clicked)
    }
}

/// 状態取得スクリプトが参加人数を読めるかの回帰テスト（偽の会議画面で実際のスクリプトを走らせる）
@available(macOS 26.0, *)
@MainActor
final class MeetBotPageStatusScriptTests: XCTestCase {

    /// 退出ボタン・参加者のタイル・人数バッジを並べた偽ページで走らせ、participants を返す
    private func participants(buttons: [String], ids: [String], counts: [String]) throws -> Int {
        let context = try XCTUnwrap(JSContext())
        context.exceptionHandler = { _, error in
            XCTFail("JS の例外: \(error?.toString() ?? "?")")
        }
        context.setObject(buttons, forKeyedSubscript: "fakeLabels" as NSString)
        context.setObject(ids, forKeyedSubscript: "fakeIds" as NSString)
        context.setObject(counts, forKeyedSubscript: "fakeCounts" as NSString)
        context.evaluateScript("""
            var element = function (name, value, text) {
                return {
                    offsetParent: {},
                    innerText: text || '',
                    getAttribute: function (key) { return key === name ? value : null; }
                };
            };
            var fakeButtons = fakeLabels.map(function (text) { return element('', null, text); });
            var fakeTiles = fakeIds.map(function (id) { return element('data-participant-id', id); });
            var fakeBadges = fakeCounts.map(function (count) { return element('data-avatar-count', count); });
            var document = {
                body: { innerText: '' },
                querySelectorAll: function (selector) {
                    if (selector.indexOf('data-participant-id') >= 0) return fakeTiles;
                    if (selector.indexOf('data-avatar-count') >= 0) return fakeBadges;
                    return selector.indexOf('button') >= 0 ? fakeButtons : [];
                }
            };
            var window = {};
            var location = { host: 'meet.google.com' };
            """)
        let result = try XCTUnwrap(context.evaluateScript(MeetBotService.pageStatusScript))
        return Int(try XCTUnwrap(result.forProperty("participants")).toInt32())
    }

    /// 同じ参加者のタイルが複数あっても 1 人と数え、人数バッジと比べて大きい方を採る
    func testCountsDistinctParticipantsAndBadge() throws {
        let device = "spaces/x/devices/"
        XCTAssertEqual(
            try participants(buttons: ["通話から退出"], ids: [device + "1", device + "1", device + "2"], counts: ["2"]), 2)
        XCTAssertEqual(try participants(buttons: ["通話から退出"], ids: [device + "1"], counts: ["1"]), 1)
        XCTAssertEqual(try participants(buttons: ["通話から退出"], ids: [device + "1"], counts: ["3", "x"]), 3)
        XCTAssertEqual(try participants(buttons: ["通話から退出"], ids: [], counts: []), 0)
    }

    /// 会議の外（ロビーなど）では、それらしい要素があっても 0
    func testZeroOutsideMeeting() throws {
        XCTAssertEqual(try participants(buttons: ["今すぐ参加"], ids: ["spaces/x/devices/1"], counts: ["2"]), 0)
    }
}
