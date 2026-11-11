"""
二重起動防止モジュール

voicekey は常駐してグローバルホットキーを監視するため、2 つ同時に動くと 1 回の
キー操作で二重に録音・貼り付けされてしまう。最初の起動（primary）がローカル
ソケットで待ち受け、2 つ目以降の起動はそこへ接続できたら自分は終了する。

設計上の安全策: 判定が何らかの理由で失敗しても、アプリ起動を妨げない
（primary とみなして通常起動する）。
"""

from typing import Optional

from PySide6.QtCore import QObject
from PySide6.QtNetwork import QLocalServer, QLocalSocket

from .logger import get_logger

logger = get_logger(__name__)

# 単一インスタンス用のローカルソケット名（ユーザー内で一意なら十分）
_SERVER_NAME = "voicekey-singleton"


class SingleInstance(QObject):
    """
    二重起動の検出を担う。

    primary（最初の起動）はローカルサーバーを立てて待ち受け、
    secondary（2 つ目以降）は接続できたことをもって「起動済み」と判断する。
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._server: Optional[QLocalServer] = None

    def try_become_primary(self) -> bool:
        """
        primary になれたら True、既に稼働中なら False を返す。

        失敗時は安全側に倒して True（通常起動）を返す。

        Returns:
            このプロセスが起動を続けてよい場合 True
        """
        try:
            probe = QLocalSocket()
            probe.connectToServer(_SERVER_NAME)
            if probe.waitForConnected(200):
                # 既存インスタンスあり → secondary
                probe.abort()
                return False
            probe.abort()
            # primary: 前回クラッシュ時の残骸を掃除してから待ち受け
            QLocalServer.removeServer(_SERVER_NAME)
            self._server = QLocalServer(self)
            self._server.newConnection.connect(self._on_new_connection)
            if not self._server.listen(_SERVER_NAME):
                logger.warning(
                    f"単一インスタンスサーバーの listen に失敗: {self._server.errorString()}"
                )
                self._server = None
            return True
        except Exception as e:
            # 何があってもアプリ起動は止めない
            logger.warning(f"単一インスタンス判定に失敗（通常起動します）: {e}")
            return True

    def _on_new_connection(self) -> None:
        """secondary からの接続（生存確認）を受けたら、そのまま切断する。"""
        if self._server is None:
            return
        conn = self._server.nextPendingConnection()
        if conn is not None:
            # 保留中の接続を溜めないために取り出して閉じる（やり取りするデータは無い）
            conn.disconnectFromServer()
