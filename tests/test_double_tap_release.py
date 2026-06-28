"""長押し離鍵で誤って auto_enter が付かないことの検証（#19）。

ダブルタップ判定は「直前の離鍵が _DOUBLE_TAP_SEC 内の短いタップ」だったかで決まる。
リリース情報（_last_release_time/_last_release_slot）を長押しの離鍵でも記録すると、
長い録音の直後に始めた録音まで誤って auto_enter 扱いになる。

VoicekeyApp 全体を構築せず、_on_release / _on_press を SimpleNamespace の
ダミー self で unbound 呼び出ししてロジックだけを検証する（test_handsfree_logic と同様）。
"""

import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from src.app import VoicekeyApp, _DOUBLE_TAP_SEC
from src.config.types import HotkeyMode


class _Slot:
    def __init__(self):
        self.required_keys = {"f2"}


def _fake_app(recording_started_ago: float):
    """_on_release を駆動できる最小のダミー self を作る。

    recording_started_ago: 録音開始から離鍵までの経過秒（短ければ short tap）。
    """
    slot = _Slot()
    app = SimpleNamespace(
        _platform=SimpleNamespace(normalize_listener_key=lambda k: k),
        _pressed_keys={"f2"},
        _recording_slot=1,
        _slots={1: slot},
        _recording_effective_mode=HotkeyMode.HOLD.value,
        _auto_enter=False,
        _recording_started=time.monotonic() - recording_started_ago,
        _last_release_time=0.0,
        _last_release_slot=None,
        _state_lock=threading.Lock(),
        _pending_tap_timer=None,
        # 実体を注入（staticmethod/通常メソッドは unbound 呼び出し）
        _acceptable_names=VoicekeyApp._acceptable_names,
        _finish_recording=mock.Mock(),
    )
    app._key_in_slot = lambda key_str, s: VoicekeyApp._key_in_slot(app, key_str, s)
    return app


class TestLongPressDoesNotRecordRelease(unittest.TestCase):
    def test_short_tap_records_release(self):
        app = _fake_app(recording_started_ago=0.05)  # 短いタップ
        # タイマーは即発火させない（start を no-op に）。short tap は待機に入る
        with mock.patch.object(threading, "Timer") as TimerCls:
            TimerCls.return_value = mock.Mock()
            VoicekeyApp._on_release(app, "f2")

        # short tap として成立 → リリース情報が記録される
        self.assertEqual(app._last_release_slot, 1)
        self.assertGreater(app._last_release_time, 0.0)
        # 短いタップは即停止しない（2 打目を待つ）
        app._finish_recording.assert_not_called()

    def test_long_press_does_not_record_release(self):
        app = _fake_app(recording_started_ago=_DOUBLE_TAP_SEC + 0.2)  # 長押し
        VoicekeyApp._on_release(app, "f2")

        # 長押し離鍵ではリリース情報を更新しない（#19）
        self.assertIsNone(app._last_release_slot)
        self.assertEqual(app._last_release_time, 0.0)
        # 長押しは即停止する
        app._finish_recording.assert_called_once()

    def test_press_after_long_press_is_not_auto_enter(self):
        """長押し→即次録音では auto_enter が付かない（_on_press 経路の総合確認）。"""
        # 長押しの離鍵（リリース情報を記録しない）
        app = _fake_app(recording_started_ago=_DOUBLE_TAP_SEC + 0.2)
        VoicekeyApp._on_release(app, "f2")

        # 続けて新規録音を押下（recording_slot を空にして _on_press を駆動）
        app._recording_slot = None
        app._pressed_keys = set()
        app._slot_matches = lambda s: VoicekeyApp._slot_matches(app, s)
        app._handsfree_keys = set()
        app._handsfree_pressed = lambda: VoicekeyApp._handsfree_pressed(app)
        captured = {}

        def _begin(slot_id, auto_enter, effective_mode):
            captured["auto_enter"] = auto_enter

        app._begin_recording = _begin
        # スロットの hotkey_mode 属性（effective_mode 算出に使う）
        app._slots[1].hotkey_mode = HotkeyMode.HOLD.value

        VoicekeyApp._on_press(app, "f2")

        self.assertIn("auto_enter", captured)
        self.assertFalse(captured["auto_enter"])  # 長押し直後は auto_enter にならない

    def test_press_after_short_tap_is_auto_enter(self):
        """短いタップで停止 → _DOUBLE_TAP_SEC 内の再押下は auto_enter（正常動作の維持）。"""
        app = _fake_app(recording_started_ago=0.05)
        with mock.patch.object(threading, "Timer") as TimerCls:
            TimerCls.return_value = mock.Mock()
            VoicekeyApp._on_release(app, "f2")
        # short tap でリリース情報が記録された状態で次の押下
        app._recording_slot = None
        app._pressed_keys = set()
        app._slot_matches = lambda s: VoicekeyApp._slot_matches(app, s)
        app._handsfree_keys = set()
        app._handsfree_pressed = lambda: VoicekeyApp._handsfree_pressed(app)
        app._slots[1].hotkey_mode = HotkeyMode.HOLD.value
        captured = {}
        app._begin_recording = lambda sid, ae, em: captured.update(auto_enter=ae)

        VoicekeyApp._on_press(app, "f2")

        self.assertTrue(captured["auto_enter"])  # 短タップ直後の再押下は auto_enter


if __name__ == "__main__":
    unittest.main()
