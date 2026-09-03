"""設定ウィンドウ（src/ui/settings_window.py）のライフサイクルテスト（#15）。

検証する性質:
1. ウィンドウは使い回されるため、開くたび（showEvent）に永続 config から再ロードし、
   前回の未保存編集・テーマプレビューを破棄する（= キャンセルが本当に破棄になる）。
2. 自動起動（レジストリ）は設定保存が成功したときだけ反映する。保存失敗時に
   「自動起動だけ変わって設定は元のまま」という不整合を起こさない。

Qt はオフスクリーンで動かし、keyring / 音声デバイス / レジストリ（autostart）は
実体に触れないようモックする（lessons 2026-06-12: テストで実 keyring を読まない）。
"""

import os

# PySide6 の import より前に必ずオフスクリーン指定する
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import tempfile
import unittest
from unittest import mock

import yaml
from PySide6.QtCore import Qt
from PySide6.QtGui import QShowEvent
from PySide6.QtWidgets import QApplication

from src.config.config_manager import ConfigManager

# QApplication はプロセスに 1 つ。各テストで使い回す
_app = QApplication.instance() or QApplication([])


class _SettingsWindowTestBase(unittest.TestCase):
    def _config(self, data):
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".yaml", delete=False, encoding="utf-8"
        )
        yaml.dump(data, handle, allow_unicode=True)
        handle.close()
        self._paths.append(handle.name)
        return ConfigManager(config_path=handle.name)

    def _make_window(self, cm, history=None, history_sync=None):
        # 重い/副作用のある依存をモックしてオフスクリーン構築する。
        # 構築時にアカウントページが login_coordinator.shared() を生成し、その __init__ が
        # secrets.get_auth_session()（実 keyring）を読むため、ここも必ずモックする。実機の
        # 未署名 python で署名アプリ作成の keychain 項目を読むと ACL でブロックする（lessons 2026-06-12）。
        with mock.patch(
            "src.ui.settings_window.secrets.is_keyring_available", return_value=False
        ), mock.patch(
            "src.ui.settings_window.AudioRecorder.list_input_devices", return_value=[]
        ), mock.patch(
            "src.ui.settings_window.autostart.is_supported", return_value=True
        ), mock.patch(
            "src.ui.settings_window.autostart.is_enabled", return_value=False
        ), mock.patch(
            "src.utils.secrets.get_auth_session", return_value=None
        ):
            from src.ui.settings_window import SettingsWindow

            win = SettingsWindow(config_manager=cm, history=history, history_sync=history_sync)
        self._windows.append(win)
        return win

    def setUp(self):
        self._paths = []
        self._windows = []
        # 共有シングルトンを毎テスト初期化し、_make_window のモック下で新規生成させる
        # （別テストが残した状態・実 keyring 参照を持ち込まない）。
        from src.core import login_coordinator
        login_coordinator._shared = None

    def tearDown(self):
        for win in self._windows:
            win.deleteLater()
        for path in self._paths:
            if os.path.exists(path):
                os.unlink(path)


class TestReopenRevertsUnsavedChanges(_SettingsWindowTestBase):
    """#15: 開き直すと未保存の編集・テーマプレビューが破棄される。"""

    def test_showevent_reverts_value_and_theme(self):
        cm = self._config({"language": "ja", "dark_mode": False, "handsfree_key": "<f9>"})
        win = self._make_window(cm)

        # ユーザーが未保存で値とテーマを変更
        win._handsfree_input.setText("ZZZ")
        win._is_dark_mode = True
        win._apply_theme(True)
        win._theme_toggle.set_dark(True)

        # 開き直す（showEvent）。履歴/実績の更新は本テストの対象外なので無効化
        with mock.patch.object(win, "_refresh_history"), \
             mock.patch.object(win, "_refresh_stats"), \
             mock.patch("src.ui.settings_window.secrets.is_keyring_available", return_value=False), \
             mock.patch("src.ui.settings_window.AudioRecorder.list_input_devices", return_value=[]), \
             mock.patch("src.ui.settings_window.autostart.is_enabled", return_value=False):
            win.showEvent(QShowEvent())

        # 永続 config の値・テーマに戻っている
        self.assertEqual(win._handsfree_input.text(), "<f9>")
        self.assertFalse(win._is_dark_mode)

    def test_showevent_reloads_from_persisted_config(self):
        cm = self._config({"language": "ja", "dark_mode": False, "handsfree_key": ""})
        win = self._make_window(cm)
        win._handsfree_input.setText("未保存編集")

        with mock.patch.object(win, "_refresh_history"), \
             mock.patch.object(win, "_refresh_stats"), \
             mock.patch("src.ui.settings_window.secrets.is_keyring_available", return_value=False), \
             mock.patch("src.ui.settings_window.AudioRecorder.list_input_devices", return_value=[]), \
             mock.patch("src.ui.settings_window.autostart.is_enabled", return_value=False):
            win.showEvent(QShowEvent())

        self.assertEqual(win._handsfree_input.text(), "")


class TestAutostartTransaction(_SettingsWindowTestBase):
    """#15: 自動起動の反映は設定保存の成否と同じ境界で行う。"""

    def test_save_failure_does_not_touch_autostart(self):
        cm = self._config({"language": "ja"})
        win = self._make_window(cm)
        win._autostart_check.setChecked(True)

        with mock.patch.object(cm, "save", return_value=False), \
             mock.patch("src.ui.settings_window.autostart.is_supported", return_value=True), \
             mock.patch("src.ui.settings_window.autostart.set_enabled") as set_enabled, \
             mock.patch("src.ui.settings_window.QMessageBox"):
            win._save_settings()

        set_enabled.assert_not_called()  # 保存失敗時は自動起動に触れない

    def test_save_success_applies_autostart(self):
        cm = self._config({"language": "ja"})
        win = self._make_window(cm)
        win._autostart_check.setChecked(True)

        with mock.patch.object(cm, "save", return_value=True), \
             mock.patch("src.ui.settings_window.autostart.is_supported", return_value=True), \
             mock.patch("src.ui.settings_window.autostart.set_enabled", return_value=True) as set_enabled, \
             mock.patch.object(win, "close"), \
             mock.patch("src.ui.settings_window.QMessageBox"):
            win._save_settings()

        set_enabled.assert_called_once_with(True)  # 保存成功時のみ反映


class TestUnassignedHotkeyPersists(_SettingsWindowTestBase):
    """ホットキーを「未割り当て」（空）にして保存すると空のまま永続し、既定へ戻らない。

    Mac 版 SlotConfigTests.testUnassignedSlotPersistsAndDoesNotRevertToDefault と対応。
    片方を未割り当てにしても、もう片方の割り当ては保持される。
    """

    def test_clearing_slot_hotkey_persists_as_empty(self):
        cm = self._config({
            "language": "ja",
            "hotkey1": {"hotkey": "<f2>", "hotkey_mode": "hold", "backend": "groq"},
            "hotkey2": {"hotkey": "<f3>", "hotkey_mode": "toggle", "backend": "groq"},
        })
        win = self._make_window(cm)

        # スロット 1 を未割り当てにする（「割り当てを外す」ボタン＝input.clear と同じ）
        win._hotkey1_input.clear()

        with mock.patch("src.ui.settings_window.autostart.is_supported", return_value=False), \
             mock.patch.object(win, "close"), \
             mock.patch("src.ui.settings_window.QMessageBox"):
            win._save_settings()

        # 保存ファイルを新しいマネージャで読み直しても、スロット 1 は空のまま（既定 <f2> に戻らない）
        reloaded = ConfigManager(config_path=cm.config_path)
        self.assertEqual(reloaded.get("hotkey1")["hotkey"], "")
        # もう片方（スロット 2）の割り当ては保持される
        self.assertEqual(reloaded.get("hotkey2")["hotkey"], "<f3>")


class _FakeHistorySync:
    """history_sync.HistorySync の最小フェイク（Mac 側の履歴を 1 件足して返すだけ）。"""

    def __init__(self, extra_items):
        self._extra_items = extra_items
        self.enabled = True

    def merged_items(self, local_items):
        return list(local_items) + self._extra_items

    def status(self):
        return {
            "enabled": True,
            "configured": True,
            "pending": 0,
            "last_sync": None,
            "token_invalid": False,
            "last_error": None,
        }

    def request_fetch(self):
        pass

    def apply_config(self):
        pass


class TestHistorySyncSettingsUI(_SettingsWindowTestBase):
    """履歴タブの「Mac と履歴を共有」UI（history_sync 連携）。"""

    def test_builds_with_history_sync_none(self):
        cm = self._config({"language": "ja"})
        win = self._make_window(cm)

        # history_sync 未配置でもウィジェット自体は存在し、既定値で開ける
        self.assertFalse(win._history_sync_enabled_toggle.isChecked())
        self.assertEqual(win._history_sync_url_input.text(), "")
        self.assertEqual(win._history_sync_status_label.text(), "")

    def test_loads_existing_history_sync_config(self):
        cm = self._config({
            "language": "ja",
            "history_sync": {"enabled": True, "url": "https://example.workers.dev/"},
        })
        win = self._make_window(cm)

        self.assertTrue(win._history_sync_enabled_toggle.isChecked())
        self.assertEqual(win._history_sync_url_input.text(), "https://example.workers.dev/")

    def test_save_writes_history_sync_config_without_trailing_slash(self):
        cm = self._config({"language": "ja"})
        win = self._make_window(cm)

        win._history_sync_enabled_toggle.setChecked(True)
        win._history_sync_url_input.setText("https://example.workers.dev/")

        with mock.patch("src.ui.settings_window.autostart.is_supported", return_value=False), \
             mock.patch.object(win, "close"), \
             mock.patch("src.ui.settings_window.QMessageBox"):
            win._save_settings()

        self.assertTrue(cm.config["history_sync"]["enabled"])
        self.assertEqual(cm.config["history_sync"]["url"], "https://example.workers.dev")

    def test_refresh_history_shows_mac_entries_from_sync(self):
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        handle.close()
        self._paths.append(handle.name)

        from src.core.history import HistoryStore

        history = HistoryStore(file_path=handle.name)
        history.add("Windows の文")

        fake_sync = _FakeHistorySync([
            {"id": "m1", "text": "Mac の文", "date": "2026-09-02T10:00:00Z", "device": "mac"},
        ])

        cm = self._config({"language": "ja"})
        win = self._make_window(cm, history=history, history_sync=fake_sync)

        texts = [
            win._history_list.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(win._history_list.count())
        ]
        previews = [win._history_list.item(i).text() for i in range(win._history_list.count())]

        # Mac 由来のエントリはプレビューが "[Mac] " から始まるが、コピーされる全文（UserRole）には
        # ラベルが付かない
        mac_previews = [p for p in previews if p.startswith("[Mac] ")]
        self.assertEqual(len(mac_previews), 1)
        self.assertIn("Mac の文", mac_previews[0])
        self.assertIn("Mac の文", texts)


if __name__ == "__main__":
    unittest.main()
