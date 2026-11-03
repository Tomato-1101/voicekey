"""段階 2（翻訳して入力・API の利用料金・翻訳ページ）のテスト。ネットワーク・課金 API には触れない。"""

import json
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

import httpx

from src.core import api_usage, dictation_translator
from tests.test_settings_window import _SettingsWindowTestBase


class TestPricing(unittest.TestCase):
    def test_table_matches_mac(self):
        self.assertEqual(api_usage.price("soniox", "stt-rt-v5"), ("audio", 0.12, 0.0))
        self.assertAlmostEqual(api_usage.price("openai", "gpt-transcribe")[1], 0.27)
        self.assertEqual(api_usage.price("groq", "whisper-large-v3-turbo"), ("audio", 0.04, 10.0))
        self.assertIsNone(api_usage.price("elevenlabs", "scribe_v1"))  # 単価未確認
        self.assertIsNone(api_usage.price("groq", "llama-3.1-8b-instant"))

    def test_minimum_billed_seconds(self):
        self.assertEqual(api_usage.billed_seconds("groq", "whisper-large-v3-turbo", 3), 10)
        self.assertEqual(api_usage.billed_seconds("soniox", "stt-rt-v5", 3), 3)

    def test_formatted(self):
        self.assertEqual(api_usage.formatted_usd(0.12), "$0.12")
        self.assertEqual(api_usage.formatted_usd(0.0012), "$0.0012")
        self.assertEqual(api_usage.formatted_usd(0), "$0.00")

    def test_provider_mapping(self):
        self.assertEqual(api_usage.provider_for("openai_live"), "openai")
        self.assertEqual(api_usage.provider_for("azure_mai"), "microsoft")


class TestUsageStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "api_usage.json"
        self._stores = []

    def tearDown(self):
        # 裏の保存が一時ディレクトリの削除と競合しないよう、終わるのを待つ
        for store in self._stores:
            store.flush()

    def _store(self):
        store = api_usage.ApiUsageStore(self.path)
        self._stores.append(store)
        return store

    def test_record_and_summaries(self):
        store = self._store()
        today = date(2026, 10, 19)
        store.record_audio("soniox", "stt-rt-v5", 3600, when=today)
        store.record_audio("groq", "whisper-large-v3-turbo", 2, when=today)
        store.record_audio("elevenlabs", "scribe_v1", 60, when=today)
        store.record_chat_usage({"usage": {"prompt_tokens": 1_000_000, "completion_tokens": 0}},
                                "groq", "openai/gpt-oss-20b", api_usage.PURPOSE_FORMATTING)
        s = store.summary(api_usage.MODE_LIST, lambda d: d == "2026-10-19")
        # Soniox 1 時間 + Groq Whisper 最低 10 秒 + gpt-oss-20b 入力 100 万トークン（$0.075）
        self.assertAlmostEqual(s.usd, 0.12 + 0.04 * 10 / 3600 + 0.075, places=6)
        self.assertTrue(s.has_unpriced)  # scribe_v1 は単価未確認を隠さない
        # 実際に払った分: Groq・ElevenLabs は無料枠として 0
        paid = store.summary(api_usage.MODE_PAID, lambda d: d == "2026-10-19")
        self.assertAlmostEqual(paid.usd, 0.12, places=6)
        self.assertFalse(paid.has_unpriced)
        store.flush()
        # 保存は数量だけ（本文・キーは無い）。読み直しても同じ
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertIn("2026-10-19", data["days"])
        again = api_usage.ApiUsageStore(self.path)
        self.assertEqual(again.summary(api_usage.MODE_LIST, lambda d: True).requests, 4)

    def test_gemini_thoughts_counted_as_output(self):
        store = self._store()
        self.assertTrue(store.record_gemini_usage(
            {"usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5, "thoughtsTokenCount": 7}},
            "gemini-3.5-flash-lite", api_usage.PURPOSE_TRANSLATION))
        row = store.all_time(api_usage.MODE_LIST).rows[0]
        self.assertEqual((row.inputTokens, row.outputTokens), (10, 12))

    def test_corrupt_file_is_quarantined(self):
        self.path.write_text("{broken", encoding="utf-8")
        store = self._store()
        self.assertEqual(store.all_time(api_usage.MODE_LIST).requests, 0)
        self.assertTrue(any(p.name.startswith("api_usage.json.corrupt-") for p in Path(self.tmp.name).iterdir()))

    def test_daily_series(self):
        store = self._store()
        store.record_audio("soniox", "stt-rt-v5", 3600, when=date(2026, 10, 18))
        series = store.daily_series(3, api_usage.MODE_LIST, end=date(2026, 10, 19))
        self.assertEqual([d for d, _ in series], ["2026-10-17", "2026-10-18", "2026-10-19"])
        self.assertAlmostEqual(series[1][1], 0.12)


class _Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


class TestDictationTranslator(unittest.TestCase):
    def _config(self, **kw):
        base = {"translate_input_engine": "groq", "translate_input_target": "en",
                "translate_input_groq_model": "qwen/qwen3.8-27b"}
        base.update(kw)
        return base

    def test_groq_success_and_request(self):
        client = mock.Mock()
        client.post.return_value = _Resp(200, {"choices": [{"message": {"content": " Hello "}, "finish_reason": "stop"}]})
        with mock.patch.object(dictation_translator.secrets, "api_key", return_value="K"), \
                mock.patch.object(dictation_translator, "_get_client", return_value=client):
            result = dictation_translator.translate("こんにちは", self._config())
        self.assertEqual((result.text, result.did_translate), ("Hello", True))
        body = client.post.call_args.kwargs["json"]
        self.assertEqual(body["model"], "qwen/qwen3.8-27b")
        self.assertEqual(body["reasoning_effort"], "none")
        self.assertIn("英語", body["messages"][0]["content"])
        self.assertEqual(client.post.call_args.kwargs["headers"]["Authorization"], "Bearer K")

    def test_truncated_returns_original(self):
        client = mock.Mock()
        client.post.return_value = _Resp(200, {"choices": [{"message": {"content": "Hel"}, "finish_reason": "length"}]})
        with mock.patch.object(dictation_translator.secrets, "api_key", return_value="K"), \
                mock.patch.object(dictation_translator, "_get_client", return_value=client):
            result = dictation_translator.translate("長い文", self._config())
        self.assertEqual((result.text, result.did_translate), ("長い文", False))

    def test_missing_key_returns_original(self):
        with mock.patch.object(dictation_translator.secrets, "api_key", return_value=None):
            result = dictation_translator.translate("原文", self._config())
        self.assertFalse(result.did_translate)
        self.assertEqual(result.text, "原文")

    def test_decommissioned_model_is_replaced_once(self):
        client = mock.Mock()
        client.post.side_effect = [
            _Resp(400, {"error": {"code": "model_decommissioned"}}),
            _Resp(200, {"choices": [{"message": {"content": "Hi"}, "finish_reason": "stop"}]}),
        ]
        replaced = []
        with mock.patch.object(dictation_translator.secrets, "api_key", return_value="K"), \
                mock.patch.object(dictation_translator, "_get_client", return_value=client), \
                mock.patch.object(dictation_translator, "on_model_replaced", lambda k, m: replaced.append((k, m))), \
                mock.patch.object(dictation_translator, "_notified", set()):
            result = dictation_translator.translate("やあ", self._config(translate_input_groq_model="llama-3.1-8b-instant"))
        self.assertEqual(result.text, "Hi")
        self.assertEqual(replaced, [("translate_input_groq_model", "qwen/qwen3.8-27b")])

    def test_gemini_uses_header_key_and_skips_thoughts(self):
        client = mock.Mock()
        client.post.return_value = _Resp(200, {"candidates": [{"content": {"parts": [
            {"text": "考え中", "thought": True}, {"text": "Hello"}]}, "finishReason": "STOP"}]})
        with mock.patch.object(dictation_translator.secrets, "api_key", return_value="GK"), \
                mock.patch.object(dictation_translator, "_get_client", return_value=client):
            result = dictation_translator.translate("こんにちは", self._config(translate_input_engine="gemini"))
        self.assertEqual(result.text, "Hello")
        url = client.post.call_args.args[0]
        self.assertNotIn("GK", url)  # キーは URL に載せない
        self.assertEqual(client.post.call_args.kwargs["headers"]["x-goog-api-key"], "GK")

    def test_gemini_max_tokens_is_failure(self):
        with self.assertRaises(ValueError):
            dictation_translator.gemini_translated_text({"candidates": [{"finishReason": "MAX_TOKENS"}]})

    def test_empty_input(self):
        self.assertEqual(dictation_translator.translate("  ", {}).failure_reason, "empty")


class TestTranslatePage(_SettingsWindowTestBase):
    def test_load_and_save(self):
        cm = self._config({"translate_once_key": "<shift_r>", "translate_input_target": "ko"})
        win = self._make_window(cm)
        self.assertIn("翻訳して入力", win._nav_row_of)
        self.assertEqual(win._translate_once_input.text(), "<shift_r>")
        self.assertEqual(win._translate_target_combo.currentData(), "ko")
        self.assertTrue(win._translate_engine_button.text().startswith("Groq · "))
        win._translate_engine_button.select(("gemini", "gemini-3.5-flash-lite"))
        win._translate_always_check.setChecked(True)
        with mock.patch("src.ui.settings_window.autostart.is_supported", return_value=False), \
             mock.patch.object(win, "close"), mock.patch("src.ui.settings_window.QMessageBox"):
            win._save_settings()
        from src.config.config_manager import ConfigManager
        reloaded = ConfigManager(config_path=cm.config_path)
        self.assertEqual(reloaded.get("translate_input_engine"), "gemini")
        self.assertEqual(reloaded.get("translate_input_gemini_model"), "gemini-3.5-flash-lite")
        self.assertTrue(reloaded.get("translate_input_enabled"))
        self.assertEqual(reloaded.get("translate_input_target"), "ko")

    def test_conflict_warning(self):
        cm = self._config({"hotkey1": {"hotkey": "<shift_r>", "backend": "groq"}, "translate_once_key": "<shift_r>"})
        win = self._make_window(cm)
        self.assertFalse(win._translate_warning_label.isHidden())
        self.assertIn("録音キー 1", win._translate_warning_label.text())

    def test_api_cost_section_renders(self):
        from src.core import api_usage
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = api_usage.ApiUsageStore(Path(tmp.name) / "u.json")
        self.addCleanup(store.flush)  # 一時ディレクトリの削除より先に裏の保存を終わらせる（後入れ先出し）
        store.record_audio("soniox", "stt-rt-v5", 3600)
        with mock.patch.object(api_usage, "shared", return_value=store):
            win = self._make_window(self._config({}))
            win._refresh_api_cost()
            self.assertEqual(win._api_cost_totals["today"][0].text(), "$0.12")
            win._set_api_cost_mode(api_usage.MODE_PAID)
            self.assertTrue(win._api_cost_mode_buttons[api_usage.MODE_PAID].isChecked())


class TestTranslateWarnings(unittest.TestCase):
    def test_rules(self):
        from src.ui.settings_widgets import translate_once_key_warnings as w
        self.assertEqual(w("<shift_r>", ["<f2>", "<f3>"], "", ""), [])
        self.assertIn("録音キー 2", w("<f3>", ["<f2>", "<f3>"], "", "")[0])
        self.assertIn("ハンズフリー", w("<shift_r>", ["<f2>", ""], "<shift_r>", "")[0])
        self.assertIn("再貼り付け", w("<ctrl>+<alt>+v", ["<f2>"], "", "<ctrl>+<alt>+v")[0])
        self.assertEqual(w("", ["<f2>"], "", ""), [])


if __name__ == "__main__":
    unittest.main()
