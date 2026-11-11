"""dictation_timeline（音声入力 1 回分の `[計測]` 行）のテスト。

行の書式は Mac 版 DictationTimeline.summaryLine() と同じ並びにしてある。
集計や grep が位置に頼るので、項目の順番・名前・値の書き方が変わったらここで気づけるようにする。
"""

import unittest

from src.core import dictation_timeline
from src.core.dictation_timeline import DictationTimeline, route


class TestRoute(unittest.TestCase):
    """文字起こしの経路の分類。"""

    def test_streaming_when_live_text_arrived(self):
        self.assertEqual(route("soniox", True, True), "streaming")
        self.assertEqual(route("openai_live", True, True), "streaming")

    def test_soniox_without_live_text_is_replay(self):
        """Soniox はライブが空でも接続が無くても、録音全体の送り直しになる。"""
        self.assertEqual(route("soniox", True, False), "replay")
        self.assertEqual(route("soniox", False, False), "replay")

    def test_other_live_engine_falls_back(self):
        self.assertEqual(route("openai_live", True, False), "fallback")

    def test_plain_rest(self):
        self.assertEqual(route("groq", False, False), "rest")


class TestSummaryLine(unittest.TestCase):
    """`[計測]` の 1 行。"""

    def test_full_line(self):
        line = DictationTimeline(
            generation=7,
            slot=1,
            backend="groq",
            model="whisper-large-v3-turbo",
            pressed_at=10.0,
            route="rest",
            outcome=dictation_timeline.OUTCOME_PASTED,
            record_started_at=10.05,
            recording_sec=2.5,
            released_at=12.5,
            stopped_at=12.75,
            finalized_at=13.0,
            stt_ms=200.4,
            vad_ms=30.0,
            format_ms=150.0,
            translate_ms=None,
            paste_ms=60.0,
            pasted_at=13.25,
            characters=12,
            target="chrome.exe",
            auto_enter=True,
        ).summary_line()
        self.assertEqual(
            line,
            "[計測] gen=7 slot=1 backend=groq model=whisper-large-v3-turbo 経路=rest 終わり方=- "
            "結果=貼付 押下→録音開始=50ms 押下→最初の音声=- 録音=2.50s 離鍵→確定=500ms "
            "離鍵→停止=250ms 停止→確定=250ms STT=200ms VAD=30ms 整形=150ms 翻訳=- 貼付=60ms "
            "離鍵→貼付完了=750ms 押下→貼付完了=3250ms 文字数=12 貼付先=chrome.exe auto_enter=true",
        )

    def test_missing_values_are_dashes(self):
        """取れなかった項目は 0 ではなく `-`（0ms だったと読めてしまうため）。"""
        line = DictationTimeline(slot=2, backend="soniox").summary_line()
        self.assertEqual(
            line,
            "[計測] gen=- slot=2 backend=soniox model=- 経路=- 終わり方=- 結果=未完了 "
            "押下→録音開始=- 押下→最初の音声=- 録音=- 離鍵→確定=- 離鍵→停止=- 停止→確定=- "
            "STT=- VAD=- 整形=- 翻訳=- 貼付=- 離鍵→貼付完了=- 押下→貼付完了=- 文字数=- "
            "貼付先=- auto_enter=false",
        )


if __name__ == "__main__":
    unittest.main()
