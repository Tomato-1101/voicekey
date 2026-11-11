"""
翻訳して入力のページと、一般ページの再貼り付けキー行のテスト

値の読み込み・保存、キーの重なりの警告、警告の有無で行の位置と高さが変わらないことを確かめる。
通信はしない（翻訳そのものは呼ばない）。
"""

import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QWidget

from src.config.config_manager import ConfigManager
from src.ui.components import MessageLabel, SettingRow
from src.ui.pages.context import PageContext
from src.ui.pages.translate import TranslatePage

_app = QApplication.instance() or QApplication([])


class TranslatePageTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.config = ConfigManager(config_path=os.path.join(temp.name, "s.yaml"))
        self.ctx = PageContext(self.config)
        self.page = TranslatePage(self.ctx)
        self.page.resize(720, 600)
        self.page.show()
        _app.processEvents()

    def tearDown(self):
        self.page.close()

    def _layout(self):
        _app.processEvents()
        root = self.page.scroll.widget()
        return [
            (type(w).__name__, w.mapTo(root, w.rect().topLeft()).y(), w.height())
            for w in self.page.findChildren(QWidget)
            if w.isVisibleTo(self.page) and isinstance(w, (SettingRow, MessageLabel))
        ]

    def test_defaults(self):
        """既定は右 Shift・常時翻訳オフ・英語。"""
        self.assertEqual(self.page.once_input.value(), "<shift_r>")
        self.assertTrue(self.page.once_clear.isVisible())
        self.assertFalse(self.page.always_switch.isChecked())
        self.assertEqual(self.page.target_picker.currentData(), "en")
        self.assertEqual(self.page.once_warning.text(), "")

    def test_saves_changes(self):
        """切り替えるとその場で設定に書く。"""
        self.page.always_switch.setChecked(True)
        self.page.target_picker.setCurrentIndex(self.page.target_picker.findData("ko"))
        self.ctx.flush()
        self.assertTrue(self.config.get("translate_input_enabled"))
        self.assertEqual(self.config.get("translate_input_target"), "ko")

    def test_warning_does_not_move_layout(self):
        """録音キーと重なると警告が出る。警告の有無・クリアの出し入れで位置と高さが変わらない。"""
        before = self._layout()
        self.page.once_input.set_value("<f2>")
        self.page.once_input.valueChanged.emit("<f2>")
        self.assertIn("録音キー 1 と重なっています", self.page.once_warning.text())
        self.assertEqual(self._layout(), before)
        self.page.once_input.clear_value()
        _app.processEvents()
        self.assertEqual(self.config.get("translate_once_key"), "")
        self.assertFalse(self.page.once_clear.isVisible())
        self.assertEqual(self.page.once_warning.text(), "")
        self.assertEqual(self._layout(), before)


class RepasteRowTests(unittest.TestCase):
    def test_repaste_key_row(self):
        """一般ページに再貼り付けキーがあり、既定値を読み、外すと空で保存する。"""
        from src.ui.pages.general import GeneralPage

        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        config = ConfigManager(config_path=os.path.join(temp.name, "s.yaml"))
        with mock.patch("src.utils.secrets.get_api_key", return_value=None), \
                mock.patch("src.core.audio_recorder.AudioRecorder.list_input_devices", return_value=[]):
            page = GeneralPage(PageContext(config))
        self.addCleanup(page.close)
        self.assertEqual(page.repaste_input.value(), "<ctrl>+<alt>+<v>")
        page.repaste_input.clear_value()
        page.ctx.flush()
        self.assertEqual(config.get("repaste_key"), "")
        self.assertFalse(page.repaste_clear.isEnabled())


if __name__ == "__main__":
    unittest.main()
