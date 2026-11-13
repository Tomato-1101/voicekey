"""音声入力まわりの追加機能（app.py の配線）のテスト。

対象:
- API 料金の記録（文字起こし・整形・翻訳。貼り付けの後に書く）
- `[計測]` の 1 行
- 貼り付け失敗時の通知と Enter の抑止
- 翻訳して入力（常に翻訳 / 1 回翻訳キー）
- 再貼り付けキー
- 貼り付け先アプリ（アプリ別使用状況・HUD へ渡すアイコンの元）
- リスナーが止まったときの離鍵の扱い

VoicekeyApp は QObject 派生で素の生成ができないため、対象メソッドが参照する属性だけを持つ
SimpleNamespace を self に見立て、未束縛メソッドとして呼び出す（既存テストと同じ流儀）。
外部への通信は一切行わない（文字起こし・整形・翻訳はすべて偽物に差し替える）。
"""

import threading
import unittest
from types import SimpleNamespace
from unittest import mock

import numpy as np

from src import app as app_mod
from src.app import (
    ENTER_FAILED_NOTICE,
    PARTIAL_INPUT_NOTICE,
    PASTE_FAILED_NOTICE,
    HotkeySlot,
    TaskContext,
    VoicekeyApp,
    _flush_usage,
)
from src.config import HotkeyMode
from src.config.constants import DEFAULT_CONFIG, SAMPLE_RATE
from src.config.types import TranscriptionTask
from src.core import TranscriptionError
from src.core import dictation_translator
from src.core.streaming_transcriber import SonioxTranscriber, resolve_replay


class _FakeTranscriber:
    """transcribe の結果（または例外）を返すだけの偽クライアント。"""

    allows_split = True
    engine_id = "groq"
    model = "whisper-large-v3-turbo"

    def __init__(self, result="結果", error=None):
        self.result = result
        self.error = error
        self.calls = 0

    def transcribe(self, audio):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result


class _FakeStreamer:
    """録音しながら送る接続の偽物。"""

    engine_id = "soniox"
    model = "stt-rt-v5"

    def __init__(self, text, sent_seconds=2.0, failed=False):
        self._text = text
        self.sent_seconds = sent_seconds
        self.failed = failed
        self.partial_text = ""

    def finish(self):
        return self._text


class _FakeConfig:
    """設定の偽物（無いキーは既定値を返す）。"""

    def __init__(self, values=None):
        self._values = values or {}

    def get(self, key, default=None):
        return self._values.get(key, default)


def _slot(transcriber, backend="groq", format_enabled=False, hotkey="<f2>") -> HotkeySlot:
    return HotkeySlot(
        slot_id=1,
        hotkey=hotkey,
        hotkey_mode="hold",
        required_keys=VoicekeyApp._parse_hotkey(hotkey),
        backend=backend,
        api_model="whisper-large-v3-turbo",
        api_prompt="",
        format_enabled=format_enabled,
        format_preset="standard",
        transcriber=transcriber,
    )


def _ctx(slot, **overrides) -> TaskContext:
    """VAD・分割・正規化を切った（録音後にまとめて 1 本で送る）コンテキスト。"""
    values = dict(
        slot=slot, language="ja", vad_on=False, split_on=False, volume_normalize=False,
        format_model="fmt-model", format_auto_prompt="",
    )
    values.update(overrides)
    return TaskContext(**values)


def _task(ctx, seconds=1.0, auto_enter=False, streamer=None) -> TranscriptionTask:
    return TranscriptionTask(
        audio_data=np.zeros(int(SAMPLE_RATE * seconds), dtype=np.float32),
        slot_id=1, timestamp=0.0, auto_enter=auto_enter, streamer=streamer, context=ctx,
    )


def _worker_self(pasted=True, enter_ok=True, config=None):
    """_process_task を本物の処理で最後まで通すための偽 self。

    呼ばれた順を events に残す（料金の記録が貼り付けの後かを確かめるため）。
    """
    events = []
    handler = mock.Mock()
    handler.insert_text.side_effect = lambda text: (events.append("paste"), pasted)[1]
    handler.press_enter.side_effect = lambda: (events.append("enter"), enter_ok)[1]
    usage = mock.Mock()
    usage.record_stt.side_effect = lambda *a, **k: events.append("record_stt")
    usage.record_chat_usage.side_effect = lambda *a, **k: events.append("record_chat")
    stats = mock.Mock()
    stats.record_session.side_effect = lambda *a, **k: events.append("stats")
    s = SimpleNamespace(
        _slots={},
        _config=_FakeConfig(dict({"auto_enter_delay_ms": 0}, **(config or {}))),
        _history=mock.Mock(),
        _input_handler=handler,
        _api_usage=usage,
        _stats=stats,
        _vad=mock.Mock(),
        notice=mock.Mock(),
        events=events,
    )
    for name in (
        "_run_task", "_maybe_format", "_maybe_translate", "_insert_and_enter",
        "_record_stats", "_apply_replacements", "_transcribe_parallel",
    ):
        setattr(s, name, getattr(VoicekeyApp, name).__get__(s))
    return s


class TestApiUsageRecording(unittest.TestCase):
    """API 料金の記録。"""

    def test_rest_transcription_recorded_after_paste(self):
        s = _worker_self()
        ctx = _ctx(_slot(_FakeTranscriber("こんにちは")))
        VoicekeyApp._process_task(s, _task(ctx, seconds=2.0))
        s._api_usage.record_stt.assert_called_once_with("groq", "whisper-large-v3-turbo", 2.0)
        # 記録はファイル保存を伴うので、貼り付けより後に書く
        self.assertLess(s.events.index("paste"), s.events.index("record_stt"))

    def test_formatting_usage_recorded(self):
        s = _worker_self()
        ctx = _ctx(_slot(_FakeTranscriber("なま"), format_enabled=True))
        response = {"usage": {"prompt_tokens": 3}}

        def fake_format(text, model, prompt="", preset="standard", on_usage=None):
            on_usage(response)
            return "整形済み"

        with mock.patch.object(app_mod.text_formatter, "format_text", fake_format):
            VoicekeyApp._process_task(s, _task(ctx))
        s._input_handler.insert_text.assert_called_once_with("整形済み")
        s._api_usage.record_chat_usage.assert_called_once_with(
            response, "groq", "fmt-model", "formatting"
        )
        self.assertLess(s.events.index("paste"), s.events.index("record_chat"))

    def test_streaming_recorded_by_sent_seconds(self):
        """ライブ接続は送った音声の長さで 1 回だけ記録する（エンジンとモデルは接続のもの）。"""
        s = _worker_self()
        transcriber = _FakeTranscriber("使われない")
        ctx = _ctx(_slot(transcriber, backend="soniox"))
        VoicekeyApp._process_task(s, _task(ctx, streamer=_FakeStreamer("ライブ", sent_seconds=3.5)))
        self.assertEqual(transcriber.calls, 0)
        s._api_usage.record_stt.assert_called_once_with("soniox", "stt-rt-v5", 3.5)
        s._input_handler.insert_text.assert_called_once_with("ライブ")

    def test_streaming_empty_then_resend_records_both(self):
        """ライブの確定が空で録音全体を送り直した回は、両方とも課金されている。"""
        s = _worker_self()
        ctx = _ctx(_slot(_FakeTranscriber("送り直し"), backend="soniox"))
        VoicekeyApp._process_task(
            s, _task(ctx, seconds=2.0, streamer=_FakeStreamer("", sent_seconds=1.5))
        )
        self.assertEqual(
            [c.args for c in s._api_usage.record_stt.call_args_list],
            [("soniox", "stt-rt-v5", 1.5), ("groq", "whisper-large-v3-turbo", 2.0)],
        )

    def test_streaming_without_sent_audio_not_recorded(self):
        s = _worker_self()
        ctx = _ctx(_slot(_FakeTranscriber(""), backend="soniox"))
        VoicekeyApp._process_task(s, _task(ctx, seconds=0.1, streamer=_FakeStreamer("", 0.0)))
        s._api_usage.record_stt.assert_not_called()

    def test_failed_transcription_not_recorded(self):
        s = _worker_self()
        ctx = _ctx(_slot(_FakeTranscriber(error=TranscriptionError("失敗"))))
        with self.assertRaises(TranscriptionError):
            VoicekeyApp._process_task(s, _task(ctx))
        s._api_usage.record_stt.assert_not_called()

    def test_parallel_segments_recorded_each(self):
        usage = []
        slot = _slot(_FakeTranscriber("あ"))
        segments = [np.zeros(SAMPLE_RATE, dtype=np.float32), np.zeros(SAMPLE_RATE * 2, dtype=np.float32)]
        VoicekeyApp._transcribe_parallel(None, slot, segments, usage)
        self.assertEqual(sorted(u[3] for u in usage), [1.0, 2.0])
        self.assertTrue(all(u[:3] == ("stt", "groq", "whisper-large-v3-turbo") for u in usage))

    def test_flush_continues_after_store_error(self):
        """1 件の記録に失敗しても、残りの記録は書く。"""
        store = mock.Mock()
        store.record_stt.side_effect = OSError("disk")
        _flush_usage(store, [("stt", "groq", "m", 1.0), ("llm", {"usage": {}}, "m2", "translation")])
        store.record_chat_usage.assert_called_once_with({"usage": {}}, "groq", "m2", "translation")


class TestTimelineLog(unittest.TestCase):
    """`[計測]` の 1 行。"""

    def _line(self, logs):
        lines = [m for m in logs.output if "[計測]" in m]
        self.assertEqual(len(lines), 1)
        return lines[0]

    def test_line_logged_on_success(self):
        s = _worker_self()
        ctx = _ctx(
            _slot(_FakeTranscriber("こんにちは")), gen=4, pressed_at=1.0, released_at=3.0,
            target_app=("chrome", r"C:\Program Files\Google\chrome.exe".replace("\\", "/")),
        )
        with self.assertLogs("src.app", level="INFO") as logs:
            VoicekeyApp._process_task(s, _task(ctx, seconds=2.0, auto_enter=True))
        line = self._line(logs)
        for part in (
            "gen=4 slot=1 backend=groq model=whisper-large-v3-turbo 経路=rest",
            "結果=貼付", "録音=2.00s", "文字数=5", "貼付先=chrome.exe", "auto_enter=true",
        ):
            self.assertIn(part, line)
        # 本文は出さない
        self.assertNotIn("こんにちは", line)

    def test_line_logged_on_failure(self):
        s = _worker_self()
        ctx = _ctx(_slot(_FakeTranscriber(error=TranscriptionError("失敗"))))
        with self.assertLogs("src.app", level="INFO") as logs:
            with self.assertRaises(TranscriptionError):
                VoicekeyApp._process_task(s, _task(ctx))
        self.assertIn("結果=文字起こし失敗", self._line(logs))

    def test_too_short_and_empty_outcomes(self):
        s = _worker_self()
        with self.assertLogs("src.app", level="INFO") as logs:
            VoicekeyApp._process_task(s, _task(_ctx(_slot(_FakeTranscriber("x"))), seconds=0.1))
        self.assertIn("結果=短すぎ", self._line(logs))
        with self.assertLogs("src.app", level="INFO") as logs:
            VoicekeyApp._process_task(s, _task(_ctx(_slot(_FakeTranscriber("")))))
        self.assertIn("結果=空結果", self._line(logs))

    def test_streaming_route_and_ending(self):
        s = _worker_self()
        ctx = _ctx(_slot(_FakeTranscriber("x"), backend="soniox"))
        with self.assertLogs("src.app", level="INFO") as logs:
            VoicekeyApp._process_task(s, _task(ctx, streamer=_FakeStreamer("ライブ")))
        self.assertIn("経路=streaming 終わり方=completed 結果=貼付", self._line(logs))


class TestPasteFailure(unittest.TestCase):
    """貼り付け・Enter が届かなかったとき。"""

    def test_paste_failure_notifies_and_skips_enter(self):
        """貼り付けできなかった回は知らせ、Enter は送らない（別の文を送信してしまうため）。"""
        s = _worker_self(pasted=False)
        ctx = _ctx(_slot(_FakeTranscriber("こんにちは")))
        with self.assertLogs("src.app", level="INFO") as logs:
            VoicekeyApp._process_task(s, _task(ctx, auto_enter=True))
        s.notice.emit.assert_called_once_with(PASTE_FAILED_NOTICE)
        s._input_handler.press_enter.assert_not_called()
        # 履歴には残っている（そこからも救える）
        s._history.add.assert_called_once_with("こんにちは")
        self.assertTrue(any("結果=貼付失敗" in m for m in logs.output))

    def test_enter_failure_notifies(self):
        s = _worker_self(enter_ok=False)
        ctx = _ctx(_slot(_FakeTranscriber("こんにちは")))
        VoicekeyApp._process_task(s, _task(ctx, auto_enter=True))
        s.notice.emit.assert_called_once_with(ENTER_FAILED_NOTICE)

    def test_success_sends_enter_without_notice(self):
        s = _worker_self()
        ctx = _ctx(_slot(_FakeTranscriber("こんにちは")))
        VoicekeyApp._process_task(s, _task(ctx, auto_enter=True))
        s._input_handler.press_enter.assert_called_once()
        s.notice.emit.assert_not_called()


class _FakeSonioxReplay(SonioxTranscriber):
    """Soniox の送り直しの偽物（通信しない）。送り直しの文と、最後まで確定したかを返す。"""

    def __init__(self, text, completed):
        super().__init__("stt-rt-v5")
        self._result = (text, completed)

    def transcribe_replay(self, audio_data):
        return self._result


def _live(ending, partial):
    """確定が空で終わったライブ接続の偽物（終わり方と、届いていた分を持つ）。"""
    streamer = _FakeStreamer("", failed=(ending == "failed"))
    streamer.ending = ending
    streamer.partial_text = partial
    return streamer


class TestLiveEndingAndReplay(unittest.TestCase):
    """ライブが最後まで確定しなかったときの送り直しと「途中までの入力」の通知。"""

    def _run(self, streamer, transcriber):
        s = _worker_self()
        ctx = _ctx(_slot(transcriber, backend="soniox"))
        with self.assertLogs("src.app", level="INFO") as logs:
            VoicekeyApp._process_task(s, _task(ctx, streamer=streamer))
        return s, logs

    def test_incomplete_replay_keeps_longer_live_text_and_notifies(self):
        """切断後の送り直しも途中で終わったら、短い送り直しで置き換えず、途中までと知らせる。"""
        s, _ = self._run(_live("failed", "ライブで取れた長い前半"), _FakeSonioxReplay("短い", False))
        s._input_handler.insert_text.assert_called_once_with("ライブで取れた長い前半")
        s.notice.emit.assert_called_once_with(PARTIAL_INPUT_NOTICE)

    def test_incomplete_but_longer_replay_is_used(self):
        s, _ = self._run(_live("failed", "前半"), _FakeSonioxReplay("前半と後半の途中", False))
        s._input_handler.insert_text.assert_called_once_with("前半と後半の途中")
        s.notice.emit.assert_called_once_with(PARTIAL_INPUT_NOTICE)

    def test_completed_replay_replaces_live_without_notice(self):
        s, _ = self._run(_live("failed", "前半だけ長い長い長い"), _FakeSonioxReplay("全文", True))
        s._input_handler.insert_text.assert_called_once_with("全文")
        s.notice.emit.assert_not_called()

    def test_timeout_uses_arrived_text_without_resend(self):
        """待ち時間切れは送り直さず届いた分を貼る（Mac と同じ）。切断ではないので知らせない。"""
        transcriber = _FakeTranscriber("全文がそろった")
        s, _ = self._run(_live("timeout", "届いた分"), transcriber)
        self.assertEqual(transcriber.calls, 0)
        s._input_handler.insert_text.assert_called_once_with("届いた分")
        s.notice.emit.assert_not_called()

    def test_timeout_with_nothing_arrived_resends(self):
        transcriber = _FakeTranscriber("全文がそろった")
        s, _ = self._run(_live("timeout", ""), transcriber)
        self.assertEqual(transcriber.calls, 1)
        s._input_handler.insert_text.assert_called_once_with("全文がそろった")
        s.notice.emit.assert_not_called()

    def test_failed_replay_falls_back_to_live_text_and_notifies(self):
        transcriber = _FakeTranscriber(error=TranscriptionError("送り直しも失敗"))
        s, _ = self._run(_live("failed", "ライブの分"), transcriber)
        s._input_handler.insert_text.assert_called_once_with("ライブの分")
        s.notice.emit.assert_called_once_with(PARTIAL_INPUT_NOTICE)


class TestResolveReplay(unittest.TestCase):
    """送り直し後にどちらを貼るか（Mac の Transcriber.resolveReplay と同じ判定）。"""

    def test_cases(self):
        self.assertEqual(resolve_replay("ライブ", "", True), ("ライブ", False))
        self.assertEqual(resolve_replay("ライブ", "全文です", True), ("全文です", True))
        self.assertEqual(resolve_replay("ライブ", "全", True), ("全", True))
        self.assertEqual(resolve_replay("長いライブ分", "短い", False), ("長いライブ分", False))
        self.assertEqual(resolve_replay("短", "長い送り直し", False), ("長い送り直し", False))


class TestTranslateOnPaste(unittest.TestCase):
    """翻訳して入力（貼り付ける直前に訳す）。"""

    def test_translated_text_is_pasted_and_usage_recorded(self):
        s = _worker_self(config={"replacements": [{"from": "こんにちは", "to": "こんばんは"}]})
        ctx = _ctx(
            _slot(_FakeTranscriber("こんにちは")), gen=9, translate=True,
            translate_target="en", translate_model="tr-model",
        )
        response = {"usage": {"prompt_tokens": 1}}
        seen = {}

        def fake_translate(text, target, model, on_usage=None):
            seen.update(text=text, target=target, model=model)
            on_usage(response)
            return "Good evening"

        with mock.patch.object(dictation_translator, "translate", fake_translate), \
                self.assertLogs("src.app", level="INFO") as logs:
            VoicekeyApp._process_task(s, _task(ctx))
        # 翻訳にかけるのは、置換まで終わった文
        self.assertEqual(seen, {"text": "こんばんは", "target": "en", "model": "tr-model"})
        s._input_handler.insert_text.assert_called_once_with("Good evening")
        s._history.add.assert_called_once_with("Good evening")
        s._api_usage.record_chat_usage.assert_called_once_with(
            response, "groq", "tr-model", "translation"
        )
        self.assertTrue(
            any("[翻訳] gen=9 engine=groq target=en" in m and "結果=ok" in m for m in logs.output)
        )

    def test_failure_pastes_original_and_notifies(self):
        s = _worker_self()
        ctx = _ctx(_slot(_FakeTranscriber("こんにちは")), translate=True)

        def fake_translate(text, target, model, on_usage=None):
            raise dictation_translator.TranslationError("HTTP 500")

        with mock.patch.object(dictation_translator, "translate", fake_translate), \
                self.assertLogs("src.app", level="INFO") as logs:
            VoicekeyApp._process_task(s, _task(ctx))
        s._input_handler.insert_text.assert_called_once_with("こんにちは")
        s.notice.emit.assert_called_once_with(dictation_translator.FAILED_NOTICE)
        self.assertTrue(any("結果=失敗(HTTP 500)" in m for m in logs.output))

    def test_not_translated_when_off(self):
        s = _worker_self()
        ctx = _ctx(_slot(_FakeTranscriber("こんにちは")))
        with mock.patch.object(dictation_translator, "translate") as translate:
            VoicekeyApp._process_task(s, _task(ctx))
        translate.assert_not_called()


class TestAppStats(unittest.TestCase):
    """アプリ別使用状況への記録。"""

    def test_target_app_passed_to_stats_after_paste(self):
        s = _worker_self()
        ctx = _ctx(
            _slot(_FakeTranscriber("こんにちは")),
            target_app=("Chrome", "C:/Program Files/Google/Chrome.exe"),
        )
        VoicekeyApp._process_task(s, _task(ctx, seconds=2.0))
        s._stats.record_session.assert_called_once_with(
            5, 2.0, app_id="chrome.exe", app_name="Chrome",
            app_path="C:/Program Files/Google/Chrome.exe",
        )
        self.assertLess(s.events.index("paste"), s.events.index("stats"))

    def test_without_target_app(self):
        s = _worker_self()
        VoicekeyApp._process_task(s, _task(_ctx(_slot(_FakeTranscriber("こんにちは"))), seconds=2.0))
        s._stats.record_session.assert_called_once_with(5, 2.0)


class TestTargetAppCapture(unittest.TestCase):
    """貼り付け先アプリの取得と HUD への受け渡し。"""

    def _self(self, app, active_ctx):
        return SimpleNamespace(
            _platform=SimpleNamespace(foreground_app=lambda: app),
            _active_context=active_ctx,
            target_app_changed=mock.Mock(),
        )

    def test_sets_context_and_emits_path(self):
        ctx = _ctx(_slot(_FakeTranscriber()))
        s = self._self(("chrome", "C:/chrome.exe"), ctx)
        VoicekeyApp._capture_target_app(s, ctx)
        self.assertEqual(ctx.target_app, ("chrome", "C:/chrome.exe"))
        s.target_app_changed.emit.assert_called_once_with("C:/chrome.exe")

    def test_late_result_does_not_touch_hud(self):
        """録音が終わった後に届いた結果は、記録には使うが HUD へは出さない。"""
        ctx = _ctx(_slot(_FakeTranscriber()))
        s = self._self(("chrome", "C:/chrome.exe"), None)
        VoicekeyApp._capture_target_app(s, ctx)
        self.assertEqual(ctx.target_app, ("chrome", "C:/chrome.exe"))
        s.target_app_changed.emit.assert_not_called()

    def test_unknown_app(self):
        ctx = _ctx(_slot(_FakeTranscriber()))
        s = self._self(None, ctx)
        VoicekeyApp._capture_target_app(s, ctx)
        self.assertIsNone(ctx.target_app)
        s.target_app_changed.emit.assert_not_called()

    def test_icon_handed_to_hud_on_ui_thread(self):
        hud = mock.Mock()
        s = SimpleNamespace(_hud=hud, _platform=SimpleNamespace(app_icon=lambda path: f"icon:{path}"))
        VoicekeyApp._on_target_app_changed(s, "C:/chrome.exe")
        hud.set_app_icon.assert_called_once_with("icon:C:/chrome.exe")
        VoicekeyApp._on_target_app_changed(s, "")
        hud.set_app_icon.assert_called_with(None)

    def test_idle_clears_icon(self):
        s = SimpleNamespace(
            _state_lock=threading.Lock(), _recording_slot=None, _outstanding=0, _auto_enter=False,
            _recording_effective_mode=HotkeyMode.HOLD.value,
            hands_free_changed=mock.Mock(), status_changed=mock.Mock(),
            target_app_changed=mock.Mock(),
        )
        VoicekeyApp._emit_state(s)
        s.target_app_changed.emit.assert_called_once_with("")
        # 変換中は残す
        s._outstanding = 1
        s.target_app_changed.reset_mock()
        VoicekeyApp._emit_state(s)
        s.target_app_changed.emit.assert_not_called()


class _InlineThread:
    """start() した時点で同じスレッドで実行する Thread の偽物。"""

    def __init__(self, target=None, args=(), kwargs=None, daemon=None, name=None):
        self._target = target
        self._args = args

    def start(self):
        self._target(*self._args)


def _listener_self(slot, recording=False, ctx=None, config=None):
    """_on_press / _on_release を本物の判定で動かすための偽 self。"""
    s = SimpleNamespace(
        _platform=SimpleNamespace(normalize_listener_key=lambda k: k),
        _pressed_keys=set(),
        _hotkey_test_active=False,
        _recording_slot=1 if recording else None,
        _recording_effective_mode=HotkeyMode.HOLD.value,
        _recording_started=0.0,
        _auto_enter=False,
        _slots={1: slot},
        _state_lock=threading.Lock(),
        _pending_tap_timer=None,
        _last_release_time=0.0,
        _last_release_slot=None,
        _handsfree_keys=set(),
        _repaste_keys=VoicekeyApp._parse_hotkey(DEFAULT_CONFIG["repaste_key"]),
        _translate_once_keys=VoicekeyApp._parse_hotkey(DEFAULT_CONFIG["translate_once_key"]),
        _repaste_armed=False,
        _translate_once=False,
        _active_context=ctx,
        _config=_FakeConfig(config),
        _acceptable_names=VoicekeyApp._acceptable_names,
        _begin_recording=mock.Mock(),
        _finish_recording=mock.Mock(),
        _emit_state=mock.Mock(),
        _repaste_last=mock.Mock(),
        translate_badge_changed=mock.Mock(),
    )
    for name in (
        "_slot_matches", "_handsfree_pressed", "_key_in_slot",
        "_translate_once_pressed", "_repaste_pressed",
    ):
        setattr(s, name, getattr(VoicekeyApp, name).__get__(s))
    return s


class TestRepasteKey(unittest.TestCase):
    """再貼り付けキー（既定 Ctrl+Alt+V）。"""

    def test_default_key(self):
        self.assertEqual(DEFAULT_CONFIG["repaste_key"], "<ctrl>+<alt>+<v>")

    def test_pastes_after_all_keys_released(self):
        """押した時点では貼らない（押している Alt 等が Ctrl+V に混ざるため）。全部離れてから貼る。"""
        s = _listener_self(_slot(_FakeTranscriber()))
        with mock.patch.object(app_mod.threading, "Thread", _InlineThread):
            for key in ("ctrl_l", "alt_l", "v"):
                VoicekeyApp._on_press(s, key)
            s._repaste_last.assert_not_called()
            VoicekeyApp._on_release(s, "v")
            VoicekeyApp._on_release(s, "alt_l")
            s._repaste_last.assert_not_called()
            VoicekeyApp._on_release(s, "ctrl_l")
        s._repaste_last.assert_called_once()
        s._begin_recording.assert_not_called()

    def test_partial_combination_does_nothing(self):
        s = _listener_self(_slot(_FakeTranscriber()))
        with mock.patch.object(app_mod.threading, "Thread", _InlineThread):
            for key in ("ctrl_l", "v"):
                VoicekeyApp._on_press(s, key)
            for key in ("v", "ctrl_l"):
                VoicekeyApp._on_release(s, key)
        s._repaste_last.assert_not_called()

    def test_ignored_while_recording(self):
        s = _listener_self(_slot(_FakeTranscriber()), recording=True, ctx=_ctx(_slot(_FakeTranscriber())))
        s._recording_effective_mode = HotkeyMode.TOGGLE.value
        with mock.patch.object(app_mod.threading, "Thread", _InlineThread):
            for key in ("ctrl_l", "alt_l", "v"):
                VoicekeyApp._on_press(s, key)
            for key in ("v", "alt_l", "ctrl_l"):
                VoicekeyApp._on_release(s, key)
        self.assertFalse(s._repaste_armed)
        s._repaste_last.assert_not_called()

    def test_disabled_when_key_empty(self):
        s = _listener_self(_slot(_FakeTranscriber()))
        s._repaste_keys = set()
        with mock.patch.object(app_mod.threading, "Thread", _InlineThread):
            VoicekeyApp._on_press(s, "v")
            VoicekeyApp._on_release(s, "v")
        s._repaste_last.assert_not_called()

    def _repaste_self(self, items, pasted=True):
        handler = mock.Mock()
        handler.insert_text.return_value = pasted
        return SimpleNamespace(
            _history=SimpleNamespace(items=lambda: items), _input_handler=handler, notice=mock.Mock()
        )

    def test_repaste_last_pastes_newest_entry(self):
        s = self._repaste_self([{"text": "新しい"}, {"text": "古い"}])
        VoicekeyApp._repaste_last(s)
        s._input_handler.insert_text.assert_called_once_with("新しい")
        s.notice.emit.assert_not_called()

    def test_repaste_last_silent_when_history_empty(self):
        s = self._repaste_self([])
        VoicekeyApp._repaste_last(s)
        s._input_handler.insert_text.assert_not_called()
        s.notice.emit.assert_not_called()

    def test_repaste_last_notifies_on_failure(self):
        s = self._repaste_self([{"text": "新しい"}], pasted=False)
        VoicekeyApp._repaste_last(s)
        s.notice.emit.assert_called_once_with(PASTE_FAILED_NOTICE)


class TestTranslateOnceKey(unittest.TestCase):
    """1 回だけ翻訳するキー（既定 右 Shift）。"""

    def test_default_settings(self):
        self.assertEqual(DEFAULT_CONFIG["translate_once_key"], "<shift_r>")
        self.assertIs(DEFAULT_CONFIG["translate_input_enabled"], False)
        self.assertEqual(DEFAULT_CONFIG["translate_input_target"], "en")

    def test_press_during_recording_toggles(self):
        ctx = _ctx(_slot(_FakeTranscriber()), gen=3, translate_target="zh-Hans")
        s = _listener_self(_slot(_FakeTranscriber()), recording=True, ctx=ctx)
        s._pressed_keys = {"f2"}
        with self.assertLogs("src.app", level="INFO") as logs:
            VoicekeyApp._on_press(s, "shift_r")
            self.assertTrue(s._translate_once)
            s.translate_badge_changed.emit.assert_called_with("→ZH")
            VoicekeyApp._on_release(s, "shift_r")
            # もう一度押すと取り消し（HUD の印も消す）
            VoicekeyApp._on_press(s, "shift_r")
            self.assertFalse(s._translate_once)
            s.translate_badge_changed.emit.assert_called_with("")
        self.assertTrue(any("[翻訳] gen=3 1回翻訳キー(録音中)" in m for m in logs.output))
        self.assertTrue(any("[翻訳] gen=3 1回翻訳キーで取り消し(録音中)" in m for m in logs.output))
        # 録音は止めない
        s._finish_recording.assert_not_called()

    def test_does_not_turn_off_always_translate(self):
        """「常に翻訳して入力」が ON の回は、このキーで外さない。"""
        ctx = _ctx(_slot(_FakeTranscriber()), translate=True)
        s = _listener_self(_slot(_FakeTranscriber()), recording=True, ctx=ctx)
        s._pressed_keys = {"f2"}
        VoicekeyApp._on_press(s, "shift_r")
        self.assertFalse(s._translate_once)
        self.assertTrue(ctx.translate)
        s.translate_badge_changed.emit.assert_not_called()

    def test_not_recording_does_nothing(self):
        s = _listener_self(_slot(_FakeTranscriber()))
        VoicekeyApp._on_press(s, "shift_r")
        self.assertFalse(s._translate_once)
        s.translate_badge_changed.emit.assert_not_called()

    def test_key_that_is_record_key_is_not_translate_key(self):
        """録音キーと同じキーは録音キーとして扱う（toggle 録音の停止を奪わない）。"""
        slot = _slot(_FakeTranscriber(), hotkey="<shift_r>")
        s = _listener_self(slot, recording=True, ctx=_ctx(slot))
        s._recording_effective_mode = HotkeyMode.TOGGLE.value
        VoicekeyApp._on_press(s, "shift_r")
        self.assertFalse(s._translate_once)
        s._finish_recording.assert_called_once()

    def test_disabled_when_key_empty(self):
        s = _listener_self(_slot(_FakeTranscriber()), recording=True, ctx=_ctx(_slot(_FakeTranscriber())))
        s._translate_once_keys = set()
        VoicekeyApp._on_press(s, "shift_r")
        self.assertFalse(s._translate_once)


class TestRecordingLifecycle(unittest.TestCase):
    """録音の開始・終了で確定する値。"""

    def _self(self, config=None):
        slot = _slot(_FakeTranscriber())
        slot.transcriber.prewarm = mock.Mock()
        values = {"sound_effects_enabled": False, "duck_media_enabled": False, "streaming_enabled": False}
        values.update(config or {})
        s = SimpleNamespace(
            _state_lock=threading.Lock(),
            _recording_slot=None,
            _recording_effective_mode=HotkeyMode.HOLD.value,
            _recording_started=0.0,
            _auto_enter=False,
            _active_streamer=None,
            _active_context=None,
            _outstanding=0,
            _pending_tap_timer=None,
            _generation=0,
            _translate_once=True,   # 前の録音の値が残っていても、開始で必ず False に戻る
            _translate_once_keys=set(),
            _practice_format_override=False,
            _slots={1: slot},
            _config=_FakeConfig(values),
            _recorder=mock.Mock(),
            _task_q=mock.Mock(),
            _emit_state=mock.Mock(),
            _capture_target_app=mock.Mock(),
            # Windows では hold の録音で離鍵の見回りが始まる（ここで見るのは開始処理だけ）
            _start_hold_release_watch=mock.Mock(),
            translate_badge_changed=mock.Mock(),
        )
        s._maybe_handsfree_slot = VoicekeyApp._maybe_handsfree_slot.__get__(s)
        s._snapshot_context = VoicekeyApp._snapshot_context.__get__(s)
        s._on_record_started = VoicekeyApp._on_record_started.__get__(s)
        return s

    def _begin(self, s):
        with mock.patch.object(app_mod.threading, "Thread", _InlineThread):
            VoicekeyApp._begin_recording(s, 1, False)

    def test_begin_numbers_recording_and_resets_once_flag(self):
        s = self._self()
        self._begin(s)
        ctx = s._active_context
        self.assertEqual(ctx.gen, 1)
        self.assertIsNotNone(ctx.pressed_at)
        self.assertFalse(ctx.translate)
        self.assertFalse(s._translate_once)
        s.translate_badge_changed.emit.assert_called_once_with("")
        s._capture_target_app.assert_called_once_with(ctx)

    def test_begin_shows_badge_when_always_translate(self):
        s = self._self({"translate_input_enabled": True, "translate_input_target": "ko"})
        self._begin(s)
        self.assertTrue(s._active_context.translate)
        self.assertEqual(s._active_context.translate_target, "ko")
        s.translate_badge_changed.emit.assert_called_once_with("→KO")

    def test_finish_fixes_translate_from_once_key(self):
        s = self._self()
        self._begin(s)
        ctx = s._active_context
        s._translate_once = True   # 録音中に 1 回翻訳キーを押した
        s.translate_badge_changed.reset_mock()
        with mock.patch.object(app_mod.media_ducker, "restore"):
            VoicekeyApp._finish_recording(s)
        self.assertTrue(ctx.translate)
        self.assertIsNotNone(ctx.released_at)
        self.assertFalse(s._translate_once)
        # 翻訳の印は録音中だけ
        s.translate_badge_changed.emit.assert_called_once_with("")

    def test_finish_without_once_key_does_not_translate(self):
        s = self._self()
        self._begin(s)
        ctx = s._active_context
        with mock.patch.object(app_mod.media_ducker, "restore"):
            VoicekeyApp._finish_recording(s)
        self.assertFalse(ctx.translate)

    def test_record_started_stamps_time(self):
        s = self._self()
        self._begin(s)
        VoicekeyApp._on_record_started(s, True)
        self.assertIsNotNone(s._active_context.record_started_at)


class TestListenerStopped(unittest.TestCase):
    """キーボードリスナーが止まって離鍵を取りこぼしたとき。"""

    def _self(self, mode):
        return SimpleNamespace(
            _pressed_keys={"f2"},
            _repaste_armed=True,
            _recording_slot=1,
            _recording_effective_mode=mode,
            _finish_recording=mock.Mock(),
        )

    def test_hold_recording_is_finished(self):
        """押している間だけ録る録音は、離鍵が届かなくなるので止める（上限まで録り続けない）。"""
        s = self._self(HotkeyMode.HOLD.value)
        VoicekeyApp._on_listener_stopped(s)
        self.assertEqual(s._pressed_keys, set())
        self.assertFalse(s._repaste_armed)
        s._finish_recording.assert_called_once()

    def test_toggle_recording_continues(self):
        s = self._self(HotkeyMode.TOGGLE.value)
        VoicekeyApp._on_listener_stopped(s)
        s._finish_recording.assert_not_called()

    def test_not_recording(self):
        s = self._self(HotkeyMode.HOLD.value)
        s._recording_slot = None
        VoicekeyApp._on_listener_stopped(s)
        s._finish_recording.assert_not_called()


if __name__ == "__main__":
    unittest.main()
