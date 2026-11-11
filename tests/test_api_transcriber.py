"""ApiTranscriber 群（OpenAI/Groq/ElevenLabs/Deepgram）のロジックテスト。

認証ヘッダー・言語解決・空音声・ステータス検査を、ネットワークなしで検証する。
"""

import unittest
from unittest.mock import patch

import httpx
import numpy as np

from src.core import api_transcriber
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
    def test_nova3_keeps_ja(self):
        # multi は日本語を韓国語等に誤判定するため廃止（nova-3 は ja サポート済み）
        self.assertEqual(DeepgramTranscriber("nova-3", "ja")._dg_language, "ja")

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


class TestDirectRouting(unittest.TestCase):
    """文字起こしは常に利用者のキーでプロバイダーを直接叩く。"""

    @staticmethod
    def _audio():
        return np.full(1600, 0.1, dtype=np.float32)

    def test_deepgram_posts_to_listen(self):
        """Deepgram は /listen へ直接 POST し、返った transcript をそのまま返す。"""
        t = DeepgramTranscriber("nova-3", "ja")
        captured = {}

        def fake_post(client, path, **kwargs):
            captured["path"] = path
            return httpx.Response(
                200, json={"results": {"channels": [{"alternatives": [{"transcript": "開発"}]}]}}
            )

        with patch.object(t, "_get_client", return_value=object()), \
                patch.object(t, "_post", side_effect=fake_post):
            self.assertEqual(t.transcribe(self._audio()), "開発")
            self.assertEqual(captured["path"], "/listen")


class TestWhisperPrompt(unittest.TestCase):
    """Whisper（Groq/OpenAI）へ数字を半角で出させる style プロンプトを付与する。"""

    @staticmethod
    def _audio():
        return np.full(1600, 0.1, dtype=np.float32)

    def _capture_direct_data(self, transcriber):
        """Whisper 系の直叩きで送る multipart data を捕捉して返す。"""
        captured = {}

        def fake_post(client, path, **kwargs):
            captured["data"] = kwargs.get("data")
            return httpx.Response(200, text="結果")

        with patch.object(transcriber, "_get_client", return_value=object()), \
                patch.object(transcriber, "_post", side_effect=fake_post):
            transcriber.transcribe(self._audio())
        return captured["data"]

    def test_groq_direct_includes_numeral_prompt(self):
        data = self._capture_direct_data(GroqTranscriber("whisper-large-v3-turbo", "ja"))
        self.assertEqual(data["prompt"], api_transcriber._NUMERAL_STYLE_PROMPT)

    def test_openai_direct_includes_numeral_prompt(self):
        data = self._capture_direct_data(OpenAITranscriber("gpt-4o-transcribe", "ja"))
        self.assertEqual(data["prompt"], api_transcriber._NUMERAL_STYLE_PROMPT)

    def test_user_prompt_is_appended_after_hint(self):
        t = GroqTranscriber("whisper-large-v3-turbo", "ja", prompt="固有名詞: VoiceKey")
        data = self._capture_direct_data(t)
        self.assertTrue(data["prompt"].startswith(api_transcriber._NUMERAL_STYLE_PROMPT))
        self.assertIn("固有名詞: VoiceKey", data["prompt"])


if __name__ == "__main__":
    unittest.main()
