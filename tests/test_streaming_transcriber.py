"""StreamingTranscriber のロジックテスト（WebSocket は張らない）。

_handle によるメッセージ解析・PCM 変換・言語解決・接続前 finish の挙動を、
ネットワークなしで検証する。
"""

import json
import struct
import unittest

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
    def test_nova3_is_multi(self):
        self.assertEqual(StreamingTranscriber("nova-3", "ja")._stream_language, "multi")

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


if __name__ == "__main__":
    unittest.main()
