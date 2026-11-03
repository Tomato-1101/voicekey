"""音声入力の流れ（app.py）に Mac から持ってきた振る舞いのテスト。

VoicekeyApp 全体は作らず、SimpleNamespace のダミー self で該当メソッドだけを呼ぶ
（test_double_tap_release などと同じやり方）。ネットワーク・キー・音声デバイスには触れない。
"""

import queue
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

import numpy as np

from src.app import TaskContext, VoicekeyApp
from src.config.types import TranscriptionTask
from src.core import diagnostics
from src.core import live_transcriber as lt
from src.core.api_transcriber import TranscriptionError


class _Signal:
    def __init__(self):
        self.calls = []

    def emit(self, *args):
        self.calls.append(args)


class TestTranslateOnceKey(unittest.TestCase):
    def _app(self, translate_keys, handsfree=(), pressed=()):
        app = SimpleNamespace(
            _translate_once_keys=set(translate_keys),
            _handsfree_keys=set(handsfree),
            _pressed_keys=set(pressed),
            _acceptable_names=VoicekeyApp._acceptable_names,
        )
        app._keys_pressed = lambda keys: VoicekeyApp._keys_pressed(app, keys)
        app._translate_once_covered = lambda slot: VoicekeyApp._translate_once_covered(app, slot)
        return app

    def test_pressed_with_record_key(self):
        app = self._app({"shift_r"}, pressed={"f2", "shift_r"})
        slot = SimpleNamespace(required_keys={"f2"})
        self.assertTrue(VoicekeyApp._translate_once_requested(app, slot))

    def test_not_pressed(self):
        app = self._app({"shift_r"}, pressed={"f2"})
        self.assertFalse(VoicekeyApp._translate_once_requested(app, SimpleNamespace(required_keys={"f2"})))

    def test_covered_by_record_or_handsfree_key_is_ignored(self):
        # 1 回翻訳キーが録音キーに含まれると録音のたびに翻訳になってしまうので効かせない
        app = self._app({"shift_r"}, pressed={"shift_r"})
        self.assertFalse(VoicekeyApp._translate_once_requested(app, SimpleNamespace(required_keys={"shift_r"})))
        app = self._app({"shift_r"}, handsfree={"shift"}, pressed={"f2", "shift_r"})
        self.assertFalse(VoicekeyApp._translate_once_requested(app, SimpleNamespace(required_keys={"f2"})))

    def test_toggle_keeps_always_on(self):
        ctx = SimpleNamespace(translate=False)
        app = SimpleNamespace(
            _config={"translate_input_enabled": False, "translate_input_target": "en"},
            _state_lock=threading.Lock(),
            _active_context=ctx,
            translate_badge=_Signal(),
        )
        app._translate_badge_text = lambda: VoicekeyApp._translate_badge_text(app)
        VoicekeyApp._toggle_translate_once(app)
        self.assertTrue(ctx.translate)
        self.assertEqual(app.translate_badge.calls[-1], ("→EN",))
        VoicekeyApp._toggle_translate_once(app)
        self.assertFalse(ctx.translate)
        # 常に翻訳の設定なら、キーでは外さない
        app._config["translate_input_enabled"] = True
        ctx.translate = True
        VoicekeyApp._toggle_translate_once(app)
        self.assertTrue(ctx.translate)


class TestRepaste(unittest.TestCase):
    def test_nothing_yet_notifies(self):
        app = SimpleNamespace(_last_inserted_text=None, notice=_Signal())
        VoicekeyApp._repaste_last(app)
        self.assertEqual(len(app.notice.calls), 1)

    def test_repastes_last_text(self):
        done = threading.Event()
        handler = mock.Mock()
        handler.insert_text.side_effect = lambda t: done.set()
        app = SimpleNamespace(_last_inserted_text="前の入力", notice=_Signal(),
                              _pressed_keys=set(), _input_handler=handler)
        VoicekeyApp._repaste_last(app)
        self.assertTrue(done.wait(2))
        handler.insert_text.assert_called_once_with("前の入力")


def _ctx(backend="soniox", transcriber=None, timeline=True):
    slot = SimpleNamespace(
        backend=backend, api_model="m", api_prompt="", format_enabled=False,
        transcriber=transcriber or mock.Mock(),
    )
    tl = diagnostics.DictationTimeline(1, 1, backend, "m", pressed_at=0.0) if timeline else None
    return TaskContext(
        slot=slot, language="ja", vad_on=False, split_on=False, volume_normalize=False,
        format_model="x", format_auto_prompt="", server_format=False, timeline=tl,
    )


class _Streamer:
    def __init__(self, text, ending):
        self._text = text
        self.ending = ending

    def finish(self):
        return self._text


class TestStreamingEndings(unittest.TestCase):
    def _app(self):
        app = SimpleNamespace(notice=_Signal(), _insert=[], _stats=mock.Mock())
        app._maybe_format = lambda text, ctx: text
        app._record_stats = lambda text, audio: None

        def insert(text, auto_enter, ctx=None):
            app._insert.append(text)
            return True

        app._insert_and_enter = insert
        return app

    def _task(self, streamer, ctx):
        return TranscriptionTask(
            audio_data=np.zeros(16000, dtype=np.float32), slot_id=1, timestamp=0.0,
            streamer=streamer, context=ctx,
        )

    def test_completed_empty_does_not_fall_back(self):
        app = self._app()
        transcriber = mock.Mock()
        ctx = _ctx(transcriber=transcriber)
        VoicekeyApp._process_task(app, self._task(_Streamer("", lt.ENDING_COMPLETED), ctx))
        transcriber.transcribe.assert_not_called()  # 本当に無言なら再送しない
        self.assertEqual(ctx.timeline.outcome, diagnostics.Outcome.LIVE_EMPTY_COMPLETED)
        self.assertEqual(app._insert, [])

    def test_partial_failure_replays_whole_audio(self):
        app = self._app()
        transcriber = mock.Mock()
        transcriber.transcribe.return_value = "全文"
        ctx = _ctx(transcriber=transcriber)
        VoicekeyApp._process_task(app, self._task(_Streamer("前半", lt.failed("disconnect")), ctx))
        self.assertEqual(app._insert, ["全文"])
        self.assertEqual(app.notice.calls, [])  # 再送で救えたら知らせない

    def test_partial_failure_without_replay_notifies(self):
        app = self._app()
        transcriber = mock.Mock()
        transcriber.transcribe.return_value = ""
        ctx = _ctx(transcriber=transcriber)
        VoicekeyApp._process_task(app, self._task(_Streamer("前半", lt.failed("error", "500")), ctx))
        self.assertEqual(app._insert, ["前半"])
        self.assertIn("途中まで", app.notice.calls[-1][0])

    def test_timeout_partial_is_used_silently(self):
        app = self._app()
        transcriber = mock.Mock()
        ctx = _ctx(transcriber=transcriber)
        VoicekeyApp._process_task(app, self._task(_Streamer("ほぼ全部", lt.failed("timeout")), ctx))
        transcriber.transcribe.assert_not_called()
        self.assertEqual(app._insert, ["ほぼ全部"])
        self.assertEqual(app.notice.calls, [])


class TestWorkerNeedsApiKey(unittest.TestCase):
    def test_opens_api_keys_once_and_emits_timeline(self):
        q = queue.Queue()
        ctx = _ctx()
        err = TranscriptionError("Soniox の API キーが未設定です", needs_api_key=True)
        app = SimpleNamespace(
            _task_q=q, _state_lock=threading.Lock(), _outstanding=2,
            notice=_Signal(), api_keys_requested=_Signal(),
            _api_key_prompt_opened=False,
            _emit_state=lambda: None, _refresh_entitlement_async=lambda: None,
        )
        app._process_task = mock.Mock(side_effect=err)
        task = TranscriptionTask(audio_data=np.zeros(10), slot_id=1, timestamp=0.0, context=ctx)
        q.put(task)
        q.put(task)
        q.put(None)
        with self.assertLogs("src.core.diagnostics", level="INFO") as logs:
            VoicekeyApp._worker_loop(app)
        # 設定画面を開くのは起動中 1 回だけ。通知は毎回
        self.assertEqual(len(app.api_keys_requested.calls), 1)
        self.assertEqual(len(app.notice.calls), 2)
        self.assertTrue(any("[計測]" in line and "結果=文字起こし失敗" in line for line in logs.output))


class TestTimelineLine(unittest.TestCase):
    def test_summary_line_order_and_no_text(self):
        tl = diagnostics.DictationTimeline(5, 2, "soniox", "stt-rt-v5", pressed_at=10.0)
        tl.record_started_at = 10.05
        tl.released_at = 12.0
        tl.finalized_at = 12.2
        tl.pasted_at = 12.3
        tl.characters = 12
        tl.target_app = "chrome.exe"
        tl.outcome = diagnostics.Outcome.PASTED
        line = tl.summary_line()
        self.assertTrue(line.startswith("[計測] gen=5 slot=2 backend=soniox model=stt-rt-v5"))
        self.assertIn("押下→録音開始=50ms", line)
        self.assertIn("離鍵→確定=200ms", line)
        self.assertIn("離鍵→貼付完了=300ms", line)
        self.assertIn("貼付先=chrome.exe", line)
        self.assertIn("結果=貼付", line)

    def test_route(self):
        self.assertEqual(diagnostics.route_for("soniox", True, True), "streaming")
        self.assertEqual(diagnostics.route_for("soniox", True, False), "replay")
        self.assertEqual(diagnostics.route_for("deepgram", True, False), "fallback")
        self.assertEqual(diagnostics.route_for("groq", False, False), "rest")


class TestInsertRecordsHistoryWithApp(unittest.TestCase):
    def test_history_gets_app_and_last_text(self):
        history = mock.Mock()
        handler = mock.Mock()
        handler.insert_text.return_value = True
        app = SimpleNamespace(
            _config={"history_enabled": True, "replacements": [], "numeral_normalize_enabled": False},
            _history=history, _input_handler=handler, notice=_Signal(),
            _platform=SimpleNamespace(foreground_app=lambda: ("chrome.exe", "Google Chrome")),
            _last_inserted_text=None,
        )
        app._apply_replacements = lambda t: t
        app._maybe_translate = lambda t, ctx: t
        ctx = _ctx()
        self.assertTrue(VoicekeyApp._insert_and_enter(app, "こんにちは", False, ctx))
        history.add.assert_called_once_with("こんにちは", app_id="chrome.exe", app_name="Google Chrome")
        self.assertEqual(app._last_inserted_text, "こんにちは")
        self.assertEqual(ctx.timeline.target_app, "chrome.exe")
        self.assertEqual(ctx.timeline.characters, 5)

    def test_history_off_and_paste_failure(self):
        history = mock.Mock()
        handler = mock.Mock()
        handler.insert_text.return_value = False
        app = SimpleNamespace(
            _config={"history_enabled": False, "numeral_normalize_enabled": False},
            _history=history, _input_handler=handler, notice=_Signal(),
            _platform=SimpleNamespace(foreground_app=lambda: (None, None)),
            _last_inserted_text=None,
        )
        app._apply_replacements = lambda t: t
        app._maybe_translate = lambda t, ctx: t
        self.assertFalse(VoicekeyApp._insert_and_enter(app, "x", True, _ctx()))
        history.add.assert_not_called()
        handler.press_enter.assert_not_called()  # 貼れていないのに Enter を送らない
        self.assertIn("Ctrl+V", app.notice.calls[-1][0])



class TestWindowsKeyNormalize(unittest.TestCase):
    def test_ctrl_letter_control_char_maps_to_letter(self):
        from src.platform.windows.adapter import WindowsPlatformAdapter

        adapter = WindowsPlatformAdapter()
        # Ctrl を押しながら V → pynput は char='\x16'（制御文字）で届ける
        self.assertEqual(adapter.normalize_listener_key(SimpleNamespace(vk=0x56, char="\x16")), "v")
        self.assertEqual(adapter.normalize_listener_key(SimpleNamespace(vk=0x41, char="a")), "a")
        self.assertEqual(adapter.normalize_listener_key(SimpleNamespace(vk=0x31, char=None)), "1")

    def test_foreground_app_off_windows(self):
        from src.platform.windows import foreground

        with mock.patch.object(foreground.sys, "platform", "linux"):
            self.assertEqual(foreground.foreground_app(), (None, None))


if __name__ == "__main__":
    unittest.main()
