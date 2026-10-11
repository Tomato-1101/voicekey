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

    /// 本当に別の発言に置き換わったら、前の発言をその場で確定する（settle を待たない）
    ///
    /// 書き直し（大文字化・訂正）は同じ発言として扱うので、ここで使うのは共通部分の無い 2 文。
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

    /// 同じ話者の字幕ブロックが 2 つ同時に見えていても、各発言は 1 回しか書かない（B1 の回帰）
    ///
    /// Meet は話者が続けて話すと、言い終わった前のブロックを残したまま次のブロックを足す。
    /// 以前は 2 つのブロックを交互に「別の発言に切り替わった」と判定し、ポーリングのたびに
    /// 2 行ずつ議事録へ重複して書いていた。
    func testTwoBlocksOfSameSpeakerDoNotDuplicate() {
        var tracker = CaptionSettleTracker(settleSeconds: 2.5)
        let start = Date()
        let screen: [CaptionSettleTracker.Entry] = [
            .init(speaker: "山田", text: "前の発言です"), .init(speaker: "山田", text: "次の話に移ります"),
        ]
        var emitted: [CaptionSettleTracker.Line] = []
        for second in 0..<12 {
            emitted += tracker.ingest(screen, now: start.addingTimeInterval(Double(second)))
        }
        emitted += tracker.flush()
        XCTAssertEqual(
            emitted,
            [.init(speaker: "山田", text: "前の発言です"), .init(speaker: "山田", text: "次の話に移ります")]
        )
    }

    /// 確定後に同じブロックが伸びたら、伸びた分だけを書く（同じ文頭を二重に書かない）
    func testGrowthAfterSettleEmitsOnlyTheRest() {
        var tracker = CaptionSettleTracker(settleSeconds: 2.5)
        let start = Date()
        _ = tracker.ingest([.init(speaker: "山田", text: "まず予算です")], now: start)
        let first = tracker.ingest([.init(speaker: "山田", text: "まず予算です")], now: start.addingTimeInterval(3))
        XCTAssertEqual(first, [.init(speaker: "山田", text: "まず予算です")])

        _ = tracker.ingest([.init(speaker: "山田", text: "まず予算です 次に日程です")], now: start.addingTimeInterval(4))
        let second = tracker.ingest(
            [.init(speaker: "山田", text: "まず予算です 次に日程です")], now: start.addingTimeInterval(7)
        )
        XCTAssertEqual(second, [.init(speaker: "山田", text: "次に日程です")])
    }

    /// 退出時は書きかけも取りこぼさない
    func testFlushEmitsPending() {
        var tracker = CaptionSettleTracker(settleSeconds: 2.5)
        _ = tracker.ingest([.init(speaker: "山田", text: "まだ途中の発言")], now: Date())
        XCTAssertEqual(tracker.flush(), [.init(speaker: "山田", text: "まだ途中の発言")])
        XCTAssertTrue(tracker.flush().isEmpty, "2 回目は空（同じ行を二重に書かない）")
    }

    // MARK: - 書き直し（途中版と完成版を二重に書かない）

    /// 英語の字幕が後から大文字化・句読点付きに書き直されても、1 行だけ書く
    func testEnglishCapitalizationIsSameUtterance() {
        var tracker = CaptionSettleTracker(settleSeconds: 2.5)
        let start = Date()
        XCTAssertTrue(tracker.ingest([.init(speaker: "Alice", text: "hello everyone we")], now: start).isEmpty)
        let rewritten = tracker.ingest(
            [.init(speaker: "Alice", text: "Hello everyone, we will start now.")], now: start.addingTimeInterval(1)
        )
        XCTAssertTrue(rewritten.isEmpty, "書き直しで途中版を確定しないこと")
        let settled = tracker.ingest(
            [.init(speaker: "Alice", text: "Hello everyone, we will start now.")], now: start.addingTimeInterval(4)
        )
        XCTAssertEqual(settled, [.init(speaker: "Alice", text: "Hello everyone, we will start now.")])
    }

    /// 日本語の字幕が途中で訂正されても（「は」→「の」）、完成版の 1 行だけ書く
    func testJapaneseCorrectionIsSameUtterance() {
        var tracker = CaptionSettleTracker(settleSeconds: 2.5)
        let start = Date()
        _ = tracker.ingest([.init(speaker: "山田", text: "今日は会議を始めま")], now: start)
        let rewritten = tracker.ingest(
            [.init(speaker: "山田", text: "今日の会議を始めます")], now: start.addingTimeInterval(1)
        )
        XCTAssertTrue(rewritten.isEmpty, "訂正で途中版を確定しないこと")
        let settled = tracker.ingest(
            [.init(speaker: "山田", text: "今日の会議を始めます")], now: start.addingTimeInterval(4)
        )
        XCTAssertEqual(settled, [.init(speaker: "山田", text: "今日の会議を始めます")])
    }

    /// 共通部分の無い文は別の発言として両方書く（書き直し判定が広すぎないこと）
    func testDifferentUtterancesAreBothWritten() {
        XCTAssertFalse(CaptionSettleTracker.isRevision(old: "前の発言です", new: "次の話に移ります"))
        XCTAssertFalse(CaptionSettleTracker.isRevision(old: "thanks everyone", new: "next item is budget"))
        XCTAssertTrue(CaptionSettleTracker.isRevision(old: "hello everyone we", new: "Hello everyone, we will"))

        var tracker = CaptionSettleTracker(settleSeconds: 2.5)
        let start = Date()
        _ = tracker.ingest([.init(speaker: "Alice", text: "thanks everyone")], now: start)
        let first = tracker.ingest([.init(speaker: "Alice", text: "next item is budget")], now: start.addingTimeInterval(1))
        XCTAssertEqual(first, [.init(speaker: "Alice", text: "thanks everyone")])
        let second = tracker.ingest([.init(speaker: "Alice", text: "next item is budget")], now: start.addingTimeInterval(4))
        XCTAssertEqual(second, [.init(speaker: "Alice", text: "next item is budget")])
    }

    /// 確定した後で同じブロックが伸びたら、伸びた部分だけを書く（先頭からの全文を繰り返さない）
    func testGrowthAfterSettleWritesOnlyContinuation() {
        var tracker = CaptionSettleTracker(settleSeconds: 2.5)
        let start = Date()
        _ = tracker.ingest([.init(speaker: "山田", text: "本日の議題は")], now: start)
        XCTAssertEqual(
            tracker.ingest([.init(speaker: "山田", text: "本日の議題は")], now: start.addingTimeInterval(3)),
            [.init(speaker: "山田", text: "本日の議題は")]
        )
        _ = tracker.ingest([.init(speaker: "山田", text: "本日の議題は三つです")], now: start.addingTimeInterval(4))
        let settled = tracker.ingest(
            [.init(speaker: "山田", text: "本日の議題は三つです")], now: start.addingTimeInterval(7)
        )
        XCTAssertEqual(settled, [.init(speaker: "山田", text: "三つです")])
    }

    /// 話者が替わっても、前の話者の発言は伸びが止まった時点で確定する（取りこぼさない）
    func testPreviousSpeakerSettlesAfterSpeakerChange() {
        var tracker = CaptionSettleTracker(settleSeconds: 2.5)
        let start = Date()
        _ = tracker.ingest([.init(speaker: "山田", text: "以上です")], now: start)
        let both = [CaptionSettleTracker.Entry(speaker: "山田", text: "以上です"),
                    CaptionSettleTracker.Entry(speaker: "佐藤", text: "ありがとうございます")]
        XCTAssertTrue(tracker.ingest(both, now: start.addingTimeInterval(1)).isEmpty)
        let settled = tracker.ingest(both, now: start.addingTimeInterval(4))
        XCTAssertEqual(settled, [.init(speaker: "山田", text: "以上です"), .init(speaker: "佐藤", text: "ありがとうございます")])
    }

    // MARK: - 代替経路（字幕欄まるごと）

    /// 字幕欄の全文が伸びても、前回確定した分から増えた末尾だけを書く
    func testRegionWritesOnlyNewTail() {
        var tracker = CaptionSettleTracker(settleSeconds: 2.5)
        let start = Date()
        XCTAssertTrue(tracker.ingestRegion("Good morning everyone.", now: start).isEmpty)
        XCTAssertEqual(
            tracker.ingestRegion("Good morning everyone.", now: start.addingTimeInterval(3)),
            [.init(speaker: nil, text: "Good morning everyone.")]
        )
        let grown = "Good morning everyone. Let's begin with the budget."
        XCTAssertTrue(tracker.ingestRegion(grown, now: start.addingTimeInterval(4)).isEmpty)
        XCTAssertEqual(
            tracker.ingestRegion(grown, now: start.addingTimeInterval(7)),
            [.init(speaker: nil, text: "Let's begin with the budget")]
        )
        // 同じ全文が残り続けても二度は書かない
        XCTAssertTrue(tracker.ingestRegion(grown, now: start.addingTimeInterval(10)).isEmpty)
    }

    /// 古い行が上へ押し出されて消えても、新しい末尾だけを書く
    func testRegionHandlesScrolledOffHead() {
        XCTAssertEqual(
            CaptionSettleTracker.newTail(of: "second line. third line", after: "first line. second line."),
            "third line"
        )
        XCTAssertEqual(CaptionSettleTracker.newTail(of: "全く別の内容", after: "first line"), "全く別の内容")
        XCTAssertEqual(CaptionSettleTracker.newTail(of: "最初の字幕", after: nil), "最初の字幕")
    }

    /// 字幕欄が伸び続けても、一定時間で確定して書き始める（終わるまで何も残らない、を防ぐ）
    func testRegionForcesSettleWhileContinuouslyChanging() {
        var tracker = CaptionSettleTracker(settleSeconds: 2.5)
        let start = Date()
        var text = "a"
        var emitted: [CaptionSettleTracker.Line] = []
        for second in 0...16 {
            text += " word\(second)"
            emitted += tracker.ingestRegion(text, now: start.addingTimeInterval(Double(second)))
        }
        XCTAssertEqual(emitted.count, 1, "15 秒で 1 回確定すること")
    }

    /// 退出時の吐き出しで、代替経路の確定待ちも書く
    func testFlushIncludesRegionPending() {
        var tracker = CaptionSettleTracker(settleSeconds: 2.5)
        _ = tracker.ingestRegion("最後の発言", now: Date())
        XCTAssertEqual(tracker.flush(), [.init(speaker: nil, text: "最後の発言")])
    }
}
