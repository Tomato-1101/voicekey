"""ダブルタップ Enter 自動送信の遅延修正（src/app.py の _on_release）のテスト。

ダブルタップは 2 打目の押下で確定する（auto_enter=True）。確定後の最後の離鍵で
さらに _DOUBLE_TAP_SEC（0.4s）の 2 打目待ちに入ると、短い録音でも Enter 自動送信が
必ず 0.4 秒遅れていた。確定後の離鍵は即座に録音停止すべき。

VoicekeyApp は QObject 派生で素の生成ができないため、_on_release が参照する属性だけを
持つ SimpleNamespace を self に見立て、未束縛メソッドとして呼び出す（QObject 構築を回避）。
"""

import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from src.app import VoicekeyApp
from src.config.types import HotkeyMode


class TestDoubleTapImmediateFinish(unittest.TestCase):
    """auto_enter 確定後の離鍵が待ち窓に入らず即停止することを検証する。"""

    def _fake_self(self, auto_enter: bool):
        platform = mock.Mock()
        platform.normalize_listener_key.return_value = "<shift_r>"
        return SimpleNamespace(
            _state_lock=threading.Lock(),
            _pressed_keys={"<shift_r>"},
            _recording_slot=1,
            _recording_effective_mode=HotkeyMode.HOLD.value,
            _auto_enter=auto_enter,
            _recording_started=0.0,
            _pending_tap_timer=None,
            _last_release_time=0.0,
            _last_release_slot=None,
            _slots={1: object()},
            _platform=platform,
            _key_in_slot=mock.Mock(return_value=True),
            _finish_recording=mock.Mock(),
            _hotkey_test_active=False,   # 通常経路（録音キーテスト OFF）を検証する
        )

    def test_auto_enter_release_finishes_immediately(self):
        """ダブルタップ確定後の離鍵は即 finish し、0.4 秒の待ちタイマーを張らない。"""
        s = self._fake_self(auto_enter=True)
        VoicekeyApp._on_release(s, "dummy_key")
        s._finish_recording.assert_called_once()
        self.assertIsNone(s._pending_tap_timer)

    def test_single_short_tap_still_waits_for_second(self):
        """単発の短いタップは従来どおり 2 打目待ち（冒頭の声を残す設計）を維持する。"""
        s = self._fake_self(auto_enter=False)
        s._recording_started = time.monotonic()  # 直近開始＝短いタップ
        VoicekeyApp._on_release(s, "dummy_key")
        # まだ確定しない（2 打目を待つ）
        s._finish_recording.assert_not_called()
        self.assertIsNotNone(s._pending_tap_timer)
        s._pending_tap_timer.cancel()  # 後始末（0.4 秒後の発火を防ぐ）


if __name__ == "__main__":
    unittest.main()
