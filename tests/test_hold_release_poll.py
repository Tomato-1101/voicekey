"""押している間だけ録音する回で、離鍵の取りこぼしを見回りで拾うかのテスト（Windows）。

バグ: キーボードフックが録音キーの離鍵を落とすと、押している間だけ録音するモードなのに
上限（300 秒）まで録音が続いていた。録音中だけ GetAsyncKeyState で押下状態を見回り、
「離れている」が 2 回続いたら離鍵とみなして止める。

GetAsyncKeyState（_win_key_down）と time.sleep は偽物に差し替え、実際のキーは読まない。
"""

import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from src import app as app_mod
from src.app import VoicekeyApp, _HOLD_POLL_MISSES, _win_key_down
from src.config import HotkeyMode
from tests.test_dictation_extras import _FakeConfig, _FakeTranscriber, _InlineThread, _slot

_CTRL_R = 0xA3


def _watch_self(gen=1, pending_tap=False):
    """_watch_hold_release が見る属性だけを持つ偽 self。"""
    s = SimpleNamespace(
        _state_lock=threading.Lock(),
        _active_context=SimpleNamespace(gen=gen),
        _recording_slot=1,
        _pending_tap_timer=mock.Mock() if pending_tap else None,
        _pressed_keys={"ctrl_r", "a"},
        _acceptable_names=VoicekeyApp._acceptable_names,
    )

    def finish(expected_gen=None):
        s._recording_slot = None
        s._active_context = None

    s._finish_recording = mock.Mock(side_effect=finish)
    return s


class TestWatchHoldRelease(unittest.TestCase):
    def _run(self, s, key_states, max_polls=10):
        """key_states を 1 周ごとの押下状態として見回りを回す。max_polls 周で録音を終わらせる。"""
        states = iter(key_states)
        polls = []

        def fake_sleep(_sec):
            polls.append(_sec)
            if len(polls) > max_polls:
                s._active_context = None  # 録音が終わった扱いで見回りを抜けさせる

        with mock.patch.object(app_mod.time, "sleep", side_effect=fake_sleep), \
             mock.patch.object(app_mod, "_win_key_down", side_effect=lambda vk: next(states, True)):
            VoicekeyApp._watch_hold_release(s, 1, {"ctrl_r": (_CTRL_R,)})
        return polls

    def test_two_misses_stop_recording(self):
        s = _watch_self()
        polls = self._run(s, [False, False])
        self.assertEqual(len(polls), _HOLD_POLL_MISSES)
        s._finish_recording.assert_called_once_with(expected_gen=1)
        # 押したままの記録を消し、次に押したときキーリピート扱いで無視されないようにする
        self.assertEqual(s._pressed_keys, {"a"})

    def test_single_miss_does_not_stop(self):
        """1 回だけ「離れている」と読めても、押下が戻れば止めない。"""
        s = _watch_self()
        self._run(s, [False, True, False, True, False, True], max_polls=6)
        s._finish_recording.assert_not_called()
        self.assertIn("ctrl_r", s._pressed_keys)

    def test_waiting_second_tap_is_skipped(self):
        """ダブルタップの 2 打目待ちはキーが離れているのが正しいので止めない。"""
        s = _watch_self(pending_tap=True)
        self._run(s, [False] * 10, max_polls=5)
        s._finish_recording.assert_not_called()

    def test_other_recording_ends_watch(self):
        """見回り中に別の録音へ替わったら、何も読まずに抜ける。"""
        s = _watch_self(gen=2)
        down = mock.Mock(return_value=False)
        with mock.patch.object(app_mod.time, "sleep"), \
             mock.patch.object(app_mod, "_win_key_down", down):
            VoicekeyApp._watch_hold_release(s, 1, {"ctrl_r": (_CTRL_R,)})
        down.assert_not_called()
        s._finish_recording.assert_not_called()


class TestFinishRecordingGeneration(unittest.TestCase):
    def test_other_generation_is_not_stopped(self):
        """見回りが判定した直後に別の録音が始まっていたら、その録音は止めない。"""
        s = SimpleNamespace(
            _state_lock=threading.Lock(),
            _recording_slot=1,
            _active_context=SimpleNamespace(gen=2),
        )
        VoicekeyApp._finish_recording(s, expected_gen=1)
        self.assertEqual(s._recording_slot, 1)
        self.assertEqual(s._active_context.gen, 2)


class TestStartHoldReleaseWatch(unittest.TestCase):
    def _begin_self(self):
        slot = _slot(_FakeTranscriber(), hotkey="<ctrl_r>")
        slot.transcriber.prewarm = mock.Mock()
        s = SimpleNamespace(
            _state_lock=threading.Lock(),
            _recording_slot=None,
            _recording_effective_mode=HotkeyMode.HOLD.value,
            _recording_started=0.0,
            _auto_enter=False,
            _active_streamer=None,
            _active_context=None,
            _generation=0,
            _translate_once=False,
            _translate_once_keys=set(),
            _practice_format_override=False,
            _slots={1: slot},
            _config=_FakeConfig({"sound_effects_enabled": False, "duck_media_enabled": False,
                                 "streaming_enabled": False}),
            _recorder=mock.Mock(),
            _emit_state=mock.Mock(),
            _capture_target_app=mock.Mock(),
            translate_badge_changed=mock.Mock(),
            _start_hold_release_watch=mock.Mock(),
            _handsfree_transcriber=_FakeTranscriber(),
        )
        s._handsfree_transcriber.prewarm = mock.Mock()
        s._maybe_handsfree_slot = VoicekeyApp._maybe_handsfree_slot.__get__(s)
        s._snapshot_context = VoicekeyApp._snapshot_context.__get__(s)
        return s

    def _begin(self, s, platform, mode):
        with mock.patch.object(app_mod.threading, "Thread", _InlineThread), \
             mock.patch.object(app_mod.sys, "platform", platform):
            VoicekeyApp._begin_recording(s, 1, False, mode)

    def test_windows_hold_starts_watch(self):
        s = self._begin_self()
        self._begin(s, "win32", HotkeyMode.HOLD.value)
        s._start_hold_release_watch.assert_called_once_with(1, {"ctrl_r"})

    def test_toggle_does_not_watch(self):
        """押し直して止めるモードは離鍵で止めないので見回らない。"""
        s = self._begin_self()
        self._begin(s, "win32", HotkeyMode.TOGGLE.value)
        s._start_hold_release_watch.assert_not_called()

    def test_other_os_does_not_watch(self):
        s = self._begin_self()
        self._begin(s, "darwin", HotkeyMode.HOLD.value)
        s._start_hold_release_watch.assert_not_called()

    def test_keys_without_vk_are_not_watched(self):
        """押下状態を読めないキー（IME 系）は見回らず、読めるキーだけ見回る。"""
        s = SimpleNamespace(_watch_hold_release=mock.Mock())
        with mock.patch.object(app_mod.threading, "Thread", _InlineThread):
            VoicekeyApp._start_hold_release_watch(s, 1, {"nonconvert"})
            s._watch_hold_release.assert_not_called()
            VoicekeyApp._start_hold_release_watch(s, 1, {"ctrl_r", "nonconvert"})
        s._watch_hold_release.assert_called_once_with(1, {"ctrl_r": (_CTRL_R,)})


class TestWinKeyDown(unittest.TestCase):
    def test_reads_high_bit(self):
        user32 = mock.Mock()
        with mock.patch("ctypes.windll", SimpleNamespace(user32=user32), create=True):
            user32.GetAsyncKeyState.return_value = 0x8000
            self.assertTrue(_win_key_down(_CTRL_R))
            # 下位ビットは「前回の問い合わせ以降に押された」印で、今の押下ではない
            user32.GetAsyncKeyState.return_value = 0x0001
            self.assertFalse(_win_key_down(_CTRL_R))
        user32.GetAsyncKeyState.assert_called_with(_CTRL_R)


if __name__ == "__main__":
    unittest.main()
