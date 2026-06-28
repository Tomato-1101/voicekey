"""録音開始失敗時のストリーミング後始末（VoicekeyApp._on_record_started）のテスト。

バグ: _on_record_started(False) が録音スロットだけを消し、_active_streamer と
recorder.chunk_callback（= 古い streamer.send）を残していた。その結果、次回に別
バックエンドで録音しても音声が前回の Deepgram WebSocket へ送られ得た。

VoicekeyApp は QObject 派生で素の生成ができないため、_on_record_started が参照する
属性だけを持つ SimpleNamespace を self に見立て、未束縛メソッドとして呼び出す。
"""

import threading
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
            _recorder=recorder,
            notice=mock.Mock(),
            _emit_state=mock.Mock(),
        )

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


if __name__ == "__main__":
    unittest.main()
