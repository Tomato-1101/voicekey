"""
議事録・会議ページのテスト

ボットの知らせ（状態・発言・要約）を受けて表示が変わること、状態が変わっても欄の位置と高さが
変わらないことを確かめる。Chrome は起動しない・通信もしない。
"""

import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QWidget

from src.config.config_manager import ConfigManager
from src.meetbot import rules, transcript
from src.meetbot.service import MeetBotService
from src.meetbot.transcript import TranscriptRecorder
from src.ui.components import MessageLabel, SettingRow
from src.ui.pages.context import PageContext
from src.ui.pages.meeting import MeetingPage

_app = QApplication.instance() or QApplication([])


class MeetingPageTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name) / "transcripts"
        recorder = TranscriptRecorder(self.directory)
        start = datetime(2026, 11, 11, 14, 5, 7)
        recorder.begin("abc-defg-hij", "Google Meet", "Meet の字幕（日本語）", start)
        recorder.append("よろしくお願いします", "山田", start + timedelta(seconds=3))
        self.path = recorder.path
        recorder.end()

        for patch in (
            mock.patch("src.meetbot.transcript.default_directory", return_value=self.directory),
            mock.patch("src.meetbot.chrome.find_chrome", return_value="/fake/chrome"),
        ):
            patch.start()
            self.addCleanup(patch.stop)

        config = ConfigManager(config_path=os.path.join(temp.name, "s.yaml"))
        self.bot = MeetBotService(config=config, directory=self.directory)
        self.ctx = PageContext(config, meetbot=self.bot)
        self.page = MeetingPage(self.ctx)
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

    def test_idle_shows_past_transcripts(self):
        """開いた時点で過去の議事録が一覧に並び、参加ボタンは URL が入るまで押せない。"""
        self.assertEqual(self.page.transcript_list.count(), 1)
        self.assertEqual(self.page.join_button.text(), "参加")
        self.page.url_input.setText("")
        self.assertFalse(self.page.join_button.isEnabled())
        self.page.url_input.setText("https://meet.google.com/abc-defg-hij")
        self.assertTrue(self.page.join_button.isEnabled())

    def test_states_do_not_move_layout(self):
        """準備中・承認待ち・記録中・失敗・ログイン済みのどれでも、行と欄の位置・高さが変わらない。"""
        before = self._layout()
        for state, text in (
            (rules.STATE_LAUNCHING, rules.state_text(rules.STATE_LAUNCHING)),
            (rules.STATE_WAITING_APPROVAL, rules.state_text(rules.STATE_WAITING_APPROVAL)),
            (rules.STATE_RECORDING, rules.state_text(rules.STATE_RECORDING, "定例")),
            (rules.STATE_FAILED, rules.state_text(rules.STATE_FAILED, "とても長い理由" * 30)),
        ):
            self.bot.state_changed.emit(state, text)
            self.assertEqual(self._layout(), before, state)
        self.bot.login_state_changed.emit(rules.LOGIN_DONE)
        self.page.notice.show_message("長い知らせ" * 40, "warn")
        self.assertEqual(self._layout(), before)

    def test_recording_lines_and_leave_button(self):
        """参加中は「会議から退出」になり、確定した発言が欄に流れる（文字は HTML として解釈しない）。"""
        self.bot.state_changed.emit(rules.STATE_LAUNCHING, rules.state_text(rules.STATE_LAUNCHING))
        self.assertEqual(self.page.join_button.text(), "会議から退出")
        self.assertFalse(self.page.url_input.isEnabled())
        self.assertFalse(self.page.language_picker.isEnabled())
        self.bot.state_changed.emit(rules.STATE_RECORDING, rules.state_text(rules.STATE_RECORDING))
        self.bot.line_confirmed.emit("山田", "14:05:10", "<b>太字にしない</b>")
        text = self.page.live_view.toPlainText()
        self.assertIn("<b>太字にしない</b>", text)
        self.assertIn("山田", text)
        self.assertFalse(self.page.live_view.empty_label.isVisible())

    def test_body_and_summary(self):
        """議事録を選ぶと本文が出て、要約ができると要約に切り替わり「要約あり」になる。"""
        self.page.transcript_list.setCurrentRow(0)
        self.assertIn("よろしくお願いします", self.page.body_view.toPlainText())
        self.assertEqual(self.page.summarize_button.text(), "要約")

        transcript.write_summary(self.path, "## 決まったこと\n- 見積もりを出す")
        self.bot.summary_finished.emit(str(self.path), "", "要約を保存しました")
        self.assertEqual(self.page.mode_switch.current_index(), 1)
        self.assertIn("見積もりを出す", self.page.body_view.toPlainText())
        self.assertEqual(self.page.summarize_button.text(), "作り直す")
        self.assertTrue(self.page._selected().has_summary)

    def test_language_is_saved(self):
        """会議の言語を変えると設定に書く。"""
        index = self.page.language_picker.findData("en")
        self.page.language_picker.setCurrentIndex(index)
        self.page.language_picker.activated.emit(index)
        self.ctx.flush()
        self.assertEqual(self.ctx.get("meetbot_caption_language"), "en")

    def test_without_service(self):
        """ボットが無いビルドでも描け、参加・ログインは押せない。"""
        page = MeetingPage(PageContext(self.ctx.config_manager))
        self.assertFalse(page.join_button.isEnabled())
        self.assertFalse(page.open_login_button.isEnabled())
        page.close()


if __name__ == "__main__":
    unittest.main()
