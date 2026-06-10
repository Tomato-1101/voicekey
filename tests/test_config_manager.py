"""ConfigManager のマイグレーション・バックエンド正規化・既定値テスト。"""

import os
import tempfile
import unittest

import yaml

from src.config.config_manager import ConfigManager, _deep_merge
from src.config.constants import DEFAULT_CONFIG


class TestDefaults(unittest.TestCase):
    def test_streaming_and_hud_present(self):
        self.assertIn("streaming_enabled", DEFAULT_CONFIG)
        self.assertIn("hud_enabled", DEFAULT_CONFIG)
        self.assertTrue(DEFAULT_CONFIG["streaming_enabled"])
        self.assertTrue(DEFAULT_CONFIG["hud_enabled"])

    def test_all_backends_have_default_model(self):
        self.assertEqual(
            set(DEFAULT_CONFIG["default_api_models"]),
            {"groq", "openai", "elevenlabs", "deepgram"},
        )


class TestDeepMerge(unittest.TestCase):
    def test_nested_merge(self):
        base = {"a": {"x": 1, "y": 2}, "b": 3}
        updates = {"a": {"y": 9}}
        self.assertEqual(_deep_merge(base, updates), {"a": {"x": 1, "y": 9}, "b": 3})


class TestBackendNormalize(unittest.TestCase):
    def test_valid_backends_kept(self):
        for backend in ("groq", "openai", "elevenlabs", "deepgram"):
            self.assertEqual(ConfigManager._normalize_backend(backend), backend)

    def test_invalid_falls_back_to_openai(self):
        self.assertEqual(ConfigManager._normalize_backend("local"), "openai")
        self.assertEqual(ConfigManager._normalize_backend("nonsense"), "openai")


class TestMigration(unittest.TestCase):
    def _manager_for(self, data):
        handle = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8")
        yaml.dump(data, handle, allow_unicode=True)
        handle.close()
        return ConfigManager(config_path=handle.name), handle.name

    def test_legacy_single_hotkey_migrates(self):
        mgr, path = self._manager_for({
            "hotkey": "<f4>",
            "hotkey_mode": "hold",
            "transcription_backend": "groq",
            "groq_model": "whisper-large-v3-turbo",
        })
        try:
            cfg = mgr.config
            self.assertIn("hotkey1", cfg)
            self.assertNotIn("hotkey", cfg)
            self.assertEqual(cfg["hotkey1"]["hotkey"], "<f4>")
            self.assertEqual(cfg["hotkey1"]["backend"], "groq")
            self.assertIn("hotkey2", cfg)
        finally:
            os.unlink(path)

    def test_deepgram_backend_preserved(self):
        mgr, path = self._manager_for({
            "hotkey1": {
                "hotkey": "<f2>", "hotkey_mode": "hold",
                "backend": "deepgram", "api_model": "nova-3",
            },
        })
        try:
            self.assertEqual(mgr.config["hotkey1"]["backend"], "deepgram")
            self.assertEqual(mgr.config["hotkey1"]["api_model"], "nova-3")
        finally:
            os.unlink(path)

    def test_streaming_defaults_merged_for_legacy_file(self):
        # 旧形式ファイルでも deep-merge で streaming/hud 既定が補完される
        mgr, path = self._manager_for({"language": "ja"})
        try:
            self.assertIn("streaming_enabled", mgr.config)
            self.assertIn("hud_enabled", mgr.config)
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
