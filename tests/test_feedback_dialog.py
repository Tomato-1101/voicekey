"""フィードバックダイアログ（src/ui/feedback_dialog.py）の送信ライフサイクルテスト（#16）。

送信ワーカー（QThread）がダイアログ破棄に巻き込まれてクラッシュしないことを検証する:
- 成功 / 失敗の結果反映
- 送信中はキャンセル無効・reject 無視（ユーザーが閉じても実行中スレッドを壊さない）
- アプリ終了など強制 close 時は closeEvent でワーカー完了を待ってから破棄する

Qt はオフスクリーン、ネットワーク（backend_client.submit_feedback / is_logged_in）はモック。
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import threading
import time
import unittest
from unittest import mock

from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QApplication

from src.core import backend_client

_app = QApplication.instance() or QApplication([])


def _pump_until(cond, timeout=5.0):
    """条件が満たされるまで Qt イベントを回す（キュー済みシグナルを処理）。"""
    deadline = time.monotonic() + timeout
    while not cond() and time.monotonic() < deadline:
        _app.processEvents()
        time.sleep(0.005)
    _app.processEvents()
    return cond()


class TestFeedbackDialogLifecycle(unittest.TestCase):
    def setUp(self):
        self._dialogs = []

    def tearDown(self):
        for dlg in self._dialogs:
            dlg.deleteLater()
        _app.processEvents()

    def _make_dialog(self):
        from src.ui.feedback_dialog import FeedbackDialog

        dlg = FeedbackDialog()
        self._dialogs.append(dlg)
        return dlg

    def test_success_shows_confirmation(self):
        with mock.patch.object(backend_client, "is_logged_in", return_value=False), \
             mock.patch.object(backend_client, "submit_feedback", return_value=None):
            dlg = self._make_dialog()
            dlg._editor.setPlainText("テスト本文")
            dlg._on_send()
            self.assertTrue(_pump_until(lambda: not dlg._sending))

        self.assertIn("送信しました", dlg._status.text())
        self.assertFalse(dlg._send_button.isVisible())
        self.assertEqual(dlg._cancel_button.text(), "閉じる")
        self.assertTrue(dlg._cancel_button.isEnabled())
        # ワーカーは finished→deleteLater/参照解除で片付く
        self.assertTrue(_pump_until(lambda: dlg._worker is None))

    def test_failure_allows_retry(self):
        with mock.patch.object(backend_client, "is_logged_in", return_value=False), \
             mock.patch.object(
                 backend_client, "submit_feedback",
                 side_effect=backend_client.BackendError("boom"),
             ):
            dlg = self._make_dialog()
            dlg._editor.setPlainText("x")
            dlg._on_send()
            self.assertTrue(_pump_until(lambda: not dlg._sending))

        self.assertIn("boom", dlg._status.text())
        self.assertTrue(dlg._send_button.isEnabled())
        self.assertFalse(dlg._editor.isReadOnly())
        self.assertTrue(dlg._cancel_button.isEnabled())

    def test_reject_blocked_while_sending(self):
        gate = threading.Event()

        def blocking_submit(_message):
            gate.wait(5)

        with mock.patch.object(backend_client, "is_logged_in", return_value=False), \
             mock.patch.object(backend_client, "submit_feedback", side_effect=blocking_submit):
            dlg = self._make_dialog()
            rejected_fired = []
            dlg.rejected.connect(lambda: rejected_fired.append(True))
            dlg._editor.setPlainText("x")
            dlg._on_send()
            self.assertTrue(
                _pump_until(lambda: dlg._worker is not None and dlg._worker.isRunning())
            )

            # 送信中: キャンセル無効、reject は無視される
            self.assertFalse(dlg._cancel_button.isEnabled())
            dlg.reject()
            _app.processEvents()
            self.assertEqual(rejected_fired, [])  # 送信中の reject は発火しない
            self.assertTrue(dlg._sending)

            gate.set()  # ワーカーを解放
            self.assertTrue(_pump_until(lambda: not dlg._sending))

            # 送信完了後は reject が通る
            dlg.reject()
            _app.processEvents()
            self.assertEqual(rejected_fired, [True])

    def test_close_during_send_waits_for_worker(self):
        gate = threading.Event()

        def blocking_submit(_message):
            gate.wait(5)

        with mock.patch.object(backend_client, "is_logged_in", return_value=False), \
             mock.patch.object(backend_client, "submit_feedback", side_effect=blocking_submit):
            dlg = self._make_dialog()
            dlg._editor.setPlainText("x")
            dlg._on_send()
            self.assertTrue(
                _pump_until(lambda: dlg._worker is not None and dlg._worker.isRunning())
            )
            worker = dlg._worker

            # 強制 close（アプリ終了相当）。closeEvent が wait() でワーカー完了を待つ
            threading.Timer(0.2, gate.set).start()
            dlg.closeEvent(QCloseEvent())

            # 破棄前にワーカーは終了している（= 実行中スレッドの巻き込み破棄が起きない）
            self.assertFalse(worker.isRunning())


if __name__ == "__main__":
    unittest.main()
