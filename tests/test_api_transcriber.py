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
from src.core.backend_client import BackendError


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


class TestServerRouting(unittest.TestCase):
    """段階3: ログイン済みはサーバー経路で文字起こしする（並存ガード）。"""

    @staticmethod
    def _audio():
        # 非空の音声（is_logged_in 判定まで到達させる）
        return np.full(1600, 0.1, dtype=np.float32)

    def test_elevenlabs_uses_proxy_when_logged_in(self):
        """正確性（ElevenLabs）はログイン時サーバープロキシを使い直叩きしない。"""
        t = ElevenLabsTranscriber("scribe_v1", "ja")
        with patch.object(api_transcriber.backend_client, "is_logged_in", return_value=True), \
                patch.object(api_transcriber.backend_client, "transcribe_elevenlabs",
                             return_value="プロキシ結果") as proxy, \
                patch.object(t, "_get_client") as direct:
            self.assertEqual(t.transcribe(self._audio()), "プロキシ結果")
            proxy.assert_called_once()
            self.assertEqual(proxy.call_args.args[1], "ja")  # language を渡す
            direct.assert_not_called()

    def test_elevenlabs_backend_error_becomes_transcription_error(self):
        """プロキシ失敗は TranscriptionError へ写す（呼び出し側の通知に乗る）。"""
        t = ElevenLabsTranscriber("scribe_v1", "ja")
        with patch.object(api_transcriber.backend_client, "is_logged_in", return_value=True), \
                patch.object(api_transcriber.backend_client, "transcribe_elevenlabs",
                             side_effect=BackendError("だめ", status=403)):
            with self.assertRaises(TranscriptionError):
                t.transcribe(self._audio())

    def test_deepgram_uses_jwt_when_logged_in(self):
        """高速リアルタイム（Deepgram）はログイン時 短命 JWT で直叩き（低レイテンシ維持）。"""
        t = DeepgramTranscriber("nova-3", "ja")
        captured = {}

        def fake_post(url, **kwargs):
            captured["url"] = url
            captured["auth"] = kwargs["headers"]["Authorization"]
            return httpx.Response(
                200,
                json={"results": {"channels": [{"alternatives": [{"transcript": "本日は晴天"}]}]}},
            )

        with patch.object(api_transcriber.backend_client, "is_logged_in", return_value=True), \
                patch.object(api_transcriber.backend_client, "fetch_ephemeral_token",
                             return_value={"token": "dg-jwt"}), \
                patch.object(api_transcriber.httpx, "post", side_effect=fake_post), \
                patch.object(t, "_get_client") as direct:
            self.assertEqual(t.transcribe(self._audio()), "本日は晴天")
            self.assertEqual(captured["auth"], "Bearer dg-jwt")  # Token ではなく Bearer
            self.assertTrue(captured["url"].endswith("/listen"))
            direct.assert_not_called()  # キャッシュ済み Token クライアントはバイパス

    def test_deepgram_token_error_becomes_transcription_error(self):
        """短命トークン取得失敗は TranscriptionError へ写す。"""
        t = DeepgramTranscriber("nova-3", "ja")
        with patch.object(api_transcriber.backend_client, "is_logged_in", return_value=True), \
                patch.object(api_transcriber.backend_client, "fetch_ephemeral_token",
                             side_effect=BackendError("無効", status=403)):
            with self.assertRaises(TranscriptionError):
                t.transcribe(self._audio())


if __name__ == "__main__":
    unittest.main()
