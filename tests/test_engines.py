"""文字起こしエンジンのカタログ（engines.py）のテスト。

並び順・id・モデル・鍵の置き場所が決めたとおりか、保存済みの値の読み替えが正しいかを確かめる。
資格情報ストアと環境変数は偽物に差し替える（実際の値は読まない）。
"""

import unittest
from unittest.mock import patch

from src.core import engines


class TestCatalog(unittest.TestCase):
    def test_order_and_ids(self):
        # id は Mac 版 Backend の rawValue と同じ。並びは設定画面の表示順
        self.assertEqual(
            engines.SELECTABLE_IDS,
            ("soniox", "elevenlabs", "openai", "azure_mai", "openai_live", "groq"),
        )
        self.assertNotIn("deepgram", engines.SELECTABLE_IDS)

    def test_models(self):
        expected = {
            "soniox": ("stt-rt-v5",),
            "elevenlabs": ("scribe_v2", "scribe_v1"),
            "openai": ("gpt-transcribe",),
            "azure_mai": ("MAI-Transcribe-2",),
            "openai_live": ("gpt-live-transcribe",),
            "groq": ("whisper-large-v3-turbo",),
        }
        for engine_id, models in expected.items():
            eng = engines.engine(engine_id)
            self.assertEqual(eng.models, models)
            self.assertEqual(eng.default_model, models[0])

    def test_streaming_engines(self):
        streaming = {e.id for e in engines.ENGINES if e.streaming}
        self.assertEqual(streaming, {"soniox", "openai_live"})

    def test_format_default_is_off_only_for_streaming(self):
        # 話しながら送るエンジンは速さが売りなので、整形の往復は既定では足さない
        for eng in engines.ENGINES:
            self.assertEqual(eng.default_format_enabled, not eng.streaming, eng.id)

    def test_key_locations(self):
        expected = {
            "soniox": ("voicekey.Soniox", "SONIOX_API_KEY"),
            "elevenlabs": ("voicekey.ElevenLabs", "ELEVENLABS_API_KEY"),
            "openai": ("voicekey.OpenAI", "OPENAI_API_KEY"),
            "azure_mai": ("voicekey.AzureSpeech", "AZURE_SPEECH_KEY"),
            "openai_live": ("voicekey.OpenAI", "OPENAI_API_KEY"),
            "groq": ("voicekey.Groq", "GROQ_API_KEY"),
        }
        for engine_id, (service, env_var) in expected.items():
            eng = engines.engine(engine_id)
            self.assertEqual((eng.keyring_service, eng.env_var), (service, env_var))

    def test_only_microsoft_has_endpoint(self):
        for eng in engines.ENGINES:
            if eng.id == "azure_mai":
                self.assertEqual(eng.endpoint_service, "voicekey.AzureSpeechEndpoint")
                self.assertEqual(eng.endpoint_env_var, "AZURE_SPEECH_ENDPOINT")
            else:
                self.assertEqual((eng.endpoint_service, eng.endpoint_env_var), ("", ""))

    def test_labels_and_captions_exist(self):
        for eng in engines.ENGINES:
            self.assertTrue(eng.label.endswith(eng.default_model), eng.id)
            self.assertTrue(eng.name and eng.provider_name and eng.caption, eng.id)

    def test_unknown_id_raises(self):
        with self.assertRaises(KeyError):
            engines.engine("deepgram")


class TestNormalizeId(unittest.TestCase):
    def test_known_ids_are_kept(self):
        for engine_id in engines.SELECTABLE_IDS:
            self.assertEqual(engines.normalize_id(engine_id), engine_id)

    def test_case_is_ignored(self):
        self.assertEqual(engines.normalize_id("Soniox"), "soniox")

    def test_deepgram_moves_to_soniox(self):
        self.assertEqual(engines.normalize_id("deepgram"), "soniox")

    def test_unknown_falls_back_to_groq(self):
        for value in ("", "apple", "local", None, 3):
            self.assertEqual(engines.normalize_id(value), "groq")

    def test_idempotent(self):
        for value in ("deepgram", "whatever", "openai_live"):
            once = engines.normalize_id(value)
            self.assertEqual(engines.normalize_id(once), once)


class TestNormalizeModel(unittest.TestCase):
    def test_empty_becomes_default(self):
        self.assertEqual(engines.normalize_model("soniox", ""), "stt-rt-v5")
        self.assertEqual(engines.normalize_model("groq", None), "whisper-large-v3-turbo")

    def test_retired_names_become_default(self):
        cases = {
            ("soniox", "nova-3"): "stt-rt-v5",
            ("openai", "gpt-4o-transcribe"): "gpt-transcribe",
            ("openai", "gpt-4o-mini-transcribe"): "gpt-transcribe",
            ("openai_live", "gpt-realtime-whisper"): "gpt-live-transcribe",
            ("groq", "whisper-large-v3"): "whisper-large-v3-turbo",
            ("elevenlabs", "scribe_v1_experimental"): "scribe_v2",
        }
        for (engine_id, model), expected in cases.items():
            self.assertEqual(engines.normalize_model(engine_id, model), expected)

    def test_other_engines_model_becomes_default(self):
        # エンジンを読み替えた後に、前のエンジンのモデル名が残っていた場合
        self.assertEqual(engines.normalize_model("groq", "scribe_v2"), "whisper-large-v3-turbo")

    def test_listed_models_are_kept(self):
        self.assertEqual(engines.normalize_model("elevenlabs", "scribe_v1"), "scribe_v1")

    def test_unlisted_name_is_kept(self):
        # 新しいモデルを手で書いて試せるようにする
        self.assertEqual(engines.normalize_model("soniox", "stt-rt-v6-preview"), "stt-rt-v6-preview")


class TestSecrets(unittest.TestCase):
    """読み順は資格情報ストア → 環境変数。"""

    def _patched(self, stored: dict, env: dict):
        """資格情報ストアと環境変数をまとめて偽物にする。"""
        return (
            patch.object(engines.secrets, "get_api_key", lambda service: stored.get(service)),
            patch.dict(engines.os.environ, env, clear=True),
        )

    def test_store_wins_over_env(self):
        store, env = self._patched({"voicekey.Groq": "STORED"}, {"GROQ_API_KEY": "ENV"})
        with store, env:
            self.assertEqual(engines.api_key("groq"), "STORED")

    def test_env_is_used_when_store_is_empty(self):
        store, env = self._patched({}, {"SONIOX_API_KEY": "ENV"})
        with store, env:
            self.assertEqual(engines.api_key("soniox"), "ENV")

    def test_missing_is_none(self):
        store, env = self._patched({}, {"OPENAI_API_KEY": ""})
        with store, env:
            self.assertIsNone(engines.api_key("openai"))
            self.assertFalse(engines.has_any_api_key())

    def test_openai_key_is_shared_by_both_openai_engines(self):
        store, env = self._patched({"voicekey.OpenAI": "K"}, {})
        with store, env:
            self.assertEqual(engines.api_key("openai"), "K")
            self.assertEqual(engines.api_key("openai_live"), "K")
            self.assertTrue(engines.has_any_api_key())

    def test_read_secret_for_endpoint(self):
        store, env = self._patched({}, {"AZURE_SPEECH_ENDPOINT": "https://r.example.com"})
        with store, env:
            eng = engines.engine("azure_mai")
            self.assertEqual(
                engines.read_secret(eng.endpoint_service, eng.endpoint_env_var),
                "https://r.example.com",
            )


if __name__ == "__main__":
    unittest.main()
