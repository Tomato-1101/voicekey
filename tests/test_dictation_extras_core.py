"""音声入力まわりの追加機能のうち、app.py 以外のモジュールのテスト。

対象:
- stats: アプリ別の使用状況（記録・上位 N 件・永続化・壊れたデータ）
- input_handler: 貼り付けに失敗した回は結果をクリップボードに残す・貼り付けの直列化
- streaming_transcriber: 送った音声の長さ（料金の記録に使う）
- text_formatter: 課金された応答を呼び出し側へ渡す
- Windows アダプタ: Ctrl 併用の英数字キーの読み替え・前面アプリ
- 設定の既定値

外部への通信は一切行わない（接続・HTTP クライアントは既存テストの偽物を使う）。
"""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np

from src.config.constants import DEFAULT_CONFIG
from src.core import streaming_transcriber as stream_mod
from src.core import text_formatter
from src.core.input_handler import InputHandler
from src.core.stats import StatsStore
from src.core.streaming_transcriber import SonioxStreamer
from src.platform.base import PlatformAdapter
from src.platform.windows import foreground_app
from src.platform.windows.adapter import WindowsPlatformAdapter
from tests.test_input_handler import _ClipboardTestBase, _FakeClipboard, _FakeTimer
from tests.test_streaming_transcriber import FakeWS, _is_text, _StreamTestCase, _wait_until
from tests.test_text_formatter import _mock_client, _response


class TestAppUsageStats(unittest.TestCase):
    """アプリ別の使用状況。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "stats.json"

    def tearDown(self):
        self._tmp.cleanup()

    def test_top_apps_sorted_by_characters(self):
        store = StatsStore(self.path)
        store.record_session(10, 1.0, app_id="notepad.exe", app_name="notepad", app_path="C:/n.exe")
        store.record_session(30, 1.0, app_id="chrome.exe", app_name="chrome", app_path="C:/c.exe")
        store.record_session(5, 1.0, app_id="notepad.exe", app_name="notepad", app_path="C:/n.exe")
        self.assertEqual(
            store.top_apps(),
            [
                {"id": "chrome.exe", "name": "chrome", "path": "C:/c.exe", "characters": 30, "sessions": 1},
                {"id": "notepad.exe", "name": "notepad", "path": "C:/n.exe", "characters": 15, "sessions": 2},
            ],
        )
        self.assertEqual([a["id"] for a in store.top_apps(1)], ["chrome.exe"])
        self.assertEqual(store.top_apps(0), [])

    def test_without_app_only_totals_grow(self):
        """貼り付け先が分からなかった回は合計にだけ積む。"""
        store = StatsStore(self.path)
        store.record_session(10, 1.0)
        self.assertEqual(store.top_apps(), [])
        self.assertEqual(store.snapshot()["total_characters"], 10)

    def test_empty_input_not_counted(self):
        store = StatsStore(self.path)
        store.record_session(0, 1.0, app_id="chrome.exe", app_name="chrome")
        self.assertEqual(store.top_apps(), [])

    def test_latest_name_and_path_win(self):
        """アプリの更新で置き場所が変わっても、同じアプリとして積み、新しいパスを覚える。"""
        store = StatsStore(self.path)
        store.record_session(10, 1.0, app_id="app.exe", app_name="app", app_path="C:/old/app.exe")
        store.record_session(10, 1.0, app_id="app.exe", app_name="app", app_path="C:/new/app.exe")
        self.assertEqual(store.top_apps()[0]["path"], "C:/new/app.exe")
        self.assertEqual(store.top_apps()[0]["characters"], 20)

    def test_persisted_and_reloaded(self):
        StatsStore(self.path).record_session(10, 1.0, app_id="chrome.exe", app_name="chrome")
        self.assertEqual(StatsStore(self.path).top_apps()[0]["characters"], 10)

    def test_broken_entries_are_dropped(self):
        self.path.write_text(json.dumps({
            "total_characters": 5,
            "apps": {
                "ok.exe": {"name": "ok", "characters": 5, "sessions": 1},
                "broken.exe": "文字列",
            },
        }), encoding="utf-8")
        store = StatsStore(self.path)
        self.assertEqual(
            store.top_apps(),
            [{"id": "ok.exe", "name": "ok", "path": "", "characters": 5, "sessions": 1}],
        )

    def test_broken_apps_container_is_ignored(self):
        self.path.write_text(json.dumps({"total_characters": 5, "apps": [1, 2]}), encoding="utf-8")
        store = StatsStore(self.path)
        self.assertEqual(store.top_apps(), [])
        self.assertEqual(store.snapshot()["total_characters"], 5)


class TestPasteFailureKeepsResult(_ClipboardTestBase):
    """貼り付けのキー合成に失敗した回は、結果をクリップボードに残す（手で貼って救えるように）。"""

    def _insert_failing(self, ih: InputHandler, clip: _FakeClipboard, text: str):
        """キーの合成が例外になる状態で insert_text を 1 回実行する。"""
        keyboard = mock.Mock()
        keyboard.press.side_effect = RuntimeError("キーを送れない")
        with mock.patch("src.core.input_handler.pyperclip") as pc, \
             mock.patch("src.core.input_handler.time.sleep"), \
             mock.patch("src.core.input_handler.threading.Timer", _FakeTimer), \
             mock.patch.object(ih, "_keyboard", keyboard):
            pc.paste.side_effect = clip.paste
            pc.copy.side_effect = clip.copy
            return ih.insert_text(text)

    def test_result_stays_on_clipboard(self):
        ih = InputHandler()
        clip = _FakeClipboard("USER")
        self.assertFalse(self._insert_failing(ih, clip, "RESULT"))
        self.assertEqual(clip.value, "RESULT")
        # 元へ戻す予約をしていない（1 秒後に結果が消えない）
        self.assertEqual(_FakeTimer.created, [])

    def test_next_paste_does_not_bring_back_stale_original(self):
        """失敗した回の退避（USER）を、次の貼り付けの「原本」として書き戻さない。"""
        ih = InputHandler()
        clip = _FakeClipboard("USER")
        self._insert_failing(ih, clip, "RESULT")
        self.assertTrue(self._insert(ih, clip, "SECOND"))
        self._fire(_FakeTimer.created[0], clip)
        # 残しておいた結果へ戻る（古い USER ではない）
        self.assertEqual(clip.value, "RESULT")

    def test_paste_is_serialized(self):
        """クリップボードへ入れてからキーを送るまでの間、ほかの貼り付けを入れない。"""
        ih = InputHandler()
        clip = _FakeClipboard("USER")
        locked = []
        keyboard = mock.Mock()
        keyboard.press.side_effect = lambda key: locked.append(ih._paste_lock.locked())
        with mock.patch("src.core.input_handler.pyperclip") as pc, \
             mock.patch("src.core.input_handler.time.sleep"), \
             mock.patch("src.core.input_handler.threading.Timer", _FakeTimer), \
             mock.patch.object(ih, "_keyboard", keyboard):
            pc.paste.side_effect = clip.paste
            pc.copy.side_effect = clip.copy
            self.assertTrue(ih.insert_text("A"))
        self.assertTrue(locked and all(locked))
        self.assertFalse(ih._paste_lock.locked())


class TestSentSeconds(_StreamTestCase):
    """ライブ接続で送った音声の長さ（料金の記録に使う）。"""

    def test_counts_sent_audio_only(self):
        self.ws = FakeWS(
            after_end=[
                {"tokens": [{"text": "こんにちは", "is_final": True}]},
                {"tokens": [{"text": "<fin>", "is_final": True}]},
            ],
            is_end=lambda f: _is_text(f, "finalize"),
        )
        s = SonioxStreamer("stt-rt-v5")
        self.assertEqual(s.sent_seconds, 0.0)
        self.assertTrue(s.start())
        s.send(np.full(1600, 0.5, dtype=np.float32))
        s.send(np.full(3200, 0.5, dtype=np.float32))
        self.assertEqual(s.finish(timeout=2.0), "こんにちは")
        # 16kHz で 4800 サンプル = 0.3 秒（締めに足す無音は数えない）
        self.assertAlmostEqual(s.sent_seconds, 0.3)

    def test_zero_when_never_connected(self):
        self._patch(stream_mod.engines, "api_key", lambda engine_id: "")
        s = SonioxStreamer("stt-rt-v5")
        self.assertFalse(s.start())
        self.assertEqual(s.sent_seconds, 0.0)


class TestFormatUsageCallback(unittest.TestCase):
    """整形の応答を料金の記録へ渡す。"""

    def _format(self, response, on_usage):
        client = _mock_client(response)
        with mock.patch.object(text_formatter.engines.secrets, "get_api_key", return_value="K"), \
                mock.patch.object(text_formatter, "_get_client", return_value=client):
            return text_formatter.format_text("原文", "m", on_usage=on_usage)

    def test_called_with_response_json(self):
        payload = {"choices": [{"message": {"content": "整形済み"}}], "usage": {"prompt_tokens": 3}}
        seen = []
        self.assertEqual(self._format(_response(200, payload), seen.append), "整形済み")
        self.assertEqual(seen, [payload])

    def test_called_even_when_truncated(self):
        """途中で切れて原文に倒す回も、トークン代はかかっているので渡す。"""
        payload = {"choices": [{"finish_reason": "length", "message": {"content": "整"}}]}
        seen = []
        self.assertEqual(self._format(_response(200, payload), seen.append), "原文")
        self.assertEqual(seen, [payload])

    def test_not_called_on_http_error(self):
        seen = []
        self.assertEqual(self._format(_response(500, text="err"), seen.append), "原文")
        self.assertEqual(seen, [])


class TestWindowsAdapter(unittest.TestCase):
    """Windows アダプタ（キーの読み替え・前面アプリ）。"""

    def setUp(self):
        self.adapter = WindowsPlatformAdapter()

    def test_ctrl_letter_is_read_from_vk(self):
        """Ctrl+V は pynput が制御文字（\\x16）で報告するので、vk から文字を決める。"""
        key = SimpleNamespace(char="\x16", vk=0x56)
        self.assertEqual(self.adapter.normalize_listener_key(key), "v")

    def test_digit_is_read_from_vk(self):
        self.assertEqual(self.adapter.normalize_listener_key(SimpleNamespace(char=None, vk=0x31)), "1")

    def test_named_key_is_not_touched(self):
        """名前を持つキー（修飾キー等）は、vk が英数字の範囲でも従来の読み方のまま。"""
        key = SimpleNamespace(name="shift_r", vk=0x41)
        self.assertEqual(self.adapter.normalize_listener_key(key), "shift_r")

    def test_jis_key_still_resolved(self):
        self.assertEqual(
            self.adapter.normalize_listener_key(SimpleNamespace(char=None, vk=0x1D)), "nonconvert"
        )

    def test_foreground_app_name_from_exe(self):
        path = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
        with mock.patch.object(foreground_app, "foreground_exe_path", return_value=path):
            self.assertEqual(self.adapter.foreground_app(), ("chrome", path))

    def test_foreground_app_unknown(self):
        with mock.patch.object(foreground_app, "foreground_exe_path", return_value=""):
            self.assertIsNone(self.adapter.foreground_app())

    def test_app_icon_delegates(self):
        with mock.patch.object(foreground_app, "icon_for_exe", return_value="ICON") as icon:
            self.assertEqual(self.adapter.app_icon("C:/a.exe"), "ICON")
        icon.assert_called_once_with("C:/a.exe")

    def test_base_adapter_defaults(self):
        """前面アプリを調べられない OS では「分からない」を返す。"""
        self.assertIsNone(PlatformAdapter.foreground_app(None))
        self.assertIsNone(PlatformAdapter.app_icon(None, "C:/a.exe"))


class TestDefaults(unittest.TestCase):
    """追加した設定の既定値（Mac 版と同じ）。"""

    def test_defaults(self):
        self.assertEqual(DEFAULT_CONFIG["repaste_key"], "<ctrl>+<alt>+<v>")
        self.assertIs(DEFAULT_CONFIG["translate_input_enabled"], False)
        self.assertEqual(DEFAULT_CONFIG["translate_input_target"], "en")
        self.assertEqual(DEFAULT_CONFIG["translate_input_model"], "qwen/qwen3.8-27b")
        self.assertEqual(DEFAULT_CONFIG["translate_once_key"], "<shift_r>")


if __name__ == "__main__":
    unittest.main()
