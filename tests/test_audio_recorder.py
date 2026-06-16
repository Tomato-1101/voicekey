"""音声録音（src/core/audio_recorder.py）の停止ロジックのテスト。

録音末尾の取りこぼし防止: stream.stop() は内部バッファの callback 完了を待つ。
その間に届く最後のチャンクを拾えるよう、_recording フラグは stop() の「後」で
False にしなければならない（先に False にすると末尾チャンクが callback で捨てられる）。

__init__ は sounddevice import と制御スレッド起動を伴うため、停止ロジックだけを
検証する目的でインスタンスを素に生成する（object.__new__）。
"""

import queue
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


if __name__ == "__main__":
    unittest.main()
