"""設定画面の作り直し（Mac 2026-10-19 の構成に合わせた Windows 版）のテスト。

サイドバーの見出し・録音キーの右端の表示・エンジン＋モデルの 1 ポップアップ・プロンプトページ・
辞書と数字・保存形式が変わっていないことを、オフスクリーンの Qt で確かめる。
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import unittest
from unittest import mock

from PySide6.QtCore import Qt

from tests.test_settings_window import _SettingsWindowTestBase  # noqa: E402

from src.config.config_manager import ConfigManager  # noqa: E402
from src.core import text_formatter  # noqa: E402


def _save(win):
    with mock.patch("src.ui.settings_window.autostart.is_supported", return_value=False), \
         mock.patch.object(win, "close"), \
         mock.patch("src.ui.settings_window.QMessageBox"):
        win._save_settings()


class TestSidebar(_SettingsWindowTestBase):
    def test_sections_and_pages(self):
        win = self._make_window(self._config({"language": "ja"}))
        titles = [win._nav.item(i).text() for i in range(win._nav.count())]
        for title in ("はじめに", "API キー", "音声入力", "入力の基本", "録音キー 1", "録音キー 2",
                      "プロンプト", "辞書と数字", "アプリ", "一般", "バージョン情報"):
            self.assertIn(title, titles)
        # 見出しの順（Mac と同じ）
        self.assertLess(titles.index("はじめに"), titles.index("音声入力"))
        self.assertLess(titles.index("音声入力"), titles.index("アプリ"))
        # 見出し行は選べない
        header_row = titles.index("音声入力")
        self.assertFalse(win._nav.item(header_row).flags() & Qt.ItemFlag.ItemIsSelectable)

    def test_slot_rows_show_assigned_key(self):
        from src.ui.settings_widgets import ROLE_TRAILING
        win = self._make_window(self._config({
            "hotkey1": {"hotkey": "<ctrl_r>", "backend": "groq"},
            "hotkey2": {"hotkey": "", "backend": "groq"},
        }))
        row1 = win._nav_row_of["録音キー 1"]
        row2 = win._nav_row_of["録音キー 2"]
        self.assertEqual(win._nav.item(row1).data(ROLE_TRAILING), "右Ctrl")
        self.assertEqual(win._nav.item(row2).data(ROLE_TRAILING), "未割り当て")

    def test_select_api_keys(self):
        win = self._make_window(self._config({"language": "ja"}))
        win.select_api_keys()
        self.assertEqual(win._page_title.text(), "API キー")
        self.assertTrue(win._page_subtitle.text())


class TestEnginePopup(_SettingsWindowTestBase):
    def test_keeps_saved_deepgram_and_saves_choice(self):
        cm = self._config({
            "hotkey1": {"hotkey": "<f2>", "backend": "deepgram", "api_model": "nova-3"},
            "hotkey2": {"hotkey": "<f3>", "backend": "groq", "api_model": ""},
        })
        win = self._make_window(cm)
        # 保存中の Deepgram は選択肢から外れていても選択中のまま見える
        self.assertEqual(win._engine1_button.text(), "Deepgram · nova-3")
        self.assertIn(("elevenlabs", "scribe_v1"), win._engine2_button.values())
        # 1 回で エンジン＋モデル が決まる
        win._engine2_button.select(("elevenlabs", "scribe_v1"))
        self.assertEqual(win._engine2_button.text(), "ElevenLabs · scribe_v1")
        _save(win)
        reloaded = ConfigManager(config_path=cm.config_path)
        self.assertEqual(reloaded.get("hotkey1")["backend"], "deepgram")  # 勝手に変えない
        self.assertEqual(reloaded.get("hotkey2")["backend"], "elevenlabs")
        self.assertEqual(reloaded.get("hotkey2")["api_model"], "scribe_v1")

    def test_default_model_saved_as_empty(self):
        cm = self._config({"hotkey1": {"hotkey": "<f2>", "backend": "groq"}})
        win = self._make_window(cm)
        win._engine1_button.select(("soniox", "stt-rt-v5"))
        _save(win)
        reloaded = ConfigManager(config_path=cm.config_path)
        self.assertEqual(reloaded.get("hotkey1")["backend"], "soniox")
        self.assertEqual(reloaded.get("hotkey1")["api_model"], "")


class TestPromptPage(_SettingsWindowTestBase):
    def test_default_prompt_shown_and_saved_as_empty(self):
        cm = self._config({"format_auto_prompt": ""})
        win = self._make_window(cm)
        self.assertEqual(win._prompt_edit.toPlainText(), text_formatter.DEFAULT_FORMAT_PROMPT)
        self.assertFalse(win._prompt_reset_btn.isEnabled())
        _save(win)
        self.assertEqual(ConfigManager(config_path=cm.config_path).get("format_auto_prompt"), "")

    def test_template_and_undo(self):
        cm = self._config({})
        win = self._make_window(cm)
        win._template_button.select("bullets")
        self.assertIn("箇条書き", win._prompt_edit.toPlainText())
        self.assertIn("箇条書き（リストに整理）", win._prompt_status.text())
        self.assertFalse(win._prompt_undo_btn.isHidden())
        win._undo_prompt_replace()
        self.assertEqual(win._prompt_edit.toPlainText(), text_formatter.DEFAULT_FORMAT_PROMPT)

    def test_custom_prompt_and_model_saved(self):
        cm = self._config({"format_model": "openai/gpt-oss-20b"})
        win = self._make_window(cm)
        self.assertEqual(win._format_model_combo.currentData(), "openai/gpt-oss-20b")
        win._prompt_edit.setPlainText("短くまとめて")
        _save(win)
        reloaded = ConfigManager(config_path=cm.config_path)
        self.assertEqual(reloaded.get("format_auto_prompt"), "短くまとめて")
        self.assertEqual(reloaded.get("format_model"), "openai/gpt-oss-20b")

    def test_slot_toggles_are_shared(self):
        cm = self._config({"hotkey1": {"hotkey": "<f2>", "backend": "groq", "format_enabled": False}})
        win = self._make_window(cm)
        win._prompt_slot1_check.setChecked(True)
        self.assertTrue(win._format1_check.isChecked())
        win._format1_check.setChecked(False)
        self.assertFalse(win._prompt_slot1_check.isChecked())


class TestDictionaryAndKeys(_SettingsWindowTestBase):
    def test_add_rule_and_count(self):
        cm = self._config({"replacements": [{"from": "a", "to": "b", "enabled": True}]})
        win = self._make_window(cm)
        self.assertIn("1 件", win._rules_title.text())
        win._new_rule_from.setText("ボイスキー")
        win._new_rule_to.setText("voicekey")
        win._add_rule_from_inputs()
        self.assertIn("2 件", win._rules_title.text())
        _save(win)
        rules = ConfigManager(config_path=cm.config_path).get("replacements")
        self.assertIn({"from": "ボイスキー", "to": "voicekey", "enabled": True}, rules)

    def test_repaste_and_history_saved(self):
        cm = self._config({})
        win = self._make_window(cm)
        self.assertEqual(win._repaste_input.text(), "")  # Windows の既定は未設定
        win._repaste_input.setText("<ctrl>+<shift>+v")
        win._history_enabled_check.setChecked(False)
        _save(win)
        reloaded = ConfigManager(config_path=cm.config_path)
        self.assertEqual(reloaded.get("repaste_key"), "<ctrl>+<shift>+v")
        self.assertFalse(reloaded.get("history_enabled"))

    def test_duplicate_hotkey_warning(self):
        cm = self._config({
            "hotkey1": {"hotkey": "<f2>", "backend": "groq"},
            "hotkey2": {"hotkey": "<f3>", "backend": "groq"},
        })
        win = self._make_window(cm)
        self.assertTrue(win._hotkey1_warning.isHidden())
        win._hotkey2_input.setText("<f2>")
        self.assertFalse(win._hotkey1_warning.isHidden())


class TestApiKeyStatus(_SettingsWindowTestBase):
    def test_env_source_shown_without_keyring(self):
        cm = self._config({})
        win = self._make_window(cm)
        with mock.patch.dict(os.environ, {"SONIOX_API_KEY": "x"}), \
                mock.patch("src.ui.settings_window.secrets.is_keyring_available", return_value=False):
            win._refresh_api_key_status("soniox")
        self.assertIn("環境変数", win._api_key_status["soniox"].text())
        # アプリに保存した値でなければ削除できない
        self.assertFalse(win._api_key_delete["soniox"].isEnabled())

    def test_deepgram_row_only_when_used(self):
        win = self._make_window(self._config({"hotkey1": {"hotkey": "<f2>", "backend": "groq"}}))
        self.assertNotIn("deepgram", win._api_key_status)
        win = self._make_window(self._config({"hotkey1": {"hotkey": "<f2>", "backend": "deepgram"}}))
        self.assertIn("deepgram", win._api_key_status)

    def test_failed_save_keeps_input(self):
        win = self._make_window(self._config({}))
        win._api_key_inputs["groq"].setText("gsk_x")
        with mock.patch("src.ui.settings_window.secrets.save_item", return_value=False):
            win._save_api_key("groq")
        self.assertEqual(win._api_key_inputs["groq"].text(), "gsk_x")
        self.assertFalse(win._api_key_error["groq"].isHidden())


if __name__ == "__main__":
    unittest.main()
