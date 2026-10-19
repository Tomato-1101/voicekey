"""Mac に合わせて追加・変更した文字起こしエンジン（api_transcriber.py）のテスト。

gpt-transcribe の送信形式・ElevenLabs の tag_audio_events・Microsoft MAI・Soniox の再送・
エラー本文を通知に混ぜないこと・キー未設定の案内を、ネットワークなしで検証する。
"""

import unittest
from unittest.mock import patch

import httpx
import numpy as np

from src.core import api_transcriber
from src.core.api_transcriber import (
    AzureMAITranscriber,
    ElevenLabsTranscriber,
    GroqTranscriber,
    OpenAILiveRestTranscriber,
    OpenAITranscriber,
    SonioxTranscriber,
    TranscriptionError,
    azure_base_url,
    azure_definition_json,
    azure_transcribe_url,
    parse_azure_response,
    response_detail,
)


def _audio():
    return np.full(1600, 0.1, dtype=np.float32)


def _direct(transcriber, response, capture):
    """開発直叩きの _post を捕捉する共通パッチ。"""

    def fake_post(client, path, **kwargs):
        capture["path"] = path
        capture.update(kwargs)
        return response

    return [
        patch.object(api_transcriber.secrets, "is_dist_build", return_value=False),
        patch.object(api_transcriber.backend_client, "is_logged_in", return_value=False),
        patch.object(transcriber, "_get_client", return_value=object()),
        patch.object(transcriber, "_post", side_effect=fake_post),
    ]


class TestGptTranscribeFormat(unittest.TestCase):
    def test_openai_uses_json_and_languages(self):
        t = OpenAITranscriber("gpt-transcribe", "ja", prompt="固有名詞")
        cap = {}
        patches = _direct(t, httpx.Response(200, json={"text": "結果 です"}), cap)
        for p in patches:
            p.start()
        try:
            self.assertEqual(t.transcribe(_audio()), "結果です")
        finally:
            for p in patches:
                p.stop()
        data = cap["data"]
        self.assertEqual(data["response_format"], "json")
        self.assertEqual(data["languages[]"], "ja")
        self.assertNotIn("language", data)
        self.assertNotIn("temperature", data)
        # gpt-transcribe には Whisper 用の数字の例文を送らない（ユーザーのプロンプトだけ）
        self.assertEqual(data["prompt"], "固有名詞")

    def test_openai_no_prompt_when_empty(self):
        t = OpenAITranscriber("gpt-transcribe", "ja")
        self.assertNotIn("prompt", t.build_form("gpt-transcribe", True))

    def test_groq_never_uses_gpt_format(self):
        t = GroqTranscriber("gpt-transcribe-like", "ja")
        self.assertFalse(t.uses_gpt_transcribe_format())

    def test_openai_live_rest_fallback_uses_gpt_transcribe(self):
        t = OpenAILiveRestTranscriber("gpt-live-transcribe", "ja")
        self.assertEqual(t.rest_model, "gpt-transcribe")
        self.assertTrue(t.uses_gpt_transcribe_format())
        self.assertEqual(t.build_form(t.rest_model, True)["model"], "gpt-transcribe")


class TestElevenLabs(unittest.TestCase):
    def test_tag_audio_events_false(self):
        t = ElevenLabsTranscriber("scribe_v2", "ja")
        cap = {}
        patches = _direct(t, httpx.Response(200, json={"text": "ok"}), cap)
        for p in patches:
            p.start()
        try:
            t.transcribe(_audio())
        finally:
            for p in patches:
                p.stop()
        self.assertEqual(cap["data"]["tag_audio_events"], "false")
        self.assertEqual(cap["data"]["model_id"], "scribe_v2")


class TestErrors(unittest.TestCase):
    def test_body_not_in_message_but_in_detail(self):
        t = OpenAITranscriber("gpt-transcribe")
        with self.assertRaises(TranscriptionError) as cm:
            t._raise_for_status(httpx.Response(500, text='{"error":\n {"message": "boom"}}'))
        self.assertEqual(str(cm.exception), "OpenAI API エラー (HTTP 500)")
        self.assertIn("boom", cm.exception.detail)
        self.assertNotIn("\n", cm.exception.detail)

    def test_401_needs_api_key(self):
        with self.assertRaises(TranscriptionError) as cm:
            GroqTranscriber("m")._raise_for_status(httpx.Response(401))
        self.assertTrue(cm.exception.needs_api_key)

    def test_missing_key_points_to_settings(self):
        t = ElevenLabsTranscriber("scribe_v2")
        with patch.object(api_transcriber.secrets, "api_key", return_value=None):
            with self.assertRaises(TranscriptionError) as cm:
                t._get_client()
        self.assertTrue(cm.exception.needs_api_key)
        self.assertIn("設定 › API キー", str(cm.exception))

    def test_response_detail_truncates(self):
        self.assertEqual(response_detail("a\nb\tc"), "a b c")
        self.assertTrue(response_detail("x" * 500).endswith("…"))
        self.assertEqual(len(response_detail("x" * 500)), 201)


class TestAzure(unittest.TestCase):
    def test_base_url_normalization(self):
        self.assertEqual(azure_base_url("myres.cognitiveservices.azure.com"),
                         "https://myres.cognitiveservices.azure.com")
        self.assertEqual(azure_base_url("https://x.example.com:8443/path?q=1"),
                         "https://x.example.com:8443")
        self.assertIsNone(azure_base_url(""))
        self.assertIsNone(azure_base_url("has space.com"))
        self.assertIsNone(azure_base_url(None))

    def test_transcribe_url(self):
        self.assertEqual(
            azure_transcribe_url("x.example.com"),
            "https://x.example.com/speechtotext/transcriptions:transcribe?api-version=2025-10-15",
        )

    def test_definition(self):
        self.assertEqual(
            azure_definition_json("MAI-Transcribe-2", "ja"),
            '{"enhancedMode": {"enabled": true, "model": "MAI-Transcribe-2"}, "locales": ["ja"]}',
        )
        self.assertNotIn("locales", azure_definition_json("m", ""))

    def test_parse(self):
        self.assertEqual(parse_azure_response({"combinedPhrases": [{"text": "全文"}]}), "全文")
        self.assertEqual(parse_azure_response({"phrases": [{"text": "a"}, {"text": "b"}]}), "a b")
        self.assertEqual(parse_azure_response({"combinedPhrases": []}), "")
        self.assertIsNone(parse_azure_response({"unexpected": 1}))

    def test_missing_endpoint_needs_api_key(self):
        t = AzureMAITranscriber("MAI-Transcribe-2", "ja")

        def fake_key(item):
            return "KEY" if item == "azure_key" else None

        with patch.object(api_transcriber.secrets, "api_key", side_effect=fake_key), \
                patch.object(api_transcriber.secrets, "is_dist_build", return_value=False):
            with self.assertRaises(TranscriptionError) as cm:
                t.transcribe(_audio())
        self.assertTrue(cm.exception.needs_api_key)
        self.assertIn("エンドポイント", str(cm.exception))

    def test_request_shape(self):
        t = AzureMAITranscriber("MAI-Transcribe-2", "ja")
        self.assertEqual(t._auth_headers("K"), {"Ocp-Apim-Subscription-Key": "K"})
        cap = {}

        def fake_key(item):
            return {"azure_key": "K", "azure_endpoint": "res.example.com"}.get(item)

        def fake_post(client, url, **kwargs):
            cap["url"] = url
            cap.update(kwargs)
            return httpx.Response(200, json={"combinedPhrases": [{"text": "こんにちは 世界"}]})

        with patch.object(api_transcriber.secrets, "api_key", side_effect=fake_key), \
                patch.object(api_transcriber.secrets, "is_dist_build", return_value=False), \
                patch.object(t, "_get_client", return_value=object()), \
                patch.object(t, "_post", side_effect=fake_post):
            self.assertEqual(t.transcribe(_audio()), "こんにちは世界")
        self.assertTrue(cap["url"].startswith("https://res.example.com/speechtotext/"))
        self.assertIn("audio", cap["files"])
        self.assertIn("MAI-Transcribe-2", cap["data"]["definition"])


class _FakeSession:
    """SonioxLiveSession の代役（送られた音声を数え、決まった結果を返す）。"""

    def __init__(self, text="", ending=None, start_ok=True):
        from src.core import live_transcriber as lt

        self.text = text
        self.ending = ending or lt.ENDING_COMPLETED
        self.start_ok = start_ok
        self.sent = 0
        self.on_usage = None

    def start(self, api_key=None):
        return self.start_ok

    def send(self, samples):
        self.sent += len(samples)

    def finish(self, timeout=3.0):
        return self.text


class TestSonioxReplay(unittest.TestCase):
    def _run(self, session):
        from src.core import live_transcriber as lt

        t = SonioxTranscriber("stt-rt-v5", "ja")
        with patch.object(api_transcriber.secrets, "api_key", return_value="K"), \
                patch.object(api_transcriber.secrets, "is_dist_build", return_value=False), \
                patch.object(lt, "SonioxLiveSession") as cls:
            cls.return_value = session
            cls.replay_finish_timeout = lt.SonioxLiveSession.replay_finish_timeout
            return t.transcribe(np.zeros(16000, dtype=np.float32))

    def test_replays_whole_audio(self):
        session = _FakeSession("全部")
        self.assertEqual(self._run(session), "全部")
        self.assertEqual(session.sent, 16000)

    def test_failure_without_text_raises_with_reason(self):
        from src.core import live_transcriber as lt

        with self.assertRaises(TranscriptionError) as cm:
            self._run(_FakeSession("", lt.failed("error", "401")))
        self.assertTrue(cm.exception.needs_api_key)
        with self.assertRaises(TranscriptionError) as cm:
            self._run(_FakeSession("", lt.failed("error", "402")))
        self.assertFalse(cm.exception.needs_api_key)
        self.assertIn("残高", str(cm.exception))

    def test_failure_with_partial_text_returns_it(self):
        from src.core import live_transcriber as lt

        self.assertEqual(self._run(_FakeSession("前半", lt.failed("disconnect"))), "前半")

    def test_missing_key(self):
        t = SonioxTranscriber("stt-rt-v5", "ja")
        with patch.object(api_transcriber.secrets, "api_key", return_value=None), \
                patch.object(api_transcriber.secrets, "is_dist_build", return_value=False):
            with self.assertRaises(TranscriptionError) as cm:
                t.transcribe(_audio())
        self.assertTrue(cm.exception.needs_api_key)
        self.assertIn("Soniox", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
