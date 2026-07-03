"""StreamingTranscriber のロジックテスト（WebSocket は張らない）。

_handle によるメッセージ解析・PCM 変換・言語解決・接続前 finish の挙動を、
ネットワークなしで検証する。
"""

import json
import struct
import threading
import time
import unittest
from unittest.mock import patch

import numpy as np

import src.core.streaming_transcriber as stream_mod
from src.core.streaming_transcriber import StreamingTranscriber, _float32_to_pcm16le


class TestPcmConversion(unittest.TestCase):
    def test_pcm16le_and_clip(self):
        # 範囲外（2.0 / -2.0）はクリップされ、リトルエンディアン int16 になる
        data = _float32_to_pcm16le(np.array([0.0, 1.0, -1.0, 2.0, -2.0], dtype=np.float32))
        self.assertEqual(struct.unpack("<5h", data), (0, 32767, -32767, 32767, -32767))

    def test_empty_input(self):
        self.assertEqual(_float32_to_pcm16le(np.array([], dtype=np.float32)), b"")


class TestStreamLanguage(unittest.TestCase):
    def test_nova3_keeps_ja(self):
        # multi は日本語を韓国語等に誤判定するため廃止（nova-3 は ja サポート済み）
        self.assertEqual(StreamingTranscriber("nova-3", "ja")._stream_language, "ja")

    def test_nova2_keeps_language(self):
        self.assertEqual(StreamingTranscriber("nova-2", "ja")._stream_language, "ja")
        self.assertEqual(StreamingTranscriber("nova-2", "en")._stream_language, "en")

    def test_empty_language_defaults_ja(self):
        self.assertEqual(StreamingTranscriber("nova-2", "")._stream_language, "ja")


class TestHandle(unittest.TestCase):
    @staticmethod
    def _result(transcript, is_final):
        return json.dumps({
            "is_final": is_final,
            "channel": {"alternatives": [{"transcript": transcript}]},
        })

    def test_interim_then_final(self):
        seen = []
        st = StreamingTranscriber("nova-3", "ja", on_interim=seen.append)
        st._handle(self._result("こんにち", is_final=False))
        self.assertEqual(st._interim, "こんにち")
        st._handle(self._result("こんにちは", is_final=True))
        self.assertEqual(st._finals, ["こんにちは"])
        self.assertEqual(st._interim, "")
        self.assertEqual(st._current_text(), "こんにちは")
        self.assertTrue(seen, "on_interim が呼ばれていない")

    def test_finals_accumulate(self):
        st = StreamingTranscriber("nova-3", "ja")
        st._handle(self._result("一つ目", is_final=True))
        st._handle(self._result("二つ目", is_final=True))
        self.assertEqual(st._current_text(), "一つ目二つ目")

    def test_metadata_resolves_finish(self):
        st = StreamingTranscriber("nova-3", "ja")
        self.assertFalse(st._done_event.is_set())
        st._handle(json.dumps({"type": "Metadata"}))
        self.assertTrue(st._done_event.is_set())

    def test_empty_alternatives_ignored(self):
        st = StreamingTranscriber("nova-3", "ja")
        st._handle(json.dumps({"is_final": True, "channel": {"alternatives": []}}))
        self.assertEqual(st._finals, [])


class TestStartRouting(unittest.TestCase):
    """段階3: start() のキー/ログイン分岐と _run への引き渡しを検証する。"""

    def test_logged_in_starts_without_key(self):
        """ログイン済みなら API キーが無くてもストリーミングを開始できる。"""
        st = StreamingTranscriber("nova-3", "ja")
        captured = {}

        def fake_thread(target=None, args=(), **kw):
            captured["args"] = args

            class _T:
                def start(self_inner):
                    captured["started"] = True

            return _T()

        with patch.object(stream_mod.backend_client, "is_logged_in", return_value=True), \
                patch.object(st, "_resolve_api_key", return_value=None), \
                patch.object(stream_mod.threading, "Thread", side_effect=fake_thread):
            self.assertTrue(st.start())
        # _run(key=None, logged_in=True) で起動する（Bearer JWT 経路）
        self.assertEqual(captured["args"], (None, True))
        self.assertTrue(captured.get("started"))

    def test_not_logged_in_without_key_fails(self):
        """未ログインかつキーも無ければ開始しない（呼び出し側は REST フォールバック）。"""
        st = StreamingTranscriber("nova-3", "ja")
        with patch.object(stream_mod.backend_client, "is_logged_in", return_value=False), \
                patch.object(st, "_resolve_api_key", return_value=None):
            self.assertFalse(st.start())

    def test_not_logged_in_with_key_passes_token_path(self):
        """未ログインでもキーがあれば従来の Token 経路（logged_in=False）で起動する。"""
        st = StreamingTranscriber("nova-3", "ja")
        captured = {}

        def fake_thread(target=None, args=(), **kw):
            captured["args"] = args

            class _T:
                def start(self_inner):
                    pass

            return _T()

        with patch.object(stream_mod.backend_client, "is_logged_in", return_value=False), \
                patch.object(st, "_resolve_api_key", return_value="dg-key"), \
                patch.object(stream_mod.threading, "Thread", side_effect=fake_thread):
            self.assertTrue(st.start())
        self.assertEqual(captured["args"], ("dg-key", False))


class TestFinishWithoutConnection(unittest.TestCase):
    def test_returns_empty_and_cancels(self):
        st = StreamingTranscriber("nova-3", "ja")
        # 接続前の finish は猶予後に空文字を返し、セッションを cancelled にする
        original = stream_mod._CONNECT_GRACE_SEC
        stream_mod._CONNECT_GRACE_SEC = 0.05  # テスト高速化
        try:
            self.assertEqual(st.finish(timeout=0.1), "")
        finally:
            stream_mod._CONNECT_GRACE_SEC = original
        self.assertTrue(st._cancelled)


class TestSendNonBlocking(unittest.TestCase):
    """item6: 音声コールバック(send)が遅い WebSocket でも即 return し、ロック/ I/O を持たない。

    send() は固定上限キューへノンブロッキング投入するだけで、実送信は専用 sender
    スレッドが行う。キュー溢れ/送信失敗は streaming 失敗として記録し、finish() が
    空文字を返して保持済み全音声での REST フォールバックへ移すことを検証する。
    """

    @staticmethod
    def _chunk(n: int = 160):
        return np.zeros(n, dtype=np.float32)

    def test_send_queues_without_touching_socket(self):
        # send() は ws.send を直接呼ばず、キューへ積むだけ（ws 未接続でも積める）
        st = StreamingTranscriber("nova-3", "ja")
        st.send(self._chunk())
        self.assertEqual(st._send_q.qsize(), 1)

    def test_send_immediate_even_with_slow_sender(self):
        # 専用 sender が遅い ws.send でブロックしていても、producer の send() は即返る
        st = StreamingTranscriber("nova-3", "ja")
        release = threading.Event()
        sent = []

        class SlowWS:
            def send(self, data):
                sent.append(data)
                release.wait(2.0)  # 1件目で長くブロックして「遅い回線」を模す

        # 先に1件積んでおき、sender がそれを掴んで ws.send でブロックする状態を作る
        st._send_q.put_nowait(b"first")
        sender = threading.Thread(target=st._sender_loop, args=(SlowWS(),), daemon=True)
        sender.start()
        for _ in range(200):
            if sent:
                break
            time.sleep(0.005)
        self.assertTrue(sent, "sender が送信を開始していない")

        # producer: sender がブロック中でも put_nowait なので即返るはず
        t0 = time.monotonic()
        for _ in range(10):
            st.send(self._chunk())
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 0.2, f"send() がブロックしている: {elapsed:.3f}s")
        self.assertEqual(st._send_q.qsize(), 10)

        # 後始末: closed を立てて release し sender を終了させる
        st._closed = True
        release.set()
        sender.join(timeout=1.0)

    def test_queue_overflow_marks_failed_and_finish_empty(self):
        st = StreamingTranscriber("nova-3", "ja")
        # sender を起動せずキューを上限まで満たす → 次の send が溢れる
        for _ in range(stream_mod._SEND_QUEUE_MAX):
            st._send_q.put_nowait(b"x")
        st.send(self._chunk())
        self.assertTrue(st._failed)
        self.assertTrue(st._closed)
        self.assertTrue(st._done_event.is_set())
        # finish は失敗時 空文字（REST フォールバックの task.audio_data に委ねる）
        self.assertEqual(st.finish(timeout=0.05), "")

    def test_send_is_noop_after_failed(self):
        st = StreamingTranscriber("nova-3", "ja")
        st._failed = True
        st.send(self._chunk())
        self.assertEqual(st._send_q.qsize(), 0)

    def test_sender_send_failure_marks_failed(self):
        st = StreamingTranscriber("nova-3", "ja")

        class BoomWS:
            def send(self, data):
                raise OSError("boom")

        st._send_q.put_nowait(b"pcm")
        st._sender_loop(BoomWS())  # 1件目で例外 → _mark_failed → return
        self.assertTrue(st._failed)
        self.assertTrue(st._done_event.is_set())

    def test_sender_close_stream_marker_sends_and_exits(self):
        st = StreamingTranscriber("nova-3", "ja")
        sent = []

        class WS:
            def send(self, data):
                sent.append(data)

        st._send_q.put_nowait(b"pcm1")
        st._send_q.put_nowait(stream_mod._CLOSE_STREAM)
        st._send_q.put_nowait(b"pcm-after")  # CloseStream 後は送られない
        st._sender_loop(WS())  # CloseStream を送って return するはず
        self.assertEqual(sent[0], b"pcm1")
        self.assertEqual(json.loads(sent[1])["type"], "CloseStream")
        self.assertEqual(len(sent), 2, "CloseStream 後にも送信している")


if __name__ == "__main__":
    unittest.main()
