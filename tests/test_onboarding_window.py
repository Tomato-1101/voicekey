"""初回起動オンボーディングウィンドウ（src/ui/onboarding_window.py）のスモークテスト。

Qt はオフスクリーン、API キーの読み出し（secrets.get_api_key）はモックして keyring/ネットワークに
触れない。検証内容（体験型 2 ペイン再デザイン・8 ステップ）:
- 8 ステップの遷移と主ボタン文言（はじめる → 次へ×6 → VoiceKey をはじめる）
- 体験ステップの成功判定（練習欄にテキストが入ると成功メッセージが出る）
- 整形体験ステップに入っている間だけ整形オーバーライドが ON になる
- 録音キーテスト／マイクテストの間だけ録音抑止モード（set_hotkey_test）が ON になる
- まとめページが settings.yaml の既定ホットキーを新語彙で表示する
- 完了/クローズで onboarding_finished が 1 度だけ発火する
- 体験ステップで、API キーが 1 つも無いときだけ注意を出す
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
            _STEP_API_KEY,
            _STEP_MIC_TEST,
            _STEP_PRACTICE_FORMAT,
            _STEP_SUMMARY,
            _STEP_WELCOME,
        )

        with mock.patch("src.utils.secrets.get_api_key", return_value=None):
            win = self._make_window()
            win.show()  # isVisible() はトップレベルが表示されていないと常に False を返すため
            _app.processEvents()
            # ステップ 0: ようこそ
            self.assertEqual(win._step, _STEP_WELCOME)
            self.assertEqual(win._primary_btn.text(), "はじめる")
            self.assertFalse(win._back_btn.isVisible())
            self.assertFalse(win._skip_btn.isVisible())

            # → ステップ 1: API キーの案内（「次へ」・スキップは出ない）
            win._primary_btn.click()
            _app.processEvents()
            self.assertEqual(win._step, _STEP_API_KEY)
            self.assertEqual(win._primary_btn.text(), "次へ")
            self.assertFalse(win._skip_btn.isVisible())

            # → ステップ 2: マイクテスト（「次へ」・スキップ常設）
            win._primary_btn.click()
            _app.processEvents()
            self.assertEqual(win._step, _STEP_MIC_TEST)
            self.assertEqual(win._primary_btn.text(), "次へ")
            self.assertTrue(win._skip_btn.isVisible())

            # 動作確認〜体験を「次へ」で通過してまとめへ
            for _ in range(_STEP_SUMMARY - _STEP_MIC_TEST):
                win._primary_btn.click()
                _app.processEvents()
            self.assertEqual(win._step, _STEP_SUMMARY)
            self.assertEqual(win._primary_btn.text(), "VoiceKey をはじめる")
            self.assertFalse(win._skip_btn.isVisible())

            # 戻る → 体験3（整形）
            win._back_btn.click()
            _app.processEvents()
            self.assertEqual(win._step, _STEP_PRACTICE_FORMAT)

    def test_skip_jumps_to_summary(self):
        # 体験ステップからスキップするとまとめへ飛ぶ（体験を強制しない）
        from src.ui.onboarding_window import _STEP_PRACTICE_BASIC, _STEP_SUMMARY

        with mock.patch("src.utils.secrets.get_api_key", return_value=None):
            win = self._make_window()
            win._step = _STEP_PRACTICE_BASIC
            win._go_to(_STEP_PRACTICE_BASIC)
            _app.processEvents()
            win._skip_btn.click()
            _app.processEvents()
            self.assertEqual(win._step, _STEP_SUMMARY)
            self.assertEqual(win._primary_btn.text(), "VoiceKey をはじめる")

    def test_practice_input_shows_success(self):
        # 練習欄にテキストが入ると成功メッセージが表示される
        from src.ui.onboarding_window import _STEP_PRACTICE_BASIC

        with mock.patch("src.utils.secrets.get_api_key", return_value=None):
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
            _STEP_PRACTICE_BASIC,
            _STEP_PRACTICE_FORMAT,
            _STEP_SUMMARY,
        )

        fake_app = mock.Mock()
        with mock.patch("src.utils.secrets.get_api_key", return_value=None):
            from src.ui.onboarding_window import OnboardingWindow

            win = OnboardingWindow(self._config, app=fake_app)
            self._windows.append(win)
            win._go_to(_STEP_PRACTICE_BASIC)
            _app.processEvents()
            self.assertEqual(fake_app.set_practice_format_override.call_args[0][0], False)
            win._go_to(_STEP_PRACTICE_FORMAT)
            _app.processEvents()
            self.assertEqual(fake_app.set_practice_format_override.call_args[0][0], True)
            win._go_to(_STEP_SUMMARY)
            _app.processEvents()
            self.assertEqual(fake_app.set_practice_format_override.call_args[0][0], False)

    def test_hotkey_test_active_only_on_test_steps(self):
        # 録音キーテスト／マイクテストの間だけ録音抑止モード（set_hotkey_test）が ON になる
        from src.ui.onboarding_window import (
            _STEP_HOTKEY_TEST,
            _STEP_MIC_TEST,
            _STEP_PRACTICE_BASIC,
        )

        fake_app = mock.Mock()
        with mock.patch("src.utils.secrets.get_api_key", return_value=None):
            from src.ui.onboarding_window import OnboardingWindow

            win = OnboardingWindow(self._config, app=fake_app)
            self._windows.append(win)
            # マイクテスト: ON（callback なし＝録音を止めるだけ）
            win._go_to(_STEP_MIC_TEST)
            _app.processEvents()
            self.assertTrue(win._app.set_hotkey_test.call_args[0][0])
            self.assertIsNone(win._app.set_hotkey_test.call_args[0][1])
            # 録音キーテスト: ON（callback あり＝押下スロットを報告）
            win._go_to(_STEP_HOTKEY_TEST)
            _app.processEvents()
            self.assertTrue(win._app.set_hotkey_test.call_args[0][0])
            self.assertIsNotNone(win._app.set_hotkey_test.call_args[0][1])
            # 体験ステップ: OFF
            win._go_to(_STEP_PRACTICE_BASIC)
            _app.processEvents()
            self.assertFalse(win._app.set_hotkey_test.call_args[0][0])

    def test_key_note_shown_only_without_any_api_key(self):
        # 体験ステップの注意は、保存済みのキーも環境変数も無いときだけ出る
        from src.ui.onboarding_window import _STEP_PRACTICE_BASIC

        env = {k: v for k, v in os.environ.items() if not k.endswith("_API_KEY")}
        with mock.patch.dict(os.environ, env, clear=True):
            with mock.patch("src.utils.secrets.get_api_key", return_value=None):
                win = self._make_window()
                win.show()
                win._go_to(_STEP_PRACTICE_BASIC)
                _app.processEvents()
                self.assertTrue(win._practice_pages[_STEP_PRACTICE_BASIC]["key_note"].isVisible())
            with mock.patch("src.utils.secrets.get_api_key", return_value="K"):
                win._go_to(_STEP_PRACTICE_BASIC)
                _app.processEvents()
                self.assertFalse(win._practice_pages[_STEP_PRACTICE_BASIC]["key_note"].isVisible())

    def test_summary_shows_default_hotkeys(self):
        # まとめページのカードが既定ホットキー（<f2>/<f3>）とエンジン名を出す
        from src.ui.onboarding_window import _STEP_SUMMARY

        with mock.patch("src.utils.secrets.get_api_key", return_value=None):
            win = self._make_window()
            win._step = _STEP_SUMMARY
            win._go_to(_STEP_SUMMARY)
            _app.processEvents()
            from PySide6.QtWidgets import QLabel

            texts = [w.text() for w in win.findChildren(QLabel)]
            joined = "\n".join(texts)
            self.assertIn("<f2>", joined)
            self.assertIn("<f3>", joined)
            self.assertIn("Soniox", joined)  # スロット 1 の既定エンジン
            self.assertIn("Groq", joined)    # スロット 2 の既定エンジン
            self.assertIn("録音キー 1（メイン）", joined)

    def test_finish_emits_once(self):
        from src.ui.onboarding_window import _STEP_SUMMARY

        with mock.patch("src.utils.secrets.get_api_key", return_value=None):
            win = self._make_window()
            fired = []
            win.onboarding_finished.connect(lambda: fired.append(True))
            win._step = _STEP_SUMMARY
            win._go_to(_STEP_SUMMARY)
            win._primary_btn.click()  # VoiceKey をはじめる
            _app.processEvents()
            self.assertEqual(fired, [True])
            # 追加のクローズで二重発火しない
            win.closeEvent(QCloseEvent())
            _app.processEvents()
            self.assertEqual(fired, [True])

    def test_close_marks_finished(self):
        # × で閉じてもスキップ扱いで完了フラグ（シグナル）を立てる
        with mock.patch("src.utils.secrets.get_api_key", return_value=None):
            win = self._make_window()
            fired = []
            win.onboarding_finished.connect(lambda: fired.append(True))
            win.closeEvent(QCloseEvent())
            _app.processEvents()
            self.assertEqual(fired, [True])


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
