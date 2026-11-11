"""整形に使う LLM モデルのカタログ（llm_catalog.py）のテスト。

モデルの一覧・提供が終わったモデルの読み替え・モデル別の追加パラメータ・出力上限を確かめる。
"""

import unittest

from src.core import llm_catalog


class TestModels(unittest.TestCase):
    def test_list_matches_mac(self):
        self.assertEqual(
            [m.id for m in llm_catalog.MODELS],
            ["qwen/qwen3.8-27b", "openai/gpt-oss-120b", "openai/gpt-oss-20b", "allam-2-7b"],
        )

    def test_exactly_one_recommended(self):
        recommended = [m.id for m in llm_catalog.MODELS if m.recommended]
        self.assertEqual(recommended, [llm_catalog.RECOMMENDED_MODEL])

    def test_listed_models_are_not_retired(self):
        for m in llm_catalog.MODELS:
            self.assertFalse(llm_catalog.is_retired(m.id), m.id)
            self.assertTrue(m.name and m.note, m.id)


class TestRetired(unittest.TestCase):
    def test_retired_names(self):
        for model in (
            "llama-3.1-8b-instant",
            "llama-3.3-70b-versatile",
            "Llama-3.1-8b-instant",
            "meta-llama/llama-4-scout-17b-16e-instruct",
            "moonshotai/kimi-k2-instruct",
        ):
            self.assertTrue(llm_catalog.is_retired(model), model)

    def test_other_names_are_not_retired(self):
        for model in ("qwen/qwen3.8-27b", "openai/gpt-oss-20b", "moonshotai/kimi-k3", "my-model"):
            self.assertFalse(llm_catalog.is_retired(model), model)

    def test_normalize_model(self):
        rec = llm_catalog.RECOMMENDED_MODEL
        self.assertEqual(llm_catalog.normalize_model("llama-3.1-8b-instant"), rec)
        self.assertEqual(llm_catalog.normalize_model(""), rec)
        self.assertEqual(llm_catalog.normalize_model(None), rec)
        # 利用者が選んだ別のモデルは残す
        self.assertEqual(llm_catalog.normalize_model("openai/gpt-oss-120b"), "openai/gpt-oss-120b")
        self.assertEqual(llm_catalog.normalize_model(" my-model "), "my-model")

    def test_normalize_is_idempotent(self):
        once = llm_catalog.normalize_model("llama-3.1-8b-instant")
        self.assertEqual(llm_catalog.normalize_model(once), once)


class TestExtraParameters(unittest.TestCase):
    def test_gpt_oss(self):
        # gpt-oss は "none" を受け付けないので low にして、考える過程を応答に含めない
        for model in ("openai/gpt-oss-120b", "openai/gpt-oss-20b"):
            self.assertEqual(
                llm_catalog.extra_parameters(model),
                {"reasoning_effort": "low", "include_reasoning": False},
            )

    def test_qwen3(self):
        self.assertEqual(
            llm_catalog.extra_parameters("qwen/qwen3.8-27b"), {"reasoning_effort": "none"}
        )

    def test_others_get_nothing(self):
        self.assertEqual(llm_catalog.extra_parameters("allam-2-7b"), {})
        self.assertEqual(llm_catalog.extra_parameters("my-model"), {})


class TestMaxCompletionTokens(unittest.TestCase):
    def test_formula(self):
        self.assertEqual(llm_catalog.max_completion_tokens(0), 512)
        self.assertEqual(llm_catalog.max_completion_tokens(100), 812)

    def test_cap(self):
        self.assertEqual(llm_catalog.max_completion_tokens(2560), 8192)
        self.assertEqual(llm_catalog.max_completion_tokens(100000), 8192)

    def test_negative_is_treated_as_zero(self):
        self.assertEqual(llm_catalog.max_completion_tokens(-5), 512)


if __name__ == "__main__":
    unittest.main()
