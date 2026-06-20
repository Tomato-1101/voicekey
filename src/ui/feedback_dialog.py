"""
フィードバック入力ダイアログ

メニューの「フィードバックを送る…」から開く小さな入力フォーム。
本文を自社サーバー（/api/v1/feedback）へ送信する。ログイン済みならアカウントに
紐づき、未ログインでも匿名（device_id + app_version）で送れる。
送信は通信ブロッキングのためワーカースレッドで行い、成功/失敗を画面に明示する。
"""

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

from ..core import backend_client
from ..utils.logger import get_logger

logger = get_logger(__name__)


class _SubmitWorker(QThread):
    """フィードバック送信をバックグラウンドで実行するワーカー。

    httpx の同期 POST は UI スレッドをブロックするため別スレッドで実行し、
    結果（成功可否・エラーメッセージ）をシグナルで返す。
    """

    # (成功したか, エラーメッセージ) を返す
    result_ready = Signal(bool, str)

    def __init__(self, message: str, parent=None) -> None:
        super().__init__(parent)
        self._message = message

    def run(self) -> None:
        try:
            backend_client.submit_feedback(self._message)
            self.result_ready.emit(True, "")
        except backend_client.BackendError as e:
            self.result_ready.emit(False, str(e))
        except Exception as e:  # 予期しない失敗も握ってユーザーに表示する
            logger.warning(f"フィードバック送信で予期しないエラー: {e}")
            self.result_ready.emit(False, f"送信に失敗しました: {e}")


class FeedbackDialog(QDialog):
    """フィードバック本文の入力・送信を行うモーダルダイアログ。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("フィードバックを送る")
        self.setMinimumWidth(420)
        self._worker: _SubmitWorker | None = None

        layout = QVBoxLayout(self)

        description = QLabel(
            "ご意見・不具合・要望をお寄せください。開発の参考にします。"
        )
        description.setWordWrap(True)
        layout.addWidget(description)

        # ログイン状態に応じた紐付けの説明（アカウント連携の有無を明示）
        if backend_client.is_logged_in():
            hint_text = "お使いのアカウントに紐づけて送信されます。"
        else:
            hint_text = "未ログインのため匿名で送信されます。"
        hint = QLabel(hint_text)
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #8E8E93; font-size: 11px;")
        layout.addWidget(hint)

        self._editor = QPlainTextEdit()
        self._editor.setPlaceholderText("ここにフィードバックを入力…")
        self._editor.setMinimumHeight(140)
        self._editor.textChanged.connect(self._update_send_enabled)
        layout.addWidget(self._editor)

        # 送信状態・結果の表示行（初期は空）
        self._status = QLabel("")
        self._status.setWordWrap(True)
        layout.addWidget(self._status)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self._cancel_button = QPushButton("キャンセル")
        self._cancel_button.clicked.connect(self.reject)
        buttons.addWidget(self._cancel_button)
        self._send_button = QPushButton("送信")
        self._send_button.setDefault(True)
        self._send_button.setEnabled(False)
        self._send_button.clicked.connect(self._on_send)
        buttons.addWidget(self._send_button)
        layout.addLayout(buttons)

    def _update_send_enabled(self) -> None:
        """本文が空のときは送信ボタンを無効化する。"""
        self._send_button.setEnabled(bool(self._editor.toPlainText().strip()))

    def _on_send(self) -> None:
        """送信を開始する（入力をロックしてワーカーを起動）。"""
        message = self._editor.toPlainText().strip()
        if not message:
            return
        self._send_button.setEnabled(False)
        self._editor.setReadOnly(True)
        self._status.setStyleSheet("color: #8E8E93;")
        self._status.setText("送信中…")

        self._worker = _SubmitWorker(message, self)
        self._worker.result_ready.connect(self._on_result)
        self._worker.start()

    def _on_result(self, ok: bool, error: str) -> None:
        """送信結果を反映する。成功なら完了表示、失敗なら再入力を許可する。"""
        if ok:
            # 成功通知（誤送信防止より「送れた確証」を優先＝lessons の送信系UI 方針）
            self._status.setStyleSheet("color: #34C759;")
            self._status.setText("送信しました。ありがとうございます！")
            self._send_button.setVisible(False)
            self._cancel_button.setText("閉じる")
        else:
            self._status.setStyleSheet("color: #FF453A;")
            self._status.setText(error or "送信に失敗しました。")
            self._editor.setReadOnly(False)
            self._send_button.setEnabled(True)
