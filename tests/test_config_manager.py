"""ConfigManager のマイグレーション・バックエンド正規化・既定値テスト。"""

import os
import tempfile
import unittest
import unittest.mock

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
        # 既定モデルは config/backends.py が正本（settings.yaml の default_api_models は使わない）
        from src.config import backends
        for backend in backends.SUPPORTED:
            self.assertTrue(backends.default_model(backend), backend)

    def test_fresh_install_slots_match_mac(self):
        # Mac の新規インストール（Apple ローカルが無い環境）と同じ: 1 = Soniox（整形 OFF）/ 2 = Groq
        self.assertEqual(DEFAULT_CONFIG["hotkey1"]["backend"], "soniox")
        self.assertFalse(DEFAULT_CONFIG["hotkey1"]["format_enabled"])
        self.assertEqual(DEFAULT_CONFIG["hotkey2"]["backend"], "groq")
        # 廃止された llama ではなく Mac と同じおすすめ
        self.assertEqual(DEFAULT_CONFIG["format_model"], "qwen/qwen3.8-27b")


class TestDeepMerge(unittest.TestCase):
    def test_nested_merge(self):
        base = {"a": {"x": 1, "y": 2}, "b": 3}
        updates = {"a": {"y": 9}}
        self.assertEqual(_deep_merge(base, updates), {"a": {"x": 1, "y": 9}, "b": 3})


class TestBackendNormalize(unittest.TestCase):
    def test_valid_backends_kept(self):
        for backend in ("groq", "openai", "elevenlabs", "deepgram", "soniox", "openai_live", "azure_mai"):
            self.assertEqual(ConfigManager._normalize_backend(backend), backend)

    def test_invalid_falls_back_to_groq(self):
        # 未知の値（新しい版で保存した値・Mac の apple_local など）は Groq へ（Mac と同じ）
        self.assertEqual(ConfigManager._normalize_backend("local"), "groq")
        self.assertEqual(ConfigManager._normalize_backend("apple_local"), "groq")
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
            # 製品版(release)の文字起こしは 2 択(deepgram/groq)。groq は有効なのでそのまま保持する
            self.assertEqual(cfg["hotkey1"]["backend"], "groq")
            self.assertIn("hotkey2", cfg)
        finally:
            os.unlink(path)

    def test_selectable_backends_and_deepgram_are_kept(self):
        # Mac と同じ選択肢（openai / elevenlabs 等）はそのまま。選択肢から外した deepgram も
        # 保存済みなら動かし続ける（本人の今の設定のエンジンを勝手に変えない）
        mgr, path = self._manager_for({
            "hotkey1": {
                "hotkey": "<f2>", "hotkey_mode": "hold",
                "backend": "openai", "api_model": "gpt-transcribe",
            },
            "hotkey2": {
                "hotkey": "<f3>", "hotkey_mode": "toggle",
                "backend": "deepgram", "api_model": "nova-3",
            },
        })
        try:
            self.assertEqual(mgr.config["hotkey1"]["backend"], "openai")
            self.assertEqual(mgr.config["hotkey1"]["api_model"], "gpt-transcribe")
            self.assertEqual(mgr.config["hotkey2"]["backend"], "deepgram")
            self.assertEqual(mgr.config["hotkey2"]["api_model"], "nova-3")
        finally:
            os.unlink(path)

    def test_unknown_backend_moves_to_groq_with_default_model(self):
        # 未知のエンジンだけ Groq へ移し、旧エンジンのモデル名は意味が無いので空（＝既定）にする
        mgr, path = self._manager_for({
            "hotkey1": {
                "hotkey": "<f9>", "hotkey_mode": "toggle",
                "backend": "apple_local", "api_model": "オンデバイス音声認識",
                "api_prompt": "固有名詞",
            },
        })
        try:
            self.assertEqual(mgr.config["hotkey1"]["backend"], "groq")
            self.assertEqual(mgr.config["hotkey1"]["api_model"], "")
            # ホットキー・モード・プロンプトなど他の設定は保持する
            self.assertEqual(mgr.config["hotkey1"]["hotkey"], "<f9>")
            self.assertEqual(mgr.config["hotkey1"]["hotkey_mode"], "toggle")
            self.assertEqual(mgr.config["hotkey1"]["api_prompt"], "固有名詞")
        finally:
            os.unlink(path)

    def test_retired_models_migrate_once(self):
        # 選択肢から外したモデルは一回限りで既定（空）へ。エンジンは変えない
        mgr, path = self._manager_for({
            "hotkey1": {"hotkey": "<f2>", "backend": "groq", "api_model": "whisper-large-v3"},
            "hotkey2": {"hotkey": "<f3>", "backend": "deepgram", "api_model": "nova-2"},
        })
        try:
            self.assertEqual(mgr.config["hotkey1"]["backend"], "groq")
            self.assertEqual(mgr.config["hotkey1"]["api_model"], "")
            self.assertEqual(mgr.config["hotkey2"]["backend"], "deepgram")
            self.assertEqual(mgr.config["hotkey2"]["api_model"], "")
            self.assertTrue(mgr.config["migrated_retired_models_v19"])
        finally:
            os.unlink(path)
        # 移行済みなら、利用者が自由入力で選び直した旧モデル名を消さない
        mgr, path = self._manager_for({
            "migrated_retired_models_v19": True,
            "hotkey1": {"hotkey": "<f2>", "backend": "groq", "api_model": "whisper-large-v3"},
        })
        try:
            self.assertEqual(mgr.config["hotkey1"]["api_model"], "whisper-large-v3")
        finally:
            os.unlink(path)

    def test_retired_llama_format_model_migrates(self):
        # 2026-10 に廃止された llama 系の整形モデルはおすすめへ（一回限り）
        mgr, path = self._manager_for({"format_model": "llama-3.1-8b-instant"})
        try:
            self.assertEqual(mgr.config["format_model"], "qwen/qwen3.8-27b")
            self.assertTrue(mgr.config["migrated_llm_models_v20"])
        finally:
            os.unlink(path)
        mgr, path = self._manager_for({"format_model": "openai/gpt-oss-20b"})
        try:
            self.assertEqual(mgr.config["format_model"], "openai/gpt-oss-20b")  # 使えるモデルは触らない
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

    def test_migrate_format_defaults_forces_deepgram_off_once(self):
        # モード別整形既定の一回限りマイグレーション（初回）。マーカー無しの既存ユーザーは、
        # リアルタイム(deepgram)スロットの整形を既定 OFF へ矯正する。スタンダード(groq)は既定 ON のまま。
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
            self.assertFalse(mgr.config["hotkey1"]["format_enabled"])  # deepgram は矯正
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


class TestCorruptSettings(unittest.TestCase):
    """壊れた settings.yaml は退避して既定で始め、ホットリロード中の書きかけは無視する。"""

    def setUp(self):
        from src.utils import corrupt_file
        self.notices = []
        corrupt_file._reset_for_tests()
        corrupt_file.set_notice_handler(self.notices.append)
        self.addCleanup(corrupt_file._reset_for_tests)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "settings.yaml")

    def test_corrupt_file_is_quarantined_at_startup(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("hotkey1: [unclosed\n  - : :")
        mgr = ConfigManager(config_path=self.path)
        # 既定値で始め、壊れたファイルは .corrupt-* として残る
        self.assertEqual(mgr.config["language"], DEFAULT_CONFIG["language"])
        self.assertFalse(os.path.exists(self.path))
        moved = [n for n in os.listdir(self.tmp.name) if n.startswith("settings.yaml.corrupt-")]
        self.assertEqual(len(moved), 1)
        self.assertEqual(len(self.notices), 1)
        # 退避できたので保存は通る
        self.assertTrue(mgr.save({"language": "en"}))

    def test_save_is_blocked_when_quarantine_fails(self):
        from src.utils import corrupt_file
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("- just a list\n")
        original = open(self.path, encoding="utf-8").read()
        with unittest.mock.patch.object(corrupt_file, "move_file", side_effect=OSError("locked")):
            mgr = ConfigManager(config_path=self.path)
        # 退避できなければ元ファイルを上書きしない
        self.assertFalse(mgr.save({"language": "en"}))
        self.assertEqual(open(self.path, encoding="utf-8").read(), original)

    def test_hot_reload_of_half_written_file_keeps_current_config(self):
        with open(self.path, "w", encoding="utf-8") as f:
            yaml.dump({"language": "en"}, f)
        mgr = ConfigManager(config_path=self.path)
        self.assertEqual(mgr.config["language"], "en")
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("language: [書きかけ")
        os.utime(self.path, (mgr.last_mtime + 5, mgr.last_mtime + 5))
        self.assertFalse(mgr.reload_if_changed())
        # 既定値に戻さず、退避もしない（手で編集している途中のため）
        self.assertEqual(mgr.config["language"], "en")
        self.assertTrue(os.path.exists(self.path))
        self.assertEqual(self.notices, [])


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
