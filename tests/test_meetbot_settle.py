"""
Meet 字幕の確定判定のテスト

議事録の質はここで決まる。同じ発言を二重に書かない・言い終わった文を落とさない、が要件。
Mac 版の `CaptionSettleTrackerTests.swift` と同じ例を通す（同じ字幕から同じ議事録になることの確認）。
"""

import unittest

from src.meetbot.settle import CaptionSettleTracker, Line, is_revision, new_tail


class CaptionSettleTrackerTests(unittest.TestCase):
    """話者ごとの字幕ブロックの確定判定。"""

    def setUp(self):
        self.tracker = CaptionSettleTracker(settle_seconds=2.5)

    def feed(self, text, at, speaker="山田"):
        """1 人の字幕を 1 回分取り込む。"""
        return self.tracker.ingest([(speaker, text)], at)

    def test_does_not_settle_while_growing(self):
        """伸びている間は確定しない（書きかけを議事録に載せない）。"""
        self.assertEqual(self.feed("こんに", 0.0), [])
        self.assertEqual(self.feed("こんにちは", 1.0), [])
        self.assertEqual(self.feed("こんにちは今日は", 2.0), [])

    def test_settles_after_quiet_period(self):
        """伸びが止まったら、最後の全文が 1 行になる。"""
        self.feed("本日の議題は", 0.0)
        self.feed("本日の議題は三つです", 1.0)
        self.assertEqual(self.feed("本日の議題は三つです", 4.0), [Line("山田", "本日の議題は三つです")])

    def test_does_not_emit_twice_while_caption_lingers(self):
        """言い終わった字幕が画面に残り続けても、二度は書かない。"""
        self.feed("一度きり", 0.0)
        self.assertEqual(self.feed("一度きり", 3.0), [Line("山田", "一度きり")])
        for at in (6.0, 9.0, 12.0):
            self.assertEqual(self.feed("一度きり", at), [])

    def test_emits_repeated_phrase_after_another_utterance(self):
        """同じ人が同じ言葉をもう一度言ったら、次の発言を挟めば書かれる。"""
        self.feed("はい", 0.0)
        self.feed("はい", 3.0)
        self.feed("では次にいきます", 4.0)
        self.feed("では次にいきます", 7.0)
        self.assertEqual(self.feed("はい", 8.0), [])
        self.assertEqual(self.feed("はい", 11.0), [Line("山田", "はい")])

    def test_settles_immediately_on_new_utterance(self):
        """別の発言に置き換わったら、前の発言をその場で確定する。"""
        self.feed("前の発言です", 0.0)
        self.assertEqual(self.feed("次の話に移ります", 1.0), [Line("山田", "前の発言です")])

    def test_tracks_speakers_separately(self):
        """話者が違えば別々に追跡する。"""
        screen = [("山田", "私は賛成です"), ("佐藤", "私は反対です")]
        self.tracker.ingest(screen, 0.0)
        settled = self.tracker.ingest(screen, 3.0)
        self.assertCountEqual(settled, [Line("山田", "私は賛成です"), Line("佐藤", "私は反対です")])

    def test_missing_speaker_becomes_none(self):
        """話者名が取れないときは None（議事録では話者なしの行になる）。"""
        self.feed("誰かの発言", 0.0, speaker="")
        self.assertEqual(self.feed("誰かの発言", 3.0, speaker=""), [Line(None, "誰かの発言")])

    def test_ignores_blank_text(self):
        """空白だけの字幕は無視する。"""
        self.assertEqual(self.feed("   ", 0.0), [])
        self.assertEqual(self.tracker.flush(), [])

    def test_two_blocks_of_same_speaker_do_not_duplicate(self):
        """同じ話者のブロックが 2 つ同時に見えていても、各発言は 1 回しか書かない。"""
        screen = [("山田", "前の発言です"), ("山田", "次の話に移ります")]
        emitted = []
        for second in range(12):
            emitted += self.tracker.ingest(screen, float(second))
        emitted += self.tracker.flush()
        self.assertEqual(emitted, [Line("山田", "前の発言です"), Line("山田", "次の話に移ります")])

    def test_growth_after_settle_emits_only_the_rest(self):
        """確定後に同じブロックが伸びたら、伸びた分だけを書く。"""
        self.feed("まず予算です", 0.0)
        self.assertEqual(self.feed("まず予算です", 3.0), [Line("山田", "まず予算です")])
        self.feed("まず予算です 次に日程です", 4.0)
        self.assertEqual(self.feed("まず予算です 次に日程です", 7.0), [Line("山田", "次に日程です")])

    def test_growth_after_settle_writes_only_continuation(self):
        """区切りの無い続きでも、伸びた部分だけを書く。"""
        self.feed("本日の議題は", 0.0)
        self.assertEqual(self.feed("本日の議題は", 3.0), [Line("山田", "本日の議題は")])
        self.feed("本日の議題は三つです", 4.0)
        self.assertEqual(self.feed("本日の議題は三つです", 7.0), [Line("山田", "三つです")])

    def test_flush_emits_pending(self):
        """退出時は書きかけも取りこぼさない（2 回目は空）。"""
        self.feed("まだ途中の発言", 0.0)
        self.assertEqual(self.tracker.flush(), [Line("山田", "まだ途中の発言")])
        self.assertEqual(self.tracker.flush(), [])

    def test_english_capitalization_is_same_utterance(self):
        """英語の字幕が後から大文字化・句読点付きに書き直されても、1 行だけ書く。"""
        self.assertEqual(self.feed("hello everyone we", 0.0, "Alice"), [])
        final = "Hello everyone, we will start now."
        self.assertEqual(self.feed(final, 1.0, "Alice"), [])
        self.assertEqual(self.feed(final, 4.0, "Alice"), [Line("Alice", final)])

    def test_japanese_correction_is_same_utterance(self):
        """日本語の字幕が途中で訂正されても、完成版の 1 行だけ書く。"""
        self.feed("今日は会議を始めま", 0.0)
        self.assertEqual(self.feed("今日の会議を始めます", 1.0), [])
        self.assertEqual(self.feed("今日の会議を始めます", 4.0), [Line("山田", "今日の会議を始めます")])

    def test_different_utterances_are_both_written(self):
        """共通部分の無い文は別の発言として両方書く（書き直し判定が広すぎないこと）。"""
        self.assertFalse(is_revision("前の発言です", "次の話に移ります"))
        self.assertFalse(is_revision("thanks everyone", "next item is budget"))
        self.assertTrue(is_revision("hello everyone we", "Hello everyone, we will"))
        self.feed("thanks everyone", 0.0, "Alice")
        self.assertEqual(self.feed("next item is budget", 1.0, "Alice"), [Line("Alice", "thanks everyone")])
        self.assertEqual(self.feed("next item is budget", 4.0, "Alice"), [Line("Alice", "next item is budget")])

    def test_previous_speaker_settles_after_speaker_change(self):
        """話者が替わっても、前の話者の発言は伸びが止まった時点で確定する。"""
        self.feed("以上です", 0.0)
        both = [("山田", "以上です"), ("佐藤", "ありがとうございます")]
        self.assertEqual(self.tracker.ingest(both, 1.0), [])
        self.assertEqual(
            self.tracker.ingest(both, 4.0), [Line("山田", "以上です"), Line("佐藤", "ありがとうございます")],
        )


class CaptionRegionTests(unittest.TestCase):
    """代替経路（字幕欄まるごと）の確定判定。"""

    def setUp(self):
        self.tracker = CaptionSettleTracker(settle_seconds=2.5)

    def test_region_writes_only_new_tail(self):
        """字幕欄の全文が伸びても、前回確定した分から増えた末尾だけを書く。"""
        self.assertEqual(self.tracker.ingest_region("Good morning everyone.", 0.0), [])
        self.assertEqual(
            self.tracker.ingest_region("Good morning everyone.", 3.0), [Line(None, "Good morning everyone.")],
        )
        grown = "Good morning everyone. Let's begin with the budget."
        self.assertEqual(self.tracker.ingest_region(grown, 4.0), [])
        self.assertEqual(self.tracker.ingest_region(grown, 7.0), [Line(None, "Let's begin with the budget")])
        self.assertEqual(self.tracker.ingest_region(grown, 10.0), [])

    def test_region_handles_scrolled_off_head(self):
        """古い行が上へ押し出されて消えても、新しい末尾だけを書く。"""
        self.assertEqual(new_tail("second line. third line", "first line. second line."), "third line")
        self.assertEqual(new_tail("全く別の内容", "first line"), "全く別の内容")
        self.assertEqual(new_tail("最初の字幕", None), "最初の字幕")

    def test_region_forces_settle_while_continuously_changing(self):
        """字幕欄が伸び続けても、一定時間で確定して書き始める。"""
        text = "a"
        emitted = []
        for second in range(17):
            text += f" word{second}"
            emitted += self.tracker.ingest_region(text, float(second))
        self.assertEqual(len(emitted), 1)

    def test_flush_includes_region_pending(self):
        """退出時の吐き出しで、代替経路の確定待ちも書く。"""
        self.tracker.ingest_region("最後の発言", 0.0)
        self.assertEqual(self.tracker.flush(), [Line(None, "最後の発言")])


if __name__ == "__main__":
    unittest.main()
