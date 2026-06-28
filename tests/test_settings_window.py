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

    def _make_window(self, cm):
        # 重い/副作用のある依存をモックしてオフスクリーン構築する
        with mock.patch(
            "src.ui.settings_window.secrets.is_keyring_available", return_value=False
        ), mock.patch(
            "src.ui.settings_window.AudioRecorder.list_input_devices", return_value=[]
        ), mock.patch(
            "src.ui.settings_window.autostart.is_supported", return_value=True
        ), mock.patch(
            "src.ui.settings_window.autostart.is_enabled", return_value=False
        ):
            from src.ui.settings_window import SettingsWindow

            win = SettingsWindow(config_manager=cm)
        self._windows.append(win)
        return win

    def setUp(self):
        self._paths = []
        self._windows = []

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


if __name__ == "__main__":
    unittest.main()
