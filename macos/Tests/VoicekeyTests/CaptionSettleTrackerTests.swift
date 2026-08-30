/// Meet 字幕の確定判定の回帰テスト
///
/// 議事録の質はここで決まる。同じ発言を二重に書かない・言い終わった文を落とさない、が要件。
import XCTest

@testable import voicekey

final class CaptionSettleTrackerTests: XCTestCase {

    /// 伸びている間は確定しない（書きかけを議事録に載せない）
    func testDoesNotSettleWhileGrowing() {
        var tracker = CaptionSettleTracker(settleSeconds: 2.5)
        let start = Date()
        XCTAssertTrue(tracker.ingest([.init(speaker: "山田", text: "こんに")], now: start).isEmpty)
        XCTAssertTrue(
            tracker.ingest([.init(speaker: "山田", text: "こんにちは")], now: start.addingTimeInterval(1)).isEmpty
        )
        XCTAssertTrue(
            tracker.ingest([.init(speaker: "山田", text: "こんにちは今日は")], now: start.addingTimeInterval(2)).isEmpty
        )
    }

    /// 伸びが止まったら確定する（最後の全文が 1 行になる）
    func testSettlesAfterQuietPeriod() {
        var tracker = CaptionSettleTracker(settleSeconds: 2.5)
        let start = Date()
        _ = tracker.ingest([.init(speaker: "山田", text: "本日の議題は")], now: start)
        _ = tracker.ingest([.init(speaker: "山田", text: "本日の議題は三つです")], now: start.addingTimeInterval(1))
        let settled = tracker.ingest(
            [.init(speaker: "山田", text: "本日の議題は三つです")], now: start.addingTimeInterval(4)
        )
        XCTAssertEqual(settled, [.init(speaker: "山田", text: "本日の議題は三つです")])
    }

    /// 言い終わった字幕が画面に残り続けても、二度は書かない
    ///
    /// Meet は確定した字幕をしばらく表示したままにする。ここを塞がないと、同じ行が
    /// settle のたびに議事録へ並ぶ。
    func testDoesNotEmitTwiceWhileCaptionLingers() {
        var tracker = CaptionSettleTracker(settleSeconds: 2.5)
        let start = Date()
        _ = tracker.ingest([.init(speaker: "山田", text: "一度きり")], now: start)
        let first = tracker.ingest([.init(speaker: "山田", text: "一度きり")], now: start.addingTimeInterval(3))
        XCTAssertEqual(first, [.init(speaker: "山田", text: "一度きり")])

        for offset in [6.0, 9.0, 12.0] {
            let again = tracker.ingest(
                [.init(speaker: "山田", text: "一度きり")], now: start.addingTimeInterval(offset)
            )
            XCTAssertTrue(again.isEmpty, "残り続ける字幕を再確定しないこと（+\(offset)s）")
        }
    }

    /// 同じ人が同じ言葉をもう一度言ったら、次の発言を挟めば書かれる
    ///
    /// 「残っている字幕を無視する」仕組みが、本当の言い直しまで捨てないことの確認。
    func testEmitsRepeatedPhraseAfterAnotherUtterance() {
        var tracker = CaptionSettleTracker(settleSeconds: 2.5)
        let start = Date()
        _ = tracker.ingest([.init(speaker: "山田", text: "はい")], now: start)
        _ = tracker.ingest([.init(speaker: "山田", text: "はい")], now: start.addingTimeInterval(3))
        _ = tracker.ingest([.init(speaker: "山田", text: "では次にいきます")], now: start.addingTimeInterval(4))
        _ = tracker.ingest([.init(speaker: "山田", text: "では次にいきます")], now: start.addingTimeInterval(7))
        let again = tracker.ingest([.init(speaker: "山田", text: "はい")], now: start.addingTimeInterval(8))
        XCTAssertTrue(again.isEmpty, "この時点ではまだ確定待ち")
        let settled = tracker.ingest([.init(speaker: "山田", text: "はい")], now: start.addingTimeInterval(11))
        XCTAssertEqual(settled, [.init(speaker: "山田", text: "はい")])
    }

    /// 別の発言に切り替わったら、前の発言をその場で確定する（settle を待たない）
    func testSettlesImmediatelyOnNewUtterance() {
        var tracker = CaptionSettleTracker(settleSeconds: 2.5)
        let start = Date()
        _ = tracker.ingest([.init(speaker: "山田", text: "前の発言です")], now: start)
        let settled = tracker.ingest(
            [.init(speaker: "山田", text: "次の話に移ります")], now: start.addingTimeInterval(1)
        )
        XCTAssertEqual(settled, [.init(speaker: "山田", text: "前の発言です")])
    }

    /// 話者が違えば別々に追跡する（同時発話でも混ざらない）
    func testTracksSpeakersSeparately() {
        var tracker = CaptionSettleTracker(settleSeconds: 2.5)
        let start = Date()
        _ = tracker.ingest(
            [.init(speaker: "山田", text: "私は賛成です"), .init(speaker: "佐藤", text: "私は反対です")], now: start
        )
        let settled = tracker.ingest(
            [.init(speaker: "山田", text: "私は賛成です"), .init(speaker: "佐藤", text: "私は反対です")],
            now: start.addingTimeInterval(3)
        )
        XCTAssertEqual(settled.count, 2)
        XCTAssertTrue(settled.contains(.init(speaker: "山田", text: "私は賛成です")))
        XCTAssertTrue(settled.contains(.init(speaker: "佐藤", text: "私は反対です")))
    }

    /// 話者名が取れないときは nil（議事録では話者なしの行になる）
    func testMissingSpeakerBecomesNil() {
        var tracker = CaptionSettleTracker(settleSeconds: 2.5)
        let start = Date()
        _ = tracker.ingest([.init(speaker: "", text: "誰かの発言")], now: start)
        let settled = tracker.ingest([.init(speaker: "", text: "誰かの発言")], now: start.addingTimeInterval(3))
        XCTAssertEqual(settled, [.init(speaker: nil, text: "誰かの発言")])
    }

    /// 空白だけの字幕は無視する
    func testIgnoresBlankText() {
        var tracker = CaptionSettleTracker(settleSeconds: 2.5)
        let start = Date()
        XCTAssertTrue(tracker.ingest([.init(speaker: "山田", text: "   ")], now: start).isEmpty)
        XCTAssertTrue(tracker.flush().isEmpty)
    }

    /// 退出時は書きかけも取りこぼさない
    func testFlushEmitsPending() {
        var tracker = CaptionSettleTracker(settleSeconds: 2.5)
        _ = tracker.ingest([.init(speaker: "山田", text: "まだ途中の発言")], now: Date())
        XCTAssertEqual(tracker.flush(), [.init(speaker: "山田", text: "まだ途中の発言")])
        XCTAssertTrue(tracker.flush().isEmpty, "2 回目は空（同じ行を二重に書かない）")
    }
}
