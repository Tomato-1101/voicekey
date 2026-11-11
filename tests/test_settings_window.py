"""設定ウィンドウ（src/ui/settings_window.py と src/ui/pages/）のテスト。

保存ボタンを無くし、変えた項目をその場で保存する作りにしたので、旧テストの意味を次の形で引き継ぐ:
1. 開き直すと永続 config から読み直す（窓は使い回すため。閉じている間の外からの変更を拾う）。
2. 自動起動（レジストリ）は切り替えが成功したときだけ反映し、失敗したらスイッチを元に戻す。
3. 録音キーを「未割り当て」にすると空のまま保存され、既定へ戻らない。もう片方は保たれる。
4. スロットごとのエンジン・モデル・整え方の選択と保存。
5. API キーページが全エンジンの鍵を持つ。
6. 「Mac と履歴を共有」の読み込みと、URL 末尾の「/」を外した保存。
加えて、新しい作りの性質（すぐ保存・入力は手が止まってから保存・ページは開くまで作らない・最後のページを覚える）。

Qt はオフスクリーンで動かし、keyring / 音声デバイス / レジストリ（autostart）は実体に触れないようモックする
（lessons 2026-06-12: テストで実 keyring を読まない。Windows 版のサービス名は Mac の実 Keychain と同じ）。
"""

import os

# PySide6 の import より前に必ずオフスクリーン指定する
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import tempfile
import unittest
from unittest import mock

import yaml
from PySide6.QtCore import QEvent
from PySide6.QtGui import QShowEvent
from PySide6.QtWidgets import QApplication

from src.config.config_manager import ConfigManager

# QApplication はプロセスに 1 つ。各テストで使い回す
_app = QApplication.instance() or QApplication([])


class _FakeSecrets:
    """資格情報ストアの偽物（メモリ上の辞書だけ。実 keyring には一切触れない）。"""

    def __init__(self, available=True):
        self.available = available
        self.store = {}

    def patches(self):
        return [
            mock.patch("src.utils.secrets.is_keyring_available", side_effect=lambda: self.available),
            mock.patch("src.utils.secrets.get_api_key", side_effect=lambda s: self.store.get(s)),
            mock.patch("src.utils.secrets.set_api_key", side_effect=self._set),
            mock.patch("src.utils.secrets.delete_api_key", side_effect=self._delete),
        ]

    def _set(self, service, key):
        self.store[service] = key
        return True

    def _delete(self, service):
        self.store.pop(service, None)
        return True


class _SettingsWindowTestBase(unittest.TestCase):
    def setUp(self):
        self._paths = []
        self._windows = []
        self.secrets = _FakeSecrets(available=False)
        patches = self.secrets.patches() + [
            mock.patch("src.core.audio_recorder.AudioRecorder.list_input_devices", return_value=[]),
            mock.patch("src.utils.autostart.is_supported", return_value=True),
            mock.patch("src.utils.autostart.is_enabled", return_value=False),
            mock.patch("src.utils.autostart.set_enabled", return_value=True),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self):
        for win in self._windows:
            win.close()
            win.deleteLater()
        # イベントループを回さないテストなので、ここで実際に捨てる（残すと外観の切り替えが
        # 生きている窓すべてを塗り直し、テストを重ねるほど遅くなる）
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        for path in self._paths:
            if os.path.exists(path):
                os.unlink(path)

    def _config(self, data):
        handle = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8")
        yaml.dump(data, handle, allow_unicode=True)
        handle.close()
        self._paths.append(handle.name)
        return ConfigManager(config_path=handle.name)

    def _make_window(self, cm, history_sync=None, updater=None):
        from src.ui.settings_window import SettingsWindow

        win = SettingsWindow(config_manager=cm, history_sync=history_sync, updater=updater)
        self._windows.append(win)
        return win

    @staticmethod
    def _reloaded(cm):
        """保存ファイルを新しいマネージャで読み直す（メモリ上の値でなく書き込まれた中身を見る）。"""
        return ConfigManager(config_path=cm.config_path)


class TestReopenReloadsPersistedConfig(_SettingsWindowTestBase):
    """窓は使い回すので、開き直すと永続 config から読み直す。"""

    def test_showevent_reloads_value_changed_elsewhere(self):
        cm = self._config({"language": "ja", "handsfree_key": "<f9>"})
        win = self._make_window(cm)
        win.select("general", animate=False)
        page = win.page("general")
        self.assertEqual(page.handsfree_input.value(), "<f9>")

        # 窓を閉じている間にトレイや設定ファイルで変わった
        cm.save({"handsfree_key": ""})
        win.showEvent(QShowEvent())

        self.assertEqual(page.handsfree_input.value(), "")

    def test_reopen_applies_saved_appearance(self):
        from src.ui import theme
        from src.ui.theme import Theme

        cm = self._config({"language": "ja", "appearance": "dark"})
        self._make_window(cm)
        self.assertEqual(Theme.instance().mode, theme.APPEARANCE_DARK)
        Theme.instance().set_mode(theme.APPEARANCE_SYSTEM)

    def test_appearance_migrates_from_dark_mode_without_writing_it(self):
        from src.ui import theme
        from src.ui.pages.general import appearance_from

        cm = self._config({"language": "ja", "dark_mode": True})
        self.assertEqual(appearance_from(cm.get), theme.APPEARANCE_DARK)
        self.assertEqual(appearance_from(self._config({"dark_mode": False}).get), theme.APPEARANCE_SYSTEM)

        win = self._make_window(cm)
        win.select("general", animate=False)
        picker = win.page("general").appearance_picker
        picker.setCurrentIndex(picker.findData(theme.APPEARANCE_LIGHT))
        saved = self._reloaded(cm)
        self.assertEqual(saved.get("appearance"), theme.APPEARANCE_LIGHT)
        self.assertTrue(saved.get("dark_mode"))  # 旧キーは書き換えない
        from src.ui.theme import Theme

        Theme.instance().set_mode(theme.APPEARANCE_SYSTEM)


class TestAutostart(_SettingsWindowTestBase):
    """自動起動は切り替えが成功したときだけ反映し、失敗ならスイッチを戻す。"""

    def _general(self):
        win = self._make_window(self._config({"language": "ja"}))
        win.select("general", animate=False)
        return win.page("general")

    def test_failure_reverts_switch(self):
        page = self._general()
        with mock.patch("src.utils.autostart.set_enabled", return_value=False) as set_enabled:
            page.autostart_switch.setChecked(True)
        set_enabled.assert_called_once_with(True)
        self.assertFalse(page.autostart_switch.isChecked())

    def test_success_applies(self):
        page = self._general()
        with mock.patch("src.utils.autostart.set_enabled", return_value=True) as set_enabled:
            page.autostart_switch.setChecked(True)
        set_enabled.assert_called_once_with(True)
        self.assertTrue(page.autostart_switch.isChecked())

    def test_opening_page_does_not_touch_autostart(self):
        with mock.patch("src.utils.autostart.set_enabled") as set_enabled:
            self._general()
        set_enabled.assert_not_called()


class TestUnassignedHotkeyPersists(_SettingsWindowTestBase):
    """録音キーを「未割り当て」（空）にすると空のまま永続し、既定へ戻らない。

    Mac 版 SlotConfigTests.testUnassignedSlotPersistsAndDoesNotRevertToDefault と対応。
    """

    def test_clearing_slot_hotkey_persists_as_empty(self):
        cm = self._config({
            "language": "ja",
            "hotkey1": {"hotkey": "<f2>", "hotkey_mode": "hold", "backend": "groq"},
            "hotkey2": {"hotkey": "<f3>", "hotkey_mode": "toggle", "backend": "groq"},
        })
        win = self._make_window(cm)
        win.select("slot1", animate=False)
        win.page("slot1").unassign_button.click()

        reloaded = self._reloaded(cm)
        self.assertEqual(reloaded.get("hotkey1")["hotkey"], "")
        self.assertEqual(reloaded.get("hotkey2")["hotkey"], "<f3>")

        # 開き直しても空のまま（既定の F2 に戻らない）
        win.showEvent(QShowEvent())
        self.assertEqual(win.page("slot1").hotkey_input.value(), "")


class TestEngineAndModelSelection(_SettingsWindowTestBase):
    """スロットごとにエンジン・モデル・整え方を選べ、保存した値がそのまま読み直せる。"""

    def test_engine_choices_follow_catalog(self):
        from src.core import engines

        win = self._make_window(self._config({"language": "ja"}))
        picker = win.page("slot1").engine_picker
        self.assertEqual([picker.itemData(i) for i in range(picker.count())], list(engines.SELECTABLE_IDS))

    def test_loads_saved_engine_model_and_preset(self):
        cm = self._config({
            "language": "ja",
            # 移行済みの設定（移行前のファイルだと、旧既定だった scribe_v1 は scribe_v2 へ移される）
            "migrated_engine_catalog": True,
            "hotkey1": {
                "hotkey": "<f2>", "hotkey_mode": "hold", "backend": "elevenlabs",
                "api_model": "scribe_v1", "format_preset": "bullets",
            },
            "hotkey2": {"hotkey": "<f3>", "hotkey_mode": "toggle", "backend": "deepgram"},
        })
        win = self._make_window(cm)
        slot1, slot2 = win.page("slot1"), win.page("slot2")
        self.assertEqual(slot1.engine_picker.currentData(), "elevenlabs")
        self.assertEqual(slot1.model_picker.currentData(), "scribe_v1")
        self.assertEqual(slot1.preset_picker.currentData(), "bullets")
        # 外したエンジン（Deepgram）は Soniox とその既定モデルで表示する
        self.assertEqual(slot2.engine_picker.currentData(), "soniox")
        self.assertEqual(slot2.model_picker.currentData(), "stt-rt-v5")

    def test_changing_engine_resets_model_to_default(self):
        win = self._make_window(self._config({"language": "ja"}))
        page = win.page("slot1")
        page.engine_picker.setCurrentIndex(page.engine_picker.findData("elevenlabs"))
        models = page.model_picker
        self.assertEqual([models.itemData(i) for i in range(models.count())], ["scribe_v2", "scribe_v1"])
        self.assertEqual(models.currentData(), "scribe_v2")

    def test_changes_persist_immediately(self):
        cm = self._config({"language": "ja"})
        win = self._make_window(cm)
        page = win.page("slot2")
        page.engine_picker.setCurrentIndex(page.engine_picker.findData("elevenlabs"))
        page.model_picker.setCurrentIndex(page.model_picker.findData("scribe_v1"))
        page.preset_picker.setCurrentIndex(page.preset_picker.findData("clean"))

        slot = self._reloaded(cm).get("hotkey2")
        self.assertEqual(slot["backend"], "elevenlabs")
        self.assertEqual(slot["api_model"], "scribe_v1")
        self.assertEqual(slot["format_preset"], "clean")

    def test_api_key_fields_cover_every_engine(self):
        from src.core import engines

        win = self._make_window(self._config({"language": "ja"}))
        services = set(win.page("api_keys").inputs)
        for eng in engines.ENGINES:
            self.assertIn(eng.keyring_service, services, eng.id)
        self.assertIn("voicekey.AzureSpeechEndpoint", services)


class _FakeHistorySync:
    """history_sync.HistorySync の最小フェイク（状態を返すだけ。通信しない）。"""

    def __init__(self, status=None):
        self._status = status or {
            "enabled": True, "configured": True, "pending": 0,
            "last_sync": None, "token_invalid": False, "last_error": None,
        }
        self.applied = 0
        self.enabled = True

    def status(self):
        return dict(self._status)

    def merged_items(self, local_items):
        return list(local_items)

    def request_fetch(self):
        pass

    def apply_config(self):
        self.applied += 1


class TestHistorySyncSettingsUI(_SettingsWindowTestBase):
    """一般 › 履歴 の「Mac と履歴を共有」。"""

    def _general(self, cm, history_sync=None):
        win = self._make_window(cm, history_sync=history_sync)
        win.select("general", animate=False)
        return win, win.page("general")

    def test_builds_with_history_sync_none(self):
        _win, page = self._general(self._config({"language": "ja"}))
        self.assertFalse(page.sync_switch.isChecked())
        self.assertEqual(page.sync_url_input.text(), "")
        self.assertEqual(page.sync_status.text(), "")

    def test_loads_existing_history_sync_config(self):
        cm = self._config({"language": "ja", "history_sync": {"enabled": True, "url": "https://example.workers.dev"}})
        _win, page = self._general(cm, _FakeHistorySync())
        self.assertTrue(page.sync_switch.isChecked())
        self.assertEqual(page.sync_url_input.text(), "https://example.workers.dev")
        self.assertEqual(page.sync_status.text(), "まだ同期していません")

    def test_url_saved_without_trailing_slash(self):
        cm = self._config({"language": "ja"})
        _win, page = self._general(cm)
        page.sync_switch.setChecked(True)
        page.sync_url_input.setText("https://example.workers.dev/")
        page.ctx.flush()

        saved = self._reloaded(cm).get("history_sync")
        self.assertTrue(saved["enabled"])
        self.assertEqual(saved["url"], "https://example.workers.dev")

    def test_token_save_and_delete_use_fake_store(self):
        from src.utils import secrets

        self.secrets.available = True
        sync = _FakeHistorySync()
        _win, page = self._general(self._config({"language": "ja"}), sync)
        self.assertEqual(page.sync_token_status.text(), "未登録")
        self.assertFalse(page.sync_token_save.isEnabled())
        self.assertFalse(page.sync_token_delete.isEnabled())

        page.sync_token_input.setText("dummy-token")
        self.assertTrue(page.sync_token_save.isEnabled())
        page.sync_token_save.click()
        self.assertEqual(self.secrets.store[secrets.SERVICE_SYNC_TOKEN], "dummy-token")
        self.assertEqual(page.sync_token_input.text(), "")  # 値は画面に残さない
        self.assertEqual(page.sync_token_status.text(), "共有トークンを保存しました")
        self.assertTrue(page.sync_token_delete.isEnabled())
        self.assertEqual(sync.applied, 1)

        page.sync_token_delete.click()
        self.assertNotIn(secrets.SERVICE_SYNC_TOKEN, self.secrets.store)
        self.assertEqual(page.sync_token_status.text(), "共有トークンを削除しました")
        self.assertFalse(page.sync_token_delete.isEnabled())

    def test_refresh_history_views_updates_created_general_page(self):
        cm = self._config({"language": "ja", "history_sync": {"enabled": True, "url": "https://x"}})
        sync = _FakeHistorySync()
        win, page = self._general(cm, sync)
        sync._status["token_invalid"] = True
        win.refresh_history_views()
        self.assertEqual(page.sync_status.text(), "トークンが無効です（保存し直してください）")


class TestImmediateSave(_SettingsWindowTestBase):
    """保存ボタンは無く、変えた項目はその場で保存する。"""

    def test_commit_saves_immediately_and_emits(self):
        cm = self._config({"language": "ja"})
        win = self._make_window(cm)
        received = []
        win.settings_saved.connect(lambda: received.append(True))
        win.select("general", animate=False)
        page = win.page("general")
        page.sound_switch.setChecked(not page.sound_switch.isChecked())

        self.assertEqual(self._reloaded(cm).get("sound_effects_enabled"), page.sound_switch.isChecked())
        self.assertEqual(len(received), 1)

    def test_commit_later_saves_on_flush(self):
        cm = self._config({"language": "ja", "auto_enter_delay_ms": 50})
        win = self._make_window(cm)
        win.select("general", animate=False)
        page = win.page("general")
        page.delay_input.setValue(120)

        # 手が止まるまでは書かない
        self.assertEqual(self._reloaded(cm).get("auto_enter_delay_ms"), 50)
        # ページを移る・閉じるときに書く
        win.select("about", animate=False)
        self.assertEqual(self._reloaded(cm).get("auto_enter_delay_ms"), 120)

    def test_hide_flushes_pending_input(self):
        cm = self._config({"language": "ja", "auto_enter_delay_ms": 50})
        win = self._make_window(cm)
        win.select("general", animate=False)
        win.page("general").delay_input.setValue(300)
        win.show()
        win.hide()
        self.assertEqual(self._reloaded(cm).get("auto_enter_delay_ms"), 300)

    def test_loading_page_does_not_write(self):
        cm = self._config({"language": "ja"})
        win = self._make_window(cm)
        with mock.patch.object(cm, "save", wraps=cm.save) as save:
            for key in ("api_keys", "slot1", "slot2", "dictionary", "general", "about"):
                page = win.page(key)  # 作る（初めて開く）ときも書き込まない
                if hasattr(page, "refresh"):
                    page.refresh()
        save.assert_not_called()

    def test_dictionary_rule_saved(self):
        cm = self._config({"language": "ja"})
        win = self._make_window(cm)
        page = win.page("dictionary")
        self.assertEqual(page.stack.currentIndex(), 0)
        page.add_button.click()
        self.assertEqual(page.stack.currentIndex(), 1)
        row = page.rows[0]
        row.from_input.setText("ぼいすきー")
        row.to_input.setText("voicekey")
        page.ctx.flush()
        self.assertEqual(
            self._reloaded(cm).get("replacements"),
            [{"from": "ぼいすきー", "to": "voicekey", "enabled": True}],
        )


class TestPagesAndNavigation(_SettingsWindowTestBase):
    """ページの並び・遅延生成・最後に開いたページの復元。"""

    def test_sidebar_order(self):
        win = self._make_window(self._config({"language": "ja"}))
        self.assertEqual(
            win.keys(), ["home", "api_keys", "slot1", "slot2", "dictionary", "translate", "meeting", "general", "about"]
        )

    def test_pages_are_created_lazily(self):
        win = self._make_window(self._config({"language": "ja", "last_settings_page": "about"}))
        self.assertIsNotNone(win.created_page("about"))
        for key in ("home", "api_keys", "slot1", "slot2", "dictionary", "general"):
            self.assertIsNone(win.created_page(key), key)
        win.select("general", animate=False)
        self.assertIsNotNone(win.created_page("general"))

    def test_last_page_is_restored(self):
        cm = self._config({"language": "ja", "last_settings_page": "about"})
        win = self._make_window(cm)
        win.select("dictionary", animate=False)
        self.assertEqual(self._reloaded(cm).get("last_settings_page"), "dictionary")

        again = self._make_window(self._reloaded(cm))
        self.assertEqual(again.current_key(), "dictionary")

    def test_unknown_last_page_falls_back_to_home_and_select_home(self):
        win = self._make_window(self._config({"language": "ja", "last_settings_page": "stats"}))
        self.assertEqual(win.current_key(), "home")
        win.select("about", animate=False)
        win.select_home()
        self.assertEqual(win.current_key(), "home")

    def test_about_without_updater(self):
        win = self._make_window(self._config({"language": "ja"}))
        page = win.page("about")
        self.assertFalse(page.check_button.isEnabled())
        self.assertFalse(page.update_button.isEnabled())
        self.assertEqual(page.status.text(), "このビルドでは自動アップデートは利用できません")


if __name__ == "__main__":
    unittest.main()
