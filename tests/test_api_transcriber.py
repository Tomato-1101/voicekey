"""ApiTranscriber 群（OpenAI/Groq/ElevenLabs/Deepgram）のロジックテスト。

認証ヘッダー・言語解決・空音声・ステータス検査を、ネットワークなしで検証する。
"""

import unittest

import httpx
import numpy as np

from src.core.api_transcriber import (
    DeepgramTranscriber,
    ElevenLabsTranscriber,
    GroqTranscriber,
    OpenAITranscriber,
    TranscriptionError,
)


class TestAuthHeaders(unittest.TestCase):
    def test_openai_bearer(self):
        self.assertEqual(OpenAITranscriber("m")._auth_headers("K"), {"Authorization": "Bearer K"})

    def test_groq_bearer(self):
        self.assertEqual(GroqTranscriber("m")._auth_headers("K"), {"Authorization": "Bearer K"})

    def test_deepgram_token(self):
        self.assertEqual(DeepgramTranscriber("nova-3")._auth_headers("K"), {"Authorization": "Token K"})

    def test_elevenlabs_xi_key(self):
        self.assertEqual(ElevenLabsTranscriber("scribe_v1")._auth_headers("K"), {"xi-api-key": "K"})


class TestDeepgramLanguage(unittest.TestCase):
    def test_nova3_multi(self):
        self.assertEqual(DeepgramTranscriber("nova-3", "ja")._dg_language, "multi")

    def test_nova2_keeps(self):
        self.assertEqual(DeepgramTranscriber("nova-2", "ja")._dg_language, "ja")

    def test_empty_defaults_ja(self):
        self.assertEqual(DeepgramTranscriber("nova-2", "")._dg_language, "ja")


class TestEmptyAudio(unittest.TestCase):
    def test_empty_returns_empty_string(self):
        empty = np.array([], dtype=np.float32)
        # ネットワークに到達せず即 "" を返す（キー不要）
        self.assertEqual(DeepgramTranscriber("nova-3").transcribe(empty), "")
        self.assertEqual(ElevenLabsTranscriber("scribe_v1").transcribe(empty), "")
        self.assertEqual(OpenAITranscriber("gpt-4o-mini-transcribe").transcribe(empty), "")


class TestRaiseForStatus(unittest.TestCase):
    def test_errors_raise(self):
        t = OpenAITranscriber("m")
        with self.assertRaises(TranscriptionError):
            t._raise_for_status(httpx.Response(401))
        with self.assertRaises(TranscriptionError):
            t._raise_for_status(httpx.Response(429))
        with self.assertRaises(TranscriptionError):
            t._raise_for_status(httpx.Response(500))

    def test_ok_passes(self):
        t = OpenAITranscriber("m")
        t._raise_for_status(httpx.Response(200))  # 例外を投げない


class TestAvailableModels(unittest.TestCase):
    def test_model_sets(self):
        self.assertIn("nova-3", DeepgramTranscriber.available_models)
        self.assertIn("scribe_v1", ElevenLabsTranscriber.available_models)
        self.assertIn("gpt-4o-transcribe", OpenAITranscriber.available_models)
        self.assertIn("whisper-large-v3-turbo", GroqTranscriber.available_models)


if __name__ == "__main__":
    unittest.main()
