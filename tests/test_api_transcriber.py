"""録音後にまとめて送る文字起こしクライアント（Groq / OpenAI / ElevenLabs / Microsoft MAI）のテスト。

リクエストの組み立て・応答の読み取り・エラー時の文言を、ネットワークなしで検証する。
通信は _post を偽物に差し替え、キーと接続先は engines.read_secret を差し替えて与える
（実際の資格情報ストア・環境変数は読まない）。
"""

import json
import unittest
from unittest.mock import patch

import httpx
import numpy as np

from src.core import api_transcriber, engines
from src.core.api_transcriber import (
    AzureMAITranscriber,
    ElevenLabsTranscriber,
    GroqTranscriber,
    OpenAITranscriber,
    TranscriptionError,
    create_transcriber,
    normalize_mai_endpoint,
)


def _audio():
    """0.1 秒ぶんのテスト用音声。"""
    return np.full(1600, 0.1, dtype=np.float32)


def _capture(transcriber, response: httpx.Response):
    """
    transcribe を偽の通信で 1 回動かし、(結果, 送ろうとした内容) を返す。

    Args:
        transcriber: 対象のクライアント
        response: 偽の応答

    Returns:
        (文字起こし結果, {"path": ..., と _post に渡したキーワード引数})
    """
    captured = {}

    def fake_post(client, path, **kwargs):
        captured["path"] = path
        captured.update(kwargs)
        return response

    with patch.object(transcriber, "_get_client", return_value=object()), \
            patch.object(transcriber, "_post", side_effect=fake_post):
        text = transcriber.transcribe(_audio())
    return text, captured


def _secrets(values: dict):
    """engines.read_secret を、サービス名 → 値の辞書で答える偽物に差し替える。"""
    return patch.object(engines, "read_secret", lambda service, env_var: values.get(service))


class TestAuthHeaders(unittest.TestCase):
    def test_openai_bearer(self):
        self.assertEqual(OpenAITranscriber("m")._auth_headers("K"), {"Authorization": "Bearer K"})

    def test_groq_bearer(self):
        self.assertEqual(GroqTranscriber("m")._auth_headers("K"), {"Authorization": "Bearer K"})

    def test_elevenlabs_xi_key(self):
        self.assertEqual(ElevenLabsTranscriber("scribe_v2")._auth_headers("K"), {"xi-api-key": "K"})

    def test_azure_subscription_key(self):
        self.assertEqual(
            AzureMAITranscriber("MAI-Transcribe-2")._auth_headers("K"),
            {"Ocp-Apim-Subscription-Key": "K"},
        )


class TestCatalogBinding(unittest.TestCase):
    """表示名・鍵の置き場所・モデル一覧はカタログ（engines.py）から引く。"""

    def test_names_and_key_locations(self):
        t = GroqTranscriber("whisper-large-v3-turbo")
        self.assertEqual(t.display_name, "Groq")
        self.assertEqual(t.keychain_service, "voicekey.Groq")
        self.assertEqual(t.env_var, "GROQ_API_KEY")
        self.assertEqual(AzureMAITranscriber("m").display_name, "Microsoft MAI")

    def test_available_models(self):
        self.assertEqual(ElevenLabsTranscriber("m").available_models, ("scribe_v2", "scribe_v1"))
        self.assertEqual(OpenAITranscriber("m").available_models, ("gpt-transcribe",))

    def test_rest_engines_allow_split(self):
        self.assertTrue(GroqTranscriber("m").allows_split)


class TestEmptyAudio(unittest.TestCase):
    def test_empty_returns_empty_string(self):
        empty = np.array([], dtype=np.float32)
        # ネットワークに到達せず即 "" を返す（キー不要）
        for cls in (GroqTranscriber, OpenAITranscriber, ElevenLabsTranscriber, AzureMAITranscriber):
            self.assertEqual(cls("m").transcribe(empty), "")


class TestGroqRequest(unittest.TestCase):
    """Groq は Whisper 形式（数字を半角で出させる style プロンプトを常に付ける）。"""

    def test_request_shape(self):
        text, sent = _capture(
            GroqTranscriber("whisper-large-v3-turbo", "ja"), httpx.Response(200, text=" 結果 \n")
        )
        self.assertEqual(text, "結果")
        self.assertEqual(sent["path"], "/audio/transcriptions")
        self.assertEqual(sent["data"], {
            "model": "whisper-large-v3-turbo",
            "response_format": "text",
            "temperature": "0.0",
            "prompt": api_transcriber._NUMERAL_STYLE_PROMPT,
            "language": "ja",
        })
        name, wav, mime = sent["files"]["file"]
        self.assertEqual((name, mime), ("audio.wav", "audio/wav"))
        self.assertEqual(wav[:4], b"RIFF")

    def test_empty_language_is_not_sent(self):
        _, sent = _capture(GroqTranscriber("m", ""), httpx.Response(200, text="x"))
        self.assertNotIn("language", sent["data"])

    def test_user_prompt_is_appended_after_hint(self):
        t = GroqTranscriber("whisper-large-v3-turbo", "ja", prompt="固有名詞: VoiceKey")
        _, sent = _capture(t, httpx.Response(200, text="x"))
        self.assertTrue(sent["data"]["prompt"].startswith(api_transcriber._NUMERAL_STYLE_PROMPT))
        self.assertIn("固有名詞: VoiceKey", sent["data"]["prompt"])

    def test_cjk_spaces_are_removed(self):
        text, _ = _capture(GroqTranscriber("m"), httpx.Response(200, text="今日 は 晴れ"))
        self.assertEqual(text, "今日は晴れ")


class TestOpenAIRequest(unittest.TestCase):
    """gpt-transcribe は Whisper と形が違う（json 応答・languages[]・temperature なし）。"""

    def test_request_shape(self):
        text, sent = _capture(
            OpenAITranscriber("gpt-transcribe", "ja"), httpx.Response(200, json={"text": " 結果 "})
        )
        self.assertEqual(text, "結果")
        self.assertEqual(sent["path"], "/audio/transcriptions")
        self.assertEqual(
            sent["data"],
            {"model": "gpt-transcribe", "response_format": "json", "languages[]": "ja"},
        )

    def test_only_user_prompt_is_sent(self):
        # Whisper 用の数字ヒントは効かないので付けない。利用者が書いたものだけ送る
        t = OpenAITranscriber("gpt-transcribe", "ja", prompt="  VoiceKey  ")
        _, sent = _capture(t, httpx.Response(200, json={"text": "x"}))
        self.assertEqual(sent["data"]["prompt"], "VoiceKey")

    def test_non_json_response_raises(self):
        with self.assertRaises(TranscriptionError) as ctx:
            _capture(OpenAITranscriber("gpt-transcribe"), httpx.Response(200, text="<html>"))
        self.assertIn("読み取れませんでした", str(ctx.exception))


class TestElevenLabsRequest(unittest.TestCase):
    def test_request_shape(self):
        text, sent = _capture(
            ElevenLabsTranscriber("scribe_v2", "ja"), httpx.Response(200, json={"text": "結果"})
        )
        self.assertEqual(text, "結果")
        self.assertEqual(sent["path"], "/speech-to-text")
        self.assertEqual(
            sent["data"],
            {"model_id": "scribe_v2", "tag_audio_events": "false", "language_code": "ja"},
        )
        self.assertIn("file", sent["files"])

    def test_null_text_is_empty(self):
        text, _ = _capture(ElevenLabsTranscriber("scribe_v2"), httpx.Response(200, json={"text": None}))
        self.assertEqual(text, "")


class TestMaiEndpoint(unittest.TestCase):
    def test_strips_path_and_slash(self):
        self.assertEqual(
            normalize_mai_endpoint(" https://my-res.cognitiveservices.azure.com/speechtotext/ "),
            "https://my-res.cognitiveservices.azure.com",
        )

    def test_adds_https(self):
        self.assertEqual(
            normalize_mai_endpoint("my-res.cognitiveservices.azure.com"),
            "https://my-res.cognitiveservices.azure.com",
        )

    def test_keeps_port(self):
        self.assertEqual(normalize_mai_endpoint("http://localhost:8080/x"), "http://localhost:8080")

    def test_invalid_values(self):
        for value in ("", "   ", "has space.example.com", "ftp://example.com", "https://"):
            self.assertIsNone(normalize_mai_endpoint(value), value)


class TestAzureMai(unittest.TestCase):
    _KEY = "voicekey.AzureSpeech"
    _ENDPOINT = "voicekey.AzureSpeechEndpoint"

    def test_request_shape(self):
        text, sent = _capture(
            AzureMAITranscriber("MAI-Transcribe-2", "ja"),
            httpx.Response(200, json={"combinedPhrases": [{"text": " 結果 "}]}),
        )
        self.assertEqual(text, "結果")
        self.assertEqual(sent["path"], "/speechtotext/transcriptions:transcribe")
        self.assertEqual(sent["params"], {"api-version": "2025-10-15"})
        name, wav, mime = sent["files"]["audio"]
        self.assertEqual((name, mime), ("audio.wav", "audio/wav"))
        filename, definition, def_mime = sent["files"]["definition"]
        self.assertIsNone(filename)
        self.assertEqual(def_mime, "application/json")
        self.assertEqual(json.loads(definition), {
            "enhancedMode": {"enabled": True, "model": "MAI-Transcribe-2"},
            "locales": ["ja"],
        })

    def test_empty_language_omits_locales(self):
        _, sent = _capture(
            AzureMAITranscriber("MAI-Transcribe-2", ""),
            httpx.Response(200, json={"combinedPhrases": []}),
        )
        self.assertNotIn("locales", json.loads(sent["files"]["definition"][1]))

    def test_empty_combined_phrases_means_no_speech(self):
        text, _ = _capture(
            AzureMAITranscriber("MAI-Transcribe-2"), httpx.Response(200, json={"combinedPhrases": []})
        )
        self.assertEqual(text, "")

    def test_falls_back_to_phrases(self):
        text, _ = _capture(
            AzureMAITranscriber("MAI-Transcribe-2", "en"),
            httpx.Response(200, json={"phrases": [{"text": "hello"}, {"text": "world"}]}),
        )
        self.assertEqual(text, "hello world")

    def test_unexpected_response_raises(self):
        with self.assertRaises(TranscriptionError):
            _capture(AzureMAITranscriber("MAI-Transcribe-2"), httpx.Response(200, json={"x": 1}))

    def test_available_needs_key_and_endpoint(self):
        t = AzureMAITranscriber("MAI-Transcribe-2")
        with _secrets({self._KEY: "K"}):
            self.assertFalse(t.is_available())
        with _secrets({self._ENDPOINT: "https://r.example.com"}):
            self.assertFalse(t.is_available())
        with _secrets({self._KEY: "K", self._ENDPOINT: "not a url"}):
            self.assertFalse(t.is_available())
        with _secrets({self._KEY: "K", self._ENDPOINT: "r.example.com/path"}):
            self.assertTrue(t.is_available())
            self.assertEqual(t._resolve_base_url(), "https://r.example.com")

    def test_missing_endpoint_message(self):
        t = AzureMAITranscriber("MAI-Transcribe-2")
        with _secrets({self._KEY: "K"}):
            with self.assertRaises(TranscriptionError) as ctx:
                t._get_client()
        self.assertIn("接続先", str(ctx.exception))


class TestErrors(unittest.TestCase):
    """失敗は短い日本語だけを通知に出す（API のエラー本文は出さない）。"""

    def test_status_messages(self):
        t = OpenAITranscriber("gpt-transcribe")
        cases = {401: "API キーが無効", 429: "レート制限", 500: "HTTP 500"}
        for status, expected in cases.items():
            with self.assertRaises(TranscriptionError) as ctx:
                t._raise_for_status(httpx.Response(status, text="secret upstream detail"))
            self.assertIn(expected, str(ctx.exception))
            self.assertIn("OpenAI", str(ctx.exception))
            self.assertNotIn("upstream", str(ctx.exception))

    def test_ok_passes(self):
        OpenAITranscriber("m")._raise_for_status(httpx.Response(200))  # 例外を投げない

    def test_timeout_and_connection_errors(self):
        t = GroqTranscriber("m")

        class FakeClient:
            def __init__(self, exc):
                self._exc = exc

            def post(self, path, **kwargs):
                raise self._exc

        with self.assertRaises(TranscriptionError) as ctx:
            t._post(FakeClient(httpx.ReadTimeout("t")), "/x")
        self.assertIn("タイムアウト", str(ctx.exception))
        with self.assertRaises(TranscriptionError) as ctx:
            t._post(FakeClient(httpx.ConnectError("refused: 10.0.0.1")), "/x")
        self.assertIn("接続できませんでした", str(ctx.exception))
        self.assertNotIn("10.0.0.1", str(ctx.exception))

    def test_missing_key_message(self):
        t = GroqTranscriber("m")
        with _secrets({}):
            self.assertFalse(t.is_available())
            with self.assertRaises(TranscriptionError) as ctx:
                t._get_client()
        self.assertIn("API キーが未設定", str(ctx.exception))


class TestCreateTranscriber(unittest.TestCase):
    def test_rest_engines(self):
        pairs = {
            "groq": GroqTranscriber,
            "openai": OpenAITranscriber,
            "elevenlabs": ElevenLabsTranscriber,
            "azure_mai": AzureMAITranscriber,
        }
        for engine_id, cls in pairs.items():
            t = create_transcriber(engine_id, "model-x", "en", "hint")
            self.assertIs(type(t), cls)
            self.assertEqual((t.model, t.language, t.prompt), ("model-x", "en", "hint"))

    def test_openai_live_falls_back_to_rest_model(self):
        # ライブがつながらなかったときの送り直し先は gpt-transcribe（ライブ用のモデル名は REST で使えない）
        t = create_transcriber("openai_live", "gpt-live-transcribe")
        self.assertIs(type(t), OpenAITranscriber)
        self.assertEqual(t.model, "gpt-transcribe")

    def test_soniox_resends_over_websocket(self):
        from src.core.streaming_transcriber import SonioxTranscriber

        t = create_transcriber("soniox", "stt-rt-v5")
        self.assertIsInstance(t, SonioxTranscriber)
        self.assertEqual(t.model, "stt-rt-v5")

    def test_every_engine_has_a_transcriber(self):
        for eng in engines.ENGINES:
            t = create_transcriber(eng.id, eng.default_model)
            for name in ("transcribe", "prewarm", "is_available", "close"):
                self.assertTrue(callable(getattr(t, name)), f"{eng.id}.{name}")


if __name__ == "__main__":
    unittest.main()
