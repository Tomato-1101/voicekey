"""文字起こしエンジン一覧（config/backends.py）と LLM モデル一覧（config/llm_models.py）のテスト。

Mac 版の Backend / LLMModelCatalog と並び・モデル名・既定がそろっていることを確かめる。
"""

import unittest

from src.config import backends, llm_models


class TestBackendCatalog(unittest.TestCase):
    def test_selectable_order_matches_mac(self):
        # Mac の selectableCases（おすすめ順）から Apple ローカルを除いた並び
        self.assertEqual(
            backends.SELECTABLE,
            ("soniox", "elevenlabs", "openai", "azure_mai", "openai_live", "groq"),
        )
        # Deepgram は選択肢に出さないが、保存値としては動かす
        self.assertNotIn("deepgram", backends.SELECTABLE)
        self.assertTrue(backends.is_supported("deepgram"))
        self.assertFalse(backends.is_supported("apple_local"))

    def test_default_models_match_mac(self):
        expected = {
            "soniox": "stt-rt-v5",
            "elevenlabs": "scribe_v2",
            "openai": "gpt-transcribe",
            "azure_mai": "MAI-Transcribe-2",
            "openai_live": "gpt-live-transcribe",
            "groq": "whisper-large-v3-turbo",
            "deepgram": "nova-3",
        }
        for backend, model in expected.items():
            self.assertEqual(backends.default_model(backend), model)

    def test_format_default_off_for_live(self):
        for backend in ("soniox", "openai_live", "deepgram"):
            self.assertFalse(backends.default_format_enabled(backend))
        for backend in ("groq", "elevenlabs", "openai", "azure_mai"):
            self.assertTrue(backends.default_format_enabled(backend))

    def test_openai_live_shares_openai_key(self):
        self.assertEqual(backends.key_item("openai_live"), "openai")
        self.assertEqual(backends.key_item("azure_mai"), "azure_key")

    def test_resolve_model_keeps_custom(self):
        self.assertEqual(backends.resolve_model("groq", ""), "whisper-large-v3-turbo")
        self.assertEqual(backends.resolve_model("groq", "my-custom"), "my-custom")

    def test_picker_groups_match_mac(self):
        groups = backends.picker_groups("groq", "")
        # 選択肢が 1 つの提供元は見出し無しの「提供元 · モデル」1 項目
        self.assertEqual(groups[0], (None, [("soniox", "stt-rt-v5", "Soniox · stt-rt-v5")]))
        # ElevenLabs は見出しの下に 2 モデル、既定に（推奨）
        self.assertEqual(groups[1], ("ElevenLabs", [
            ("elevenlabs", "scribe_v2", "scribe_v2（推奨）"),
            ("elevenlabs", "scribe_v1", "scribe_v1"),
        ]))
        # OpenAI の通常とライブは同じ見出しにまとまり、ライブには（ライブ）
        self.assertEqual(groups[2], ("OpenAI", [
            ("openai", "gpt-transcribe", "gpt-transcribe"),
            ("openai_live", "gpt-live-transcribe", "gpt-live-transcribe（ライブ）"),
        ]))
        self.assertEqual(groups[3][1][0][2], "Microsoft · MAI-Transcribe-2")
        self.assertEqual(groups[-1][1][0][2], "Groq · whisper-large-v3-turbo")
        self.assertEqual(len(groups), 5)

    def test_picker_keeps_legacy_deepgram_and_custom_model(self):
        groups = backends.picker_groups("deepgram", "nova-3")
        self.assertEqual(groups[-1][1], [("deepgram", "nova-3", "Deepgram · nova-3")])
        # 一覧外の自由入力モデルも、その提供元に足して選択中のまま見せる
        groups = backends.picker_groups("groq", "whisper-large-v3")
        self.assertEqual(groups[-1][0], "Groq")
        self.assertIn(("groq", "whisper-large-v3", "whisper-large-v3"), groups[-1][1])

    def test_choice_title(self):
        self.assertEqual(backends.choice_title("soniox", ""), "Soniox · stt-rt-v5")
        self.assertEqual(backends.choice_title("openai_live", ""), "OpenAI · gpt-live-transcribe")

    def test_captions_exist_for_selectable(self):
        for backend in backends.SELECTABLE:
            self.assertTrue(backends.caption(backend), backend)


class TestLLMModels(unittest.TestCase):
    def test_recommended(self):
        self.assertEqual(llm_models.recommended(llm_models.GROQ), "qwen/qwen3.8-27b")
        self.assertEqual(llm_models.recommended(llm_models.GEMINI), "gemini-3.5-flash-lite")

    def test_retired_llama_detected_and_migrated(self):
        self.assertTrue(llm_models.is_known_retired("llama-3.1-8b-instant"))
        self.assertTrue(llm_models.is_known_retired("meta-llama/llama-4-scout"))
        self.assertFalse(llm_models.is_known_retired("openai/gpt-oss-20b"))
        self.assertEqual(llm_models.migrated_model("llama-3.3-70b-versatile"), "qwen/qwen3.8-27b")
        self.assertIsNone(llm_models.migrated_model("openai/gpt-oss-120b"))
        # Gemini は起動時には触らない
        self.assertIsNone(llm_models.migrated_model("gemini-1.0-pro", llm_models.GEMINI))

    def test_extra_parameters(self):
        self.assertEqual(
            llm_models.extra_parameters("openai/gpt-oss-20b"),
            {"reasoning_effort": "low", "include_reasoning": False},
        )
        self.assertEqual(llm_models.extra_parameters("qwen/qwen3.8-27b"), {"reasoning_effort": "none"})
        self.assertEqual(llm_models.extra_parameters("allam-2-7b"), {})

    def test_max_completion_tokens(self):
        self.assertEqual(llm_models.max_completion_tokens(0), 512)
        self.assertEqual(llm_models.max_completion_tokens(100), 812)
        self.assertEqual(llm_models.max_completion_tokens(100000), 8192)

    def test_is_truncated(self):
        self.assertTrue(llm_models.is_truncated({"choices": [{"finish_reason": "length"}]}))
        self.assertFalse(llm_models.is_truncated({"choices": [{"finish_reason": "stop"}]}))
        self.assertFalse(llm_models.is_truncated(b"not json"))

    def test_is_model_unavailable(self):
        self.assertTrue(llm_models.is_model_unavailable(
            400, {"error": {"code": "model_decommissioned", "message": "x"}}))
        self.assertTrue(llm_models.is_model_unavailable(
            404, {"error": {"code": "model_not_found"}}))
        self.assertTrue(llm_models.is_model_unavailable(
            404, {"error": {"code": 404, "status": "NOT_FOUND"}}))
        self.assertFalse(llm_models.is_model_unavailable(
            429, {"error": {"code": "model_decommissioned"}}))
        self.assertFalse(llm_models.is_model_unavailable(400, {"error": {"code": "invalid_api_key"}}))

    def test_options_mark_recommended_and_unavailable(self):
        opts = llm_models.options(llm_models.GROQ, "llama-3.1-8b-instant")
        self.assertEqual(opts[0], ("qwen/qwen3.8-27b", "Qwen3.8 27B（おすすめ）"))
        self.assertEqual(opts[-1], ("llama-3.1-8b-instant", "llama-3.1-8b-instant（利用不可）"))


if __name__ == "__main__":
    unittest.main()
