"""ConfigManager のマイグレーション・バックエンド正規化・既定値テスト。"""

import copy
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

    def test_default_slots_use_soniox_and_groq(self):
        # 新規インストールの既定: スロット 1 = 押している間・Soniox / スロット 2 = 切り替え・Groq
        self.assertEqual(DEFAULT_CONFIG["hotkey1"]["hotkey"], "<f2>")
        self.assertEqual(DEFAULT_CONFIG["hotkey1"]["hotkey_mode"], "hold")
        self.assertEqual(DEFAULT_CONFIG["hotkey1"]["backend"], "soniox")
        self.assertEqual(DEFAULT_CONFIG["hotkey2"]["hotkey"], "<f3>")
        self.assertEqual(DEFAULT_CONFIG["hotkey2"]["hotkey_mode"], "toggle")
        self.assertEqual(DEFAULT_CONFIG["hotkey2"]["backend"], "groq")
        # エンジン別の既定モデル表は廃止（既定はカタログが持つ）
        self.assertNotIn("default_api_models", DEFAULT_CONFIG)
        self.assertEqual(DEFAULT_CONFIG["format_model"], "qwen/qwen3.8-27b")

    def test_missing_file_keeps_soniox_format_default(self):
        # 設定ファイルが無い新規インストールでは、移行済みの印を最初から持つ
        # （初回保存の後に「旧版からの移行」が走って既定を書き換えないため）
        path = os.path.join(tempfile.gettempdir(), "voicekey_nonexistent_config_abc.yaml")
        if os.path.exists(path):
            os.unlink(path)
        cfg = ConfigManager(config_path=path).config
        self.assertTrue(cfg["migrated_engine_catalog"])
        self.assertTrue(cfg["migrated_format_defaults"])
        self.assertEqual(cfg["hotkey1"]["backend"], "soniox")


class TestDeepMerge(unittest.TestCase):
    def test_nested_merge(self):
        base = {"a": {"x": 1, "y": 2}, "b": 3}
        updates = {"a": {"y": 9}}
        self.assertEqual(_deep_merge(base, updates), {"a": {"x": 1, "y": 9}, "b": 3})


class TestBackendNormalize(unittest.TestCase):
    def test_valid_backends_kept(self):
        for backend in ("soniox", "elevenlabs", "openai", "azure_mai", "openai_live", "groq"):
            self.assertEqual(ConfigManager._normalize_backend(backend), backend)

    def test_deepgram_moves_to_soniox(self):
        self.assertEqual(ConfigManager._normalize_backend("deepgram"), "soniox")

    def test_invalid_falls_back_to_groq(self):
        self.assertEqual(ConfigManager._normalize_backend("local"), "groq")
        self.assertEqual(ConfigManager._normalize_backend("nonsense"), "groq")


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
            # groq はいまも選べるエンジンなのでそのまま保持する
            self.assertEqual(cfg["hotkey1"]["backend"], "groq")
            self.assertIn("hotkey2", cfg)
        finally:
            os.unlink(path)

    def test_deepgram_moves_to_soniox_and_other_engines_kept(self):
        # Deepgram は Soniox へ（モデルは Soniox の既定）。それ以外のエンジン選択は保つ。
        # 提供の終わったモデル名（gpt-4o-transcribe）はそのエンジンの新しい既定へ。
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
            self.assertEqual(mgr.config["hotkey1"]["backend"], "openai")
            self.assertEqual(mgr.config["hotkey1"]["api_model"], "gpt-transcribe")
            self.assertEqual(mgr.config["hotkey2"]["backend"], "soniox")
            self.assertEqual(mgr.config["hotkey2"]["api_model"], "stt-rt-v5")
            # ホットキー・録音のしかたは触らない
            self.assertEqual(mgr.config["hotkey2"]["hotkey"], "<f3>")
            self.assertEqual(mgr.config["hotkey2"]["hotkey_mode"], "toggle")
        finally:
            os.unlink(path)

    def test_old_default_models_move_to_new_defaults(self):
        # 旧版の既定モデル名（空・scribe_v1）は新しい既定へ。エンジンは保つ
        mgr, path = self._manager_for({
            "default_api_models": {"groq": "whisper-large-v3-turbo", "deepgram": "nova-3"},
            "hotkey1": {
                "hotkey": "<f2>", "hotkey_mode": "hold",
                "backend": "elevenlabs", "api_model": "scribe_v1",
            },
            "hotkey2": {
                "hotkey": "<f3>", "hotkey_mode": "toggle",
                "backend": "groq", "api_model": "",
            },
        })
        try:
            self.assertEqual(mgr.config["hotkey1"]["backend"], "elevenlabs")
            self.assertEqual(mgr.config["hotkey1"]["api_model"], "scribe_v2")
            self.assertEqual(mgr.config["hotkey2"]["backend"], "groq")
            self.assertEqual(mgr.config["hotkey2"]["api_model"], "whisper-large-v3-turbo")
            self.assertEqual(mgr.config["hotkey2"]["format_preset"], "standard")
            # 使わなくなった既定モデル表は消える
            self.assertNotIn("default_api_models", mgr.config)
            self.assertTrue(mgr.config["migrated_engine_catalog"])
        finally:
            os.unlink(path)

    def test_scribe_v1_chosen_after_migration_is_kept(self):
        # 移行の後に利用者が選び直した scribe_v1 は、次の読み込みで戻さない
        mgr, path = self._manager_for({
            "migrated_engine_catalog": True,
            "hotkey1": {
                "hotkey": "<f2>", "hotkey_mode": "hold",
                "backend": "elevenlabs", "api_model": "scribe_v1",
            },
        })
        try:
            self.assertEqual(mgr.config["hotkey1"]["api_model"], "scribe_v1")
        finally:
            os.unlink(path)

    def test_custom_model_name_is_kept(self):
        # 一覧に無いだけのモデル名（手で書いた新しいモデル）は残す
        mgr, path = self._manager_for({
            "hotkey1": {
                "hotkey": "<f2>", "hotkey_mode": "hold",
                "backend": "groq", "api_model": "whisper-next-preview",
            },
        })
        try:
            self.assertEqual(mgr.config["hotkey1"]["api_model"], "whisper-next-preview")
        finally:
            os.unlink(path)

    def test_retired_format_model_moves_to_recommended(self):
        # 旧既定の整形モデルは新しい既定へ。利用者が選んだ現役のモデルは残す
        mgr, path = self._manager_for({"format_model": "llama-3.1-8b-instant"})
        try:
            self.assertEqual(mgr.config["format_model"], "qwen/qwen3.8-27b")
        finally:
            os.unlink(path)
        mgr, path = self._manager_for({"format_model": "openai/gpt-oss-120b"})
        try:
            self.assertEqual(mgr.config["format_model"], "openai/gpt-oss-120b")
        finally:
            os.unlink(path)

    def test_migration_is_idempotent(self):
        # 移行後の設定を保存して読み直しても、何度通しても結果は変わらない
        mgr, path = self._manager_for({
            "format_model": "llama-3.1-8b-instant",
            "hotkey1": {
                "hotkey": "<f2>", "hotkey_mode": "hold",
                "backend": "deepgram", "api_model": "nova-3", "format_enabled": True,
            },
            "hotkey2": {
                "hotkey": "<f3>", "hotkey_mode": "toggle",
                "backend": "elevenlabs", "api_model": "scribe_v1", "format_enabled": True,
            },
        })
        try:
            first = copy.deepcopy(mgr.config)
            self.assertTrue(mgr.save({}))
            second = ConfigManager(config_path=path).config
            self.assertEqual(second, first)
            self.assertTrue(ConfigManager(config_path=path).save({}))
            self.assertEqual(ConfigManager(config_path=path).config, first)
            # 同じ辞書にもう一度かけても変わらない
            again = copy.deepcopy(first)
            ConfigManager._normalize_engines(again)
            ConfigManager._migrate_format_defaults(again)
            self.assertEqual(again, first)
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

    def test_migrate_format_defaults_forces_deepgram_off_once(self):
        # モード別整形既定の一回限りマイグレーション（初回）。マーカー無しの既存ユーザーは、
        # 速さ優先のエンジン（旧 deepgram → Soniox）のスロットの整形を既定 OFF へ矯正する。groq は既定 ON のまま。
        mgr, path = self._manager_for({
            "hotkey1": {
                "hotkey": "<f2>", "hotkey_mode": "hold",
                "backend": "deepgram", "api_model": "nova-3", "format_enabled": True,
            },
            "hotkey2": {
                "hotkey": "<f3>", "hotkey_mode": "toggle",
                "backend": "groq", "api_model": "", "format_enabled": True,
            },
        })
        try:
            self.assertEqual(mgr.config["hotkey1"]["backend"], "soniox")
            self.assertFalse(mgr.config["hotkey1"]["format_enabled"])  # 速さ優先のエンジンは矯正
            self.assertTrue(mgr.config["hotkey2"]["format_enabled"])   # groq は据え置き
            self.assertTrue(mgr.config["migrated_format_defaults"])    # 矯正済みマーカー
        finally:
            os.unlink(path)

    def test_migrate_format_defaults_respects_user_choice_second_time(self):
        # 二度目（マーカー済み）＝ユーザーが deepgram の整形を ON に戻している → 尊重する。
        mgr, path = self._manager_for({
            "migrated_format_defaults": True,
            "hotkey1": {
                "hotkey": "<f2>", "hotkey_mode": "hold",
                "backend": "deepgram", "api_model": "nova-3", "format_enabled": True,
            },
        })
        try:
            self.assertTrue(mgr.config["hotkey1"]["format_enabled"])  # ON のまま尊重
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


class TestFirstRunFlag(unittest.TestCase):
    """初回起動判定（config_file_existed）と did_complete_onboarding 既定値（Phase 5）。"""

    def test_existing_file_marks_existed_true(self):
        # 起動時に settings.yaml が既にある＝既存ユーザー扱い
        handle = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8")
        yaml.dump({"language": "ja"}, handle, allow_unicode=True)
        handle.close()
        try:
            mgr = ConfigManager(config_path=handle.name)
            self.assertTrue(mgr.config_file_existed)
        finally:
            os.unlink(handle.name)

    def test_missing_file_marks_existed_false(self):
        # ファイルが無い＝完全な初回起動 → オンボーディングを出す対象
        path = os.path.join(tempfile.gettempdir(), "voicekey_nonexistent_config_xyz.yaml")
        if os.path.exists(path):
            os.unlink(path)
        mgr = ConfigManager(config_path=path)
        self.assertFalse(mgr.config_file_existed)

    def test_did_complete_onboarding_default_false(self):
        self.assertIn("did_complete_onboarding", DEFAULT_CONFIG)
        self.assertFalse(DEFAULT_CONFIG["did_complete_onboarding"])


if __name__ == "__main__":
    unittest.main()
