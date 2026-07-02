"""初回起動オンボーディングウィンドウ（src/ui/onboarding_window.py）のスモークテスト（Phase 5）。

Qt はオフスクリーン、ログイン（login_coordinator.shared）はモックして keyring/ネットワークに
触れない。検証内容:
- 3 ステップの遷移と主ボタン文言（セットアップを始める → あとで/次へ → 使い始める）
- 完了ページが settings.yaml の既定ホットキーを新語彙で表示する
- 完了/クローズで onboarding_finished が 1 度だけ発火する
- ログインボタンで begin_login とブラウザ起動が呼ばれる
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import tempfile
import time
import unittest
from unittest import mock

import yaml
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QApplication

from src.config.config_manager import ConfigManager

_app = QApplication.instance() or QApplication([])


class _FakeCoord:
    """login_coordinator.shared() の差し替え用（keyring に触れない）。"""

    def __init__(self, status="idle"):
        self.status = status
        self.account_email = None
        self.error = None
        self.begin_login_called = False

    def begin_login(self):
        self.begin_login_called = True
        return "https://example.com/login"


class TestOnboardingWindow(unittest.TestCase):
    def setUp(self):
        # 既定値そのままの ConfigManager（一時ファイル）
        handle = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8")
        yaml.dump({}, handle, allow_unicode=True)
        handle.close()
        self._path = handle.name
        self._config = ConfigManager(config_path=self._path)
        self._windows = []

    def tearDown(self):
        for win in self._windows:
            win.deleteLater()
        _app.processEvents()
        try:
            os.unlink(self._path)
        except OSError:
            pass

    def _make_window(self):
        from src.ui.onboarding_window import OnboardingWindow

        win = OnboardingWindow(self._config)
        self._windows.append(win)
        return win

    def test_navigation_and_primary_labels(self):
        with mock.patch("src.core.login_coordinator.shared", return_value=_FakeCoord()):
            win = self._make_window()
            # ステップ 0: ようこそ
            self.assertEqual(win._step, 0)
            self.assertEqual(win._primary_btn.text(), "セットアップを始める")
            self.assertFalse(win._back_btn.isVisible())

            # → ステップ 1: ログイン（未ログインなので「あとで」）
            win._primary_btn.click()
            _app.processEvents()
            self.assertEqual(win._step, 1)
            self.assertEqual(win._primary_btn.text(), "あとで")

            # → ステップ 2: 完了
            win._primary_btn.click()
            _app.processEvents()
            self.assertEqual(win._step, 2)
            self.assertEqual(win._primary_btn.text(), "使い始める")

            # 戻る → ステップ 1
            win._back_btn.click()
            _app.processEvents()
            self.assertEqual(win._step, 1)

    def test_logged_in_shows_next(self):
        # ログイン済みなら主ボタンは「次へ」になる
        with mock.patch(
            "src.core.login_coordinator.shared",
            return_value=_FakeCoord(status="logged_in"),
        ):
            win = self._make_window()
            win._primary_btn.click()  # → ログインステップ
            _app.processEvents()
            self.assertEqual(win._step, 1)
            self.assertEqual(win._primary_btn.text(), "次へ")

    def test_done_page_shows_default_hotkeys(self):
        # 完了ページのカードが既定ホットキー（<f2>/<f3>）と新語彙（スタンダード）を出す
        with mock.patch("src.core.login_coordinator.shared", return_value=_FakeCoord()):
            win = self._make_window()
            win._step = 2
            win._go_to(2)
            _app.processEvents()
            from PySide6.QtWidgets import QLabel

            texts = [w.text() for w in win.findChildren(QLabel)]
            joined = "\n".join(texts)
            self.assertIn("<f2>", joined)
            self.assertIn("<f3>", joined)
            self.assertIn("スタンダード", joined)  # 既定バックエンド groq の新語彙
            self.assertIn("録音キー 1（メイン）", joined)

    def test_finish_emits_once(self):
        with mock.patch("src.core.login_coordinator.shared", return_value=_FakeCoord()):
            win = self._make_window()
            fired = []
            win.onboarding_finished.connect(lambda: fired.append(True))
            win._step = 2
            win._go_to(2)
            win._primary_btn.click()  # 使い始める
            _app.processEvents()
            self.assertEqual(fired, [True])
            # 追加のクローズで二重発火しない
            win.closeEvent(QCloseEvent())
            _app.processEvents()
            self.assertEqual(fired, [True])

    def test_close_marks_finished(self):
        # × で閉じてもスキップ扱いで完了フラグ（シグナル）を立てる
        with mock.patch("src.core.login_coordinator.shared", return_value=_FakeCoord()):
            win = self._make_window()
            fired = []
            win.onboarding_finished.connect(lambda: fired.append(True))
            win.closeEvent(QCloseEvent())
            _app.processEvents()
            self.assertEqual(fired, [True])

    def test_login_click_opens_browser(self):
        fake = _FakeCoord()
        with mock.patch("src.core.login_coordinator.shared", return_value=fake), \
             mock.patch("src.ui.onboarding_window.QDesktopServices.openUrl") as open_url:
            win = self._make_window()
            win._primary_btn.click()  # → ログインステップ
            _app.processEvents()
            win._login_btn.click()
            _app.processEvents()
            self.assertTrue(fake.begin_login_called)
            open_url.assert_called_once()
            self.assertTrue(win._login_poll.isActive())
            win._login_poll.stop()


if __name__ == "__main__":
    unittest.main()
