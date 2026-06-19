"""音声録音（src/core/audio_recorder.py）の停止ロジックのテスト。

録音末尾の取りこぼし防止: stream.stop() は内部バッファの callback 完了を待つ。
その間に届く最後のチャンクを拾えるよう、_recording フラグは stop() の「後」で
False にしなければならない（先に False にすると末尾チャンクが callback で捨てられる）。

__init__ は sounddevice import と制御スレッド起動を伴うため、停止ロジックだけを
検証する目的でインスタンスを素に生成する（object.__new__）。
"""

import queue
import sys
import types
import unittest

import numpy as np

from src.core.audio_recorder import AudioRecorder


class TestDoStopTailChunk(unittest.TestCase):
    """_do_stop が録音末尾チャンクを取りこぼさないことを検証する。"""

    def _bare_recorder(self):
        rec = object.__new__(AudioRecorder)
        rec.sample_rate = 16000
        rec._audio_q = queue.Queue()
        rec._recording = True
        rec._stream = None
        rec._pending_stop_cb = None
        rec._session_id = 1
        return rec

    def test_recording_flag_stays_true_during_stop_and_tail_is_captured(self):
        """stop() 実行中も _recording が True で、その間のチャンクが回収される。"""
        rec = self._bare_recorder()
        observed = {}

        class _FakeStream:
            def stop(self_inner):
                # 実 audio callback は _recording / session を見てから enqueue する。
                # その判定を模擬: ここで True なら末尾チャンクが拾われる
                observed["recording_during_stop"] = rec._recording
                if rec._recording and rec._session_id == 1:
                    rec._audio_q.put(np.ones(160, dtype=np.float32))

        rec._stream = _FakeStream()
        captured = {}
        rec._do_stop(lambda audio: captured.__setitem__("audio", audio))

        # 取りこぼし防止: stop() 中はまだ録音中フラグが立っている
        self.assertTrue(observed["recording_during_stop"])
        # stop() の後にだけ False になる
        self.assertFalse(rec._recording)
        # stop() 中に届いた末尾チャンクが確定音声に含まれている
        self.assertEqual(len(captured["audio"]), 160)

    def test_not_recording_is_noop(self):
        """既に録音中でなければ何も確定しない（空音声を返す）。"""
        rec = self._bare_recorder()
        rec._recording = False
        captured = {}
        rec._do_stop(lambda audio: captured.__setitem__("audio", audio))
        self.assertEqual(len(captured["audio"]), 0)


class _FakeStream:
    """sd.InputStream の最小モック。start/stop は状態フラグのみ、feed で
    PortAudio コールバックの到来を模擬する（実マイク無しで callback 経路を検証する）。"""

    def __init__(self, **kwargs):
        self._callback = kwargs.get("callback")
        self.started = False

    def start(self):
        self.started = True

    def stop(self):
        self.started = False

    def close(self):
        pass

    def feed(self, indata):
        """1 回ぶんの音声 callback を発火させる。"""
        self._callback(indata, len(indata), None, None)


class TestPersistentStreamSession(unittest.TestCase):
    """永続ストリームで 2 回目以降の録音も音声を拾えること（session_id バグの回帰）。

    バグ: callback の my_session は「ストリームを開いた時点」で固定されるのに、
    _do_start が録音のたびに session を +1 していた。結果、1 回目は偶然一致するが
    2 回目以降は session 不一致で callback が全音声を破棄し、無音になっていた。
    """

    def setUp(self):
        # _do_start は遅延 import するため、sounddevice をモックに差し替える
        self._orig_sd = sys.modules.get("sounddevice")
        fake = types.ModuleType("sounddevice")
        fake.InputStream = _FakeStream
        sys.modules["sounddevice"] = fake

    def tearDown(self):
        if self._orig_sd is not None:
            sys.modules["sounddevice"] = self._orig_sd
        else:
            sys.modules.pop("sounddevice", None)

    def _bare_recorder(self):
        rec = object.__new__(AudioRecorder)
        rec.sample_rate = 16000
        rec._audio_q = queue.Queue()
        rec._recording = False
        rec._stream = None
        rec._stream_device = None
        rec._input_device = None
        rec._session_id = 0
        rec._callback_logged = False
        rec._pending_stop_cb = None
        rec._last_record_end = 0.0
        rec._last_level_time = 0.0
        rec._level_callback = None
        rec.chunk_callback = None
        return rec

    def test_records_on_first_and_second_recording(self):
        """同じ永続ストリームで 1 回目も 2 回目も音声が回収される。"""
        rec = self._bare_recorder()
        indata = np.ones((160, 1), dtype=np.float32)

        # 1 回目
        rec._do_start(None)
        stream = rec._stream
        self.assertIsNotNone(stream)
        stream.feed(indata)
        first = rec._drain_audio()
        self.assertEqual(len(first), 160, "1 回目の録音で音声が拾えていない")

        # 停止しても永続ストリームは閉じない
        rec._do_stop(lambda audio: None)
        self.assertIs(rec._stream, stream, "永続ストリームが閉じられてしまった")

        # 2 回目（同じストリーム・同じ callback）
        rec._do_start(None)
        self.assertIs(rec._stream, stream, "2 回目で別ストリームになっている")
        stream.feed(indata)
        second = rec._drain_audio()
        # バグがあると session 不一致で callback が全データを捨て、ここが 0 になる
        self.assertEqual(
            len(second), 160,
            "2 回目以降の録音で音声が拾えていない（session_id バグの再発）",
        )


if __name__ == "__main__":
    unittest.main()
