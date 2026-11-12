"""録音開始失敗時のストリーミング後始末（VoicekeyApp._on_record_started）のテスト。

バグ: _on_record_started(False) が録音スロットだけを消し、_active_streamer と
recorder.chunk_callback（= 古い streamer.send）を残していた。その結果、次回に別
バックエンドで録音しても音声が前回の Deepgram WebSocket へ送られ得た。

VoicekeyApp は QObject 派生で素の生成ができないため、_on_record_started が参照する
属性だけを持つ SimpleNamespace を self に見立て、未束縛メソッドとして呼び出す。
"""

import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from src.app import VoicekeyApp


class _FakeRecorder:
    """録音世代束縛のストリーミングフックを持つ最小レコーダ。send 経路の有無を観測する。"""

    def __init__(self):
        self._gen = 0
        self._entry = (0, None)

    def set_chunk_callback(self, callback):
        if callback is None:
            self._entry = (0, None)
            return 0
        self._gen += 1
        self._entry = (self._gen, callback)
        return self._gen

    @property
    def chunk_cb(self):
        """現在登録されている送出コールバック（無ければ None）。"""
        return self._entry[1]


class _FakeStreamer:
    """Deepgram ストリーマのモック。send 呼び出しと cancel を観測する。"""

    def __init__(self):
        self.sent = []
        self.cancelled = False

    def send(self, samples):
        self.sent.append(samples)

    def cancel(self):
        self.cancelled = True


class TestRecordStartFailureCleanup(unittest.TestCase):
    def _fake_self(self, streamer, recorder):
        return SimpleNamespace(
            _state_lock=threading.Lock(),
            _recording_slot=1,
            _active_streamer=streamer,
            # 録音開始時の処理コンテキスト（開始成功時に録り始めた時刻を書き込む先）
            _active_context=None,
            _recorder=recorder,
            notice=mock.Mock(),
            _emit_state=mock.Mock(),
            translate_badge_changed=mock.Mock(),
        )

    def setUp(self):
        # 実機のメディア音量に触れないよう、ダッキング解除は偽物にする
        patcher = mock.patch("src.app.media_ducker")
        self.ducker = patcher.start()
        self.addCleanup(patcher.stop)

    def test_start_failure_discards_streamer_and_callback(self):
        """開始失敗で streamer.cancel・chunk_callback 解除・状態クリアが行われる。"""
        streamer = _FakeStreamer()
        recorder = _FakeRecorder()
        recorder.set_chunk_callback(streamer.send)  # 開始前に張られたフック
        s = self._fake_self(streamer, recorder)

        VoicekeyApp._on_record_started(s, False)

        self.assertIsNone(s._recording_slot)
        self.assertIsNone(s._active_streamer)
        self.assertIsNone(recorder.chunk_cb, "古い送出フックが残っている")
        self.assertTrue(streamer.cancelled, "古いストリーマが破棄されていない")

    def test_next_recording_audio_does_not_reach_old_connection(self):
        """後始末後、次回録音の音声が前回の接続へ 1 チャンクも送られない。"""
        old = _FakeStreamer()
        recorder = _FakeRecorder()
        recorder.set_chunk_callback(old.send)
        s = self._fake_self(old, recorder)

        VoicekeyApp._on_record_started(s, False)

        # 次回録音（別バックエンド = streamer を張らない）を模擬：
        # 送出フックは解除済み（None）なので、音声フレームは誰にも送られない
        cb = recorder.chunk_cb
        if cb is not None:
            cb(b"frame-of-next-recording")
        self.assertEqual(old.sent, [], "次回録音の音声が古い接続へ送られた")

    def test_failure_restores_media_and_badge(self):
        """開始失敗でも、下げたメディア音量と翻訳の印を戻す（録音が終わる経路と揃える）。"""
        streamer = _FakeStreamer()
        recorder = _FakeRecorder()
        s = self._fake_self(streamer, recorder)
        s._active_context = SimpleNamespace(gen=3)

        VoicekeyApp._on_record_started(s, False, 3)

        self.ducker.restore.assert_called_once()
        s.translate_badge_changed.emit.assert_called_once_with("")
        self.assertIsNone(s._recording_slot)

    def test_late_failure_of_old_recording_keeps_current_one(self):
        """終わった録音の開始失敗が遅れて届いても、いまの録音を消さない・通知しない。"""
        streamer = _FakeStreamer()
        recorder = _FakeRecorder()
        recorder.set_chunk_callback(streamer.send)
        s = self._fake_self(streamer, recorder)
        s._active_context = SimpleNamespace(gen=5)  # いまの録音は世代 5

        VoicekeyApp._on_record_started(s, False, 4)  # 世代 4 の失敗が遅れて届いた

        self.assertEqual(s._recording_slot, 1, "別の録音のスロットが消された")
        self.assertIs(s._active_streamer, streamer)
        self.assertFalse(streamer.cancelled, "別の録音の接続が切られた")
        self.assertIsNotNone(recorder.chunk_cb, "別の録音の送出フックが外された")
        s.notice.emit.assert_not_called()
        self.ducker.restore.assert_not_called()

    def test_late_success_of_old_recording_does_not_stamp_current_one(self):
        """終わった録音の開始成功が、いまの録音の開始時刻を書き換えない。"""
        s = self._fake_self(_FakeStreamer(), _FakeRecorder())
        ctx = SimpleNamespace(gen=5, record_started_at=None)
        s._active_context = ctx

        VoicekeyApp._on_record_started(s, True, 4)

        self.assertIsNone(ctx.record_started_at)

    def test_success_is_noop(self):
        """開始成功時は何も破棄しない（ストリーミングを継続する）。"""
        streamer = _FakeStreamer()
        recorder = _FakeRecorder()
        recorder.set_chunk_callback(streamer.send)
        s = self._fake_self(streamer, recorder)

        VoicekeyApp._on_record_started(s, True)

        self.assertIs(s._active_streamer, streamer)
        self.assertIsNotNone(recorder.chunk_cb)
        self.assertFalse(streamer.cancelled)



class TestWatchdogHangRecoveryCleanup(unittest.TestCase):
    """音声処理のハング自動復旧（_watchdog_check）でも録音の後片付けが揃うこと。"""

    def test_hang_recovery_restores_media_and_badge(self):
        recorder = mock.Mock()
        recorder.health.return_value = {"busy_op": "start", "busy_sec": 10.0, "alive": True}
        streamer = _FakeStreamer()
        s = SimpleNamespace(
            _state_lock=threading.Lock(),
            _recording_slot=1,
            _recording_started=time.monotonic(),
            _active_streamer=streamer,
            _active_context=SimpleNamespace(gen=1),
            _recorder=recorder,
            notice=mock.Mock(),
            _emit_state=mock.Mock(),
            _finish_recording=mock.Mock(),
            translate_badge_changed=mock.Mock(),
            _listener_thread=SimpleNamespace(is_alive=lambda: True),
            _monitoring=True,
        )
        with mock.patch("src.app.media_ducker") as ducker:
            VoicekeyApp._watchdog_check(s)

        ducker.restore.assert_called_once()
        s.translate_badge_changed.emit.assert_called_once_with("")
        self.assertTrue(streamer.cancelled)
        recorder.recover.assert_called_once()
        self.assertIsNone(s._recording_slot)


if __name__ == "__main__":
    unittest.main()
