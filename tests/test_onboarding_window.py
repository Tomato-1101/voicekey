"""初回起動オンボーディングウィンドウ（src/ui/onboarding_window.py）のスモークテスト（Phase 5）。

Qt はオフスクリーン、ログイン（login_coordinator.shared）はモックして keyring/ネットワークに
触れない。検証内容:
- 7 ステップの遷移と主ボタン文言（セットアップを始める → あとで/次へ → 次へ×4 → 使い始める）
- 体験ステップの成功判定（練習欄にテキストが入ると成功メッセージが出る）
- 整形体験ステップに入っている間だけ整形オーバーライドが ON になる
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
        from src.ui.onboarding_window import (
            _STEP_DONE,
            _STEP_FEATURE_TOUR,
            _STEP_LOGIN,
            _STEP_PRACTICE_BASIC,
            _STEP_WELCOME,
        )

        with mock.patch("src.core.login_coordinator.shared", return_value=_FakeCoord()):
            win = self._make_window()
            win.show()  # isVisible() はトップレベルが表示されていないと常に False を返すため
            _app.processEvents()
            # ステップ 0: ようこそ
            self.assertEqual(win._step, _STEP_WELCOME)
            self.assertEqual(win._primary_btn.text(), "セットアップを始める")
            self.assertFalse(win._back_btn.isVisible())
            self.assertFalse(win._skip_btn.isVisible())

            # → ステップ 1: ログイン（未ログインなので「あとで」・スキップは出ない）
            win._primary_btn.click()
            _app.processEvents()
            self.assertEqual(win._step, _STEP_LOGIN)
            self.assertEqual(win._primary_btn.text(), "あとで")
            self.assertFalse(win._skip_btn.isVisible())

            # → ステップ 2: 体験1（「次へ」・スキップ常設）
            win._primary_btn.click()
            _app.processEvents()
            self.assertEqual(win._step, _STEP_PRACTICE_BASIC)
            self.assertEqual(win._primary_btn.text(), "次へ")
            self.assertTrue(win._skip_btn.isVisible())

            # 体験〜機能ツアーを「次へ」で通過して完了へ
            for _ in range(_STEP_DONE - _STEP_PRACTICE_BASIC):
                win._primary_btn.click()
                _app.processEvents()
            self.assertEqual(win._step, _STEP_DONE)
            self.assertEqual(win._primary_btn.text(), "使い始める")
            self.assertFalse(win._skip_btn.isVisible())

            # 戻る → 機能ツアー
            win._back_btn.click()
            _app.processEvents()
            self.assertEqual(win._step, _STEP_FEATURE_TOUR)

    def test_skip_jumps_to_done(self):
        # 体験ステップからスキップすると完了ステップへ飛ぶ（体験を強制しない）
        from src.ui.onboarding_window import _STEP_DONE, _STEP_PRACTICE_BASIC

        with mock.patch("src.core.login_coordinator.shared", return_value=_FakeCoord()):
            win = self._make_window()
            win._step = _STEP_PRACTICE_BASIC
            win._go_to(_STEP_PRACTICE_BASIC)
            _app.processEvents()
            win._skip_btn.click()
            _app.processEvents()
            self.assertEqual(win._step, _STEP_DONE)
            self.assertEqual(win._primary_btn.text(), "使い始める")

    def test_practice_input_shows_success(self):
        # 練習欄にテキストが入ると成功メッセージが表示される
        from src.ui.onboarding_window import _STEP_PRACTICE_BASIC

        with mock.patch("src.core.login_coordinator.shared", return_value=_FakeCoord()):
            win = self._make_window()
            win.show()  # isVisible() はトップレベルが表示されていないと常に False を返すため
            win._step = _STEP_PRACTICE_BASIC
            win._go_to(_STEP_PRACTICE_BASIC)
            _app.processEvents()
            parts = win._practice_pages[_STEP_PRACTICE_BASIC]
            self.assertFalse(parts["success"].isVisible())
            parts["edit"].setPlainText("明日の打ち合わせ、15時に変更でお願いします。")
            _app.processEvents()
            self.assertTrue(parts["success"].isVisible())

    def test_format_override_only_during_format_step(self):
        # 整形体験ステップに入っている間だけ app へ整形オーバーライド ON が伝わる
        from src.ui.onboarding_window import (
            _STEP_FEATURE_TOUR,
            _STEP_PRACTICE_BASIC,
            _STEP_PRACTICE_FORMAT,
        )

        fake_app = mock.Mock()
        with mock.patch("src.core.login_coordinator.shared", return_value=_FakeCoord()):
            from src.ui.onboarding_window import OnboardingWindow

            win = OnboardingWindow(self._config, app=fake_app)
            self._windows.append(win)
            win._go_to(_STEP_PRACTICE_BASIC)
            _app.processEvents()
            self.assertEqual(fake_app.set_practice_format_override.call_args[0][0], False)
            win._go_to(_STEP_PRACTICE_FORMAT)
            _app.processEvents()
            self.assertEqual(fake_app.set_practice_format_override.call_args[0][0], True)
            win._go_to(_STEP_FEATURE_TOUR)
            _app.processEvents()
            self.assertEqual(fake_app.set_practice_format_override.call_args[0][0], False)

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
        from src.ui.onboarding_window import _STEP_DONE

        with mock.patch("src.core.login_coordinator.shared", return_value=_FakeCoord()):
            win = self._make_window()
            win._step = _STEP_DONE
            win._go_to(_STEP_DONE)
            _app.processEvents()
            from PySide6.QtWidgets import QLabel

            texts = [w.text() for w in win.findChildren(QLabel)]
            joined = "\n".join(texts)
            self.assertIn("<f2>", joined)
            self.assertIn("<f3>", joined)
            self.assertIn("スタンダード", joined)  # 既定バックエンド groq の新語彙
            self.assertIn("録音キー 1（メイン）", joined)

    def test_finish_emits_once(self):
        from src.ui.onboarding_window import _STEP_DONE

        with mock.patch("src.core.login_coordinator.shared", return_value=_FakeCoord()):
            win = self._make_window()
            fired = []
            win.onboarding_finished.connect(lambda: fired.append(True))
            win._step = _STEP_DONE
            win._go_to(_STEP_DONE)
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


class TestPracticeInputDetection(unittest.TestCase):
    """体験ステップの成功判定（純ロジック）。Mac 版 OnboardingPractice.hasInput と同観点。"""

    def test_has_practice_input(self):
        from src.ui.onboarding_window import _has_practice_input

        self.assertFalse(_has_practice_input(""))
        self.assertFalse(_has_practice_input("   "))
        self.assertFalse(_has_practice_input("\n \t"))
        self.assertTrue(_has_practice_input("あ"))
        self.assertTrue(_has_practice_input("  明日の打ち合わせ  "))


if __name__ == "__main__":
    unittest.main()
