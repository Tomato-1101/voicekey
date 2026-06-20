"""
deep link（voicekey://）の OS 連携モジュール（製品版・Phase 5 段階4・増分3）

ブラウザ経由ログインのコールバック voicekey://auth?code=&state= を OS から受け取るための連携。
Windows では:
  (1) URL スキームを Windows レジストリに登録する（HKCU\\Software\\Classes\\voicekey）。
  (2) 単一インスタンス化: 2 つ目以降の起動（OS がスキームハンドラとして起動する
      voicekey.exe "voicekey://..."）は、URL を稼働中インスタンスへ転送して即終了する。

レジストリ登録は win32 のみ（他 OS は no-op）。Mac は Info.plist の CFBundleURLTypes と
NSAppleEventManager で受けるため本モジュールの OS 登録は使わない（IPC 部分は Qt 製で
クロスプラットフォームだが、Windows の単一インスタンス用途のみ）。

設計上の安全策: 単一インスタンス判定が何らかの理由で失敗しても、アプリ起動を妨げない
（primary とみなして通常起動する）。deep link が届かなくてもアプリ自体は必ず立ち上がる。
"""

import sys
from typing import List, Optional

from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket

from ..utils.logger import get_logger

logger = get_logger(__name__)

# 単一インスタンス用のローカルソケット名（ユーザー内で一意なら十分）
_SERVER_NAME = "voicekey-singleton"

# deep link の接頭辞（argv からの抽出・転送に使う）
_URL_PREFIX = "voicekey://"


def extract_url_from_argv(argv: List[str]) -> Optional[str]:
    """起動引数から voicekey:// URL を取り出す（無ければ None）。

    OS がスキームハンドラとして起動すると argv に URL が 1 つ載る。

    Args:
        argv: sys.argv 相当のリスト

    Returns:
        最初に見つかった voicekey:// URL、無ければ None
    """
    for arg in argv[1:]:
        if arg.startswith(_URL_PREFIX):
            return arg
    return None


class SingleInstance(QObject):
    """単一インスタンス化と deep link 転送を担う。

    primary（最初の起動）はローカルサーバーを立てて待ち受け、
    secondary（2 つ目以降）は URL を primary へ送って終了する。

    Signals:
        url_received(str): primary が転送 URL を受信したとき発火
    """

    url_received = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._server: Optional[QLocalServer] = None

    def try_become_primary(self) -> bool:
        """primary になれたら True、既に稼働中なら False。

        失敗時は安全側に倒して True（通常起動）を返す。
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
            # 何があってもアプリ起動は止めない（deep link 転送だけ諦める）
            logger.warning(f"単一インスタンス判定に失敗（通常起動します）: {e}")
            return True

    def send_to_primary(self, message: str) -> bool:
        """稼働中の primary へメッセージ（URL）を送る。

        Args:
            message: 送る文字列（deep link URL）

        Returns:
            送信に成功したら True
        """
        try:
            sock = QLocalSocket()
            sock.connectToServer(_SERVER_NAME)
            if not sock.waitForConnected(500):
                return False
            sock.write(message.encode("utf-8"))
            sock.flush()
            sock.waitForBytesWritten(500)
            sock.disconnectFromServer()
            return True
        except Exception as e:
            logger.warning(f"primary への転送に失敗: {e}")
            return False

    def _on_new_connection(self) -> None:
        """secondary からの接続を受け、受信文字列を url_received で流す。"""
        if self._server is None:
            return
        conn = self._server.nextPendingConnection()
        if conn is None:
            return
        if conn.waitForReadyRead(500):
            data = bytes(conn.readAll()).decode("utf-8", "ignore").strip()
            if data:
                self.url_received.emit(data)
        conn.disconnectFromServer()


def register_url_scheme() -> bool:
    """voicekey:// を Windows レジストリに登録する（win32 のみ）。

    OS がこのスキームの URL を開くとき、登録した実行ファイルを
    `voicekey.exe "voicekey://..."` の形で起動する。配布（凍結）ビルドでのみ意味があり、
    開発実行や他 OS では no-op で False を返す。

    Returns:
        登録した場合 True、対象外（非 win32・非凍結）や失敗時は False
    """
    if sys.platform != "win32":
        return False
    # 凍結（PyInstaller）ビルドのみ登録する。開発実行は python 経由で
    # コマンドが複雑になり、配布時とパスが食い違うため対象外にする。
    if not getattr(sys, "frozen", False):
        return False
    try:
        import winreg

        exe = sys.executable
        base = r"Software\Classes\voicekey"
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, base) as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, "URL:voicekey Protocol")
            winreg.SetValueEx(key, "URL Protocol", 0, winreg.REG_SZ, "")
        with winreg.CreateKey(
            winreg.HKEY_CURRENT_USER, base + r"\shell\open\command"
        ) as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, f'"{exe}" "%1"')
        return True
    except Exception as e:
        logger.warning(f"URL スキーム登録に失敗: {e}")
        return False
