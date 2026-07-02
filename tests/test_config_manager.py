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
            # 製品版(release)の文字起こしは 2 択(deepgram/groq)。groq は有効なのでそのまま保持する
            self.assertEqual(cfg["hotkey1"]["backend"], "groq")
            self.assertIn("hotkey2", cfg)
        finally:
            os.unlink(path)

    def test_release_constrains_openai_to_groq_keeps_deepgram(self):
        # 製品版は openai を文字起こしに選べないため groq(正確性=普通入力の既定)へ移行し、
        # api_model は空にして default_api_models(whisper-large-v3-turbo) へフォールバックさせる。
        # deepgram(高速リアルタイム)は有効な選択肢なのでそのまま保持する。
        mgr, path = self._manager_for({
            "hotkey1": {
                "hotkey": "<f2>", "hotkey_mode": "hold",
                "backend": "openai", "api_model": "gpt-4o-transcribe",
            },
            "hotkey2": {
                "hotkey": "<f3>", "hotkey_mode": "toggle",
                "backend": "deepgram", "api_model": "nova-3",
            },
        })
        try:
            self.assertEqual(mgr.config["hotkey1"]["backend"], "groq")
            self.assertEqual(mgr.config["hotkey1"]["api_model"], "")
            # deepgram は選択肢に残したため維持される（api_model も保持）
            self.assertEqual(mgr.config["hotkey2"]["backend"], "deepgram")
            self.assertEqual(mgr.config["hotkey2"]["api_model"], "nova-3")
        finally:
            os.unlink(path)

    def test_release_migrates_elevenlabs_to_groq(self):
        # 旧「高精度」= elevenlabs はユーザーが選べる 2 択（deepgram/groq）から外したため、
        # スタンダード(groq)へ移行する。api_model は空にして default_api_models へフォールバックさせる。
        # （EL は enum としては残り、スタンダードのハンズフリー録音時に内部でのみ使われる）
        mgr, path = self._manager_for({
            "hotkey1": {
                "hotkey": "<f2>", "hotkey_mode": "hold",
                "backend": "elevenlabs", "api_model": "scribe_v1",
            },
        })
        try:
            self.assertEqual(mgr.config["hotkey1"]["backend"], "groq")
            self.assertEqual(mgr.config["hotkey1"]["api_model"], "")
        finally:
            os.unlink(path)

    def test_deepgram_backend_preserved(self):
        # deepgram(高速リアルタイム)は製品版の文字起こし選択肢に残したため維持される
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


class TestAtomicSave(unittest.TestCase):
    """#12: settings.yaml の保存は一時ファイル経由のアトミック置換にする。"""

    def _manager(self, data):
        handle = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8")
        yaml.dump(data, handle, allow_unicode=True)
        handle.close()
        return ConfigManager(config_path=handle.name), handle.name

    def test_save_round_trips_and_leaves_no_tmp(self):
        """通常保存は反映され、後に .tmp を残さない。"""
        mgr, path = self._manager({"language": "ja"})
        try:
            self.assertTrue(mgr.save({"language": "en"}))
            reloaded = ConfigManager(config_path=path)
            self.assertEqual(reloaded.config["language"], "en")
            self.assertFalse(os.path.exists(path + ".tmp"))  # 成功後は一時ファイルが消えている
        finally:
            os.unlink(path)

    def test_failed_write_keeps_original_intact(self):
        """書き込み途中で失敗しても、元の settings.yaml が破損・空にならない。"""
        from unittest import mock
        from src.config import config_manager as cm

        mgr, path = self._manager({"language": "ja"})
        try:
            original = open(path, encoding="utf-8").read()
            # yaml.dump が一時ファイルへの書き込み中に落ちる状況を模擬
            with mock.patch.object(cm.yaml, "dump", side_effect=OSError("disk full")):
                self.assertFalse(mgr.save({"language": "en"}))
            # 直書きなら truncate 済みで壊れるが、アトミック置換なので元ファイルは無傷
            self.assertEqual(open(path, encoding="utf-8").read(), original)
        finally:
            os.unlink(path)
            if os.path.exists(path + ".tmp"):
                os.unlink(path + ".tmp")


if __name__ == "__main__":
    unittest.main()
