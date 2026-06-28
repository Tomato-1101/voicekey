"""音声録音（src/core/audio_recorder.py）の停止ロジックのテスト。

録音末尾の取りこぼし防止: stream.stop() は内部バッファの callback 完了を待つ。
その間に届く最後のチャンクを拾えるよう、_recording フラグは stop() の「後」で
False にしなければならない（先に False にすると末尾チャンクが callback で捨てられる）。

__init__ は sounddevice import と制御スレッド起動を伴うため、停止ロジックだけを
検証する目的でインスタンスを素に生成する（object.__new__）。
"""

import queue
import sys
import threading
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
        rec._record_gen = 0
        rec._active_chunk_gen = 0
        rec._chunk_entry = (0, None)
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


class TestRecoverDoubleCompletion(unittest.TestCase):
    """停止ハング→recover→新規録音→旧stop復帰でコールバックが二重発火しない回帰。

    バグ: stream.stop() がハングして recover() が完了コールバックを代行発火した後、
    古い停止スレッドが復帰すると同じコールバックを再度呼べた（callback_count==2）。
    さらに古いスレッドが新世代の _recording / バッファを破壊し得た。
    """

    def _bare_recorder(self):
        rec = object.__new__(AudioRecorder)
        rec.sample_rate = 16000
        rec._audio_q = queue.Queue()
        rec._recording = True
        rec._stream = None
        rec._stream_device = None
        rec._pending_stop_cb = None
        rec._session_id = 1
        rec._generation = 0
        rec._busy_op = None
        rec._busy_since = 0.0
        rec._thread = None
        rec._commands = queue.Queue()
        rec._last_record_end = 0.0
        rec._state_lock = threading.Lock()
        # recover() が実制御スレッドを起動しないように差し替える（PortAudio に触れない）
        rec._start_control_thread = lambda: None
        return rec

    def test_hang_recover_new_recording_old_stop_resume(self):
        rec = self._bare_recorder()

        entered_stop = threading.Event()   # 旧 stop が stream.stop() に入った
        release_stop = threading.Event()   # テストがハングを解除する

        class _HangStream:
            def stop(self_inner):
                entered_stop.set()
                # ハングを模擬: テストが解除するまで戻らない（制御スレッドだけがブロック）
                release_stop.wait(timeout=5)

        rec._stream = _HangStream()

        # 録音済みの旧世代音声（recover が代行発火で配るべきデータ）
        rec._audio_q.put(np.ones((160, 1), dtype=np.float32))

        callbacks = []  # 完了コールバックの呼び出し履歴（音声長を記録）

        def on_audio(audio):
            callbacks.append(len(audio))

        # 旧制御スレッド（世代0）で停止 → stream.stop() でハングする
        t_old = threading.Thread(
            target=rec._do_stop, args=(on_audio, 0), name="OldStop", daemon=True
        )
        t_old.start()
        self.assertTrue(entered_stop.wait(timeout=5), "stop() に入らなかった")

        # ハング中に recover()。世代を進め、pending を代行発火し、新世代に別バッファを割り当てる
        rec.recover()
        self.assertEqual(rec._generation, 1)
        self.assertEqual(callbacks, [160], "recover が旧世代の音声を 1 回配っていない")

        # 新世代の録音を模擬: 新しいバッファ（recover が差し替えたもの）へ音声を積む
        self.assertTrue(rec._recording is False)
        rec._recording = True
        new_q = rec._audio_q
        new_q.put(np.full((320, 1), 0.5, dtype=np.float32))

        # 旧 stop のハングを解除 → 旧スレッドが復帰して finally まで走る
        release_stop.set()
        t_old.join(timeout=5)
        self.assertFalse(t_old.is_alive(), "旧 stop スレッドが終了しない")

        # 二重発火していない（コールバックは合計 1 回だけ）
        self.assertEqual(callbacks, [160], "完了コールバックが二重発火した")
        # 旧スレッドは新世代の _recording を False に戻していない
        self.assertTrue(rec._recording, "古い世代が新世代の録音状態を破壊した")
        # 旧スレッドは新世代のバッファをドレインしていない（新音声がそのまま残る）
        remaining = rec._drain_queue(new_q)
        self.assertEqual(len(remaining), 320, "古い世代が新世代の音声を奪った")


class TestCrossRecordingChunkBinding(unittest.TestCase):
    """item7: 旧録音の stop ドレイン中に次録音が streamer を差し替えても、旧録音末尾の
    チャンクが次録音の streamer へ混入しないことを検証する（連続録音間の音声混入防止）。

    chunk_callback を録音世代に束縛し、_do_start で確定した受理世代（_active_chunk_gen）と
    一致しないチャンクを audio callback が拒否する。受理世代は _do_start でのみ進むため、
    旧録音の stop ドレイン中（_do_start 前）は進まず、差し替えられた新 streamer に渡らない。
    """

    def setUp(self):
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
        rec._record_gen = 0
        rec._active_chunk_gen = 0
        rec._chunk_entry = (0, None)
        return rec

    def test_old_recording_tail_not_sent_to_next_streamer(self):
        rec = self._bare_recorder()
        a_chunks, b_chunks = [], []

        # 録音A: streamerA を世代1で登録 → _do_start で受理世代=1 に確定
        self.assertEqual(rec.set_chunk_callback(lambda s: a_chunks.append(s)), 1)
        rec._do_start(None)
        stream = rec._stream
        self.assertEqual(rec._active_chunk_gen, 1)

        # A 録音中のチャンクは streamerA に届く（世代一致）
        stream.feed(np.ones((160, 1), dtype=np.float32))
        self.assertEqual(len(a_chunks), 1, "A の音声が streamerA に届いていない")

        # 次録音Bが listener スレッドで streamerB を世代2で差し替える（B の _do_start 前）。
        # 受理世代は 1 のままなので、この瞬間に届く「A の末尾チャンク」は誰にも送られない
        self.assertEqual(rec.set_chunk_callback(lambda s: b_chunks.append(s)), 2)
        stream.feed(np.full((160, 1), 0.3, dtype=np.float32))
        self.assertEqual(b_chunks, [], "A の末尾が次録音 streamerB へ混入した")
        self.assertEqual(len(a_chunks), 1, "差し替え後に streamerA へも送られた")

        # A の停止 → B の物理録音開始で受理世代が 2 になり、以降は streamerB に届く
        rec._do_stop(lambda audio: None)
        self.assertFalse(rec._recording)
        rec._do_start(None)
        self.assertEqual(rec._active_chunk_gen, 2)
        stream.feed(np.full((160, 1), 0.5, dtype=np.float32))
        self.assertEqual(len(b_chunks), 1, "B の音声が streamerB に届いていない")

    def test_disarm_blocks_all_delivery(self):
        # set_chunk_callback(None) で解除したら、録音中でも誰にも送られない
        rec = self._bare_recorder()
        got = []
        rec.set_chunk_callback(lambda s: got.append(s))
        rec._do_start(None)
        rec.set_chunk_callback(None)  # 解除（finish 経路）
        rec._stream.feed(np.ones((160, 1), dtype=np.float32))
        self.assertEqual(got, [], "解除後もチャンクが送出された")


if __name__ == "__main__":
    unittest.main()
