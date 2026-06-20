"""
ブラウザ経由ログインの司令塔モジュール（製品版・Phase 5 段階4・増分2）

ログイン開始（state 生成 → ログイン URL）と deep link 受信
（voicekey://auth?code=&state= の解析 → state 照合 → コード交換）を束ねる。
CSRF 対策の state は「ログイン開始〜deep link 受信」の間だけメモリに保持し、
戻ってきた state と一致しなければ無視する（トークンは URL に乗らずコード交換でのみ取得）。

URL スキーム登録（レジストリ）と OS からの URL 受信配線（単一インスタンス IPC）は
増分3で行う。このモジュールは Qt 非依存（テスト容易）にし、ネットワーク I/O を伴う
complete_login の UI からの呼び出しはワーカースレッドで行う想定。
"""

from typing import Optional, Tuple
from urllib.parse import parse_qs, urlparse

from ..utils.logger import get_logger
from . import auth_client
from .backend_client import BackendError

logger = get_logger(__name__)


class LoginCoordinator:
    """ブラウザ経由ログインの進行を管理する（保留 state の保持・state 照合・交換）。"""

    # 進行状態
    IDLE = "idle"            # 未ログイン・待機なし
    WAITING = "waiting"      # ブラウザでログイン待ち
    EXCHANGING = "exchanging"  # コードをトークンに交換中
    LOGGED_IN = "logged_in"  # ログイン済み
    FAILED = "failed"        # 失敗（error にメッセージ）

    def __init__(self):
        self._pending_state: Optional[str] = None
        # 起動時に保存済みセッションがあればログイン済みから始める
        self.status: str = self.LOGGED_IN if auth_client.secrets.get_auth_session() else self.IDLE
        self.error: Optional[str] = None

    def begin_login(self) -> str:
        """ログインを開始する: state を生成し、ブラウザで開くべき URL を返す。

        実際にブラウザを開くのは呼び出し側（UI）の責務。

        Returns:
            ログインページの URL
        """
        state = auth_client.make_state()
        self._pending_state = state
        self.status = self.WAITING
        self.error = None
        return auth_client.make_login_url(state)

    def complete_login(self, url: str) -> bool:
        """deep link を処理する: URL を解析 → state 照合 → コード交換。

        ネットワーク I/O（exchange）を伴うため、UI からはワーカースレッドで呼ぶこと。

        Args:
            url: 受信した voicekey:// URL

        Returns:
            この URL が自分（ログイン）宛てとして処理されたか。
            別用途の URL は False を返し、呼び出し側で無視させる。
        """
        parsed = self.parse_auth_url(url)
        if parsed is None:
            return False
        code, state = parsed
        # state 照合（CSRF）。保留 state と一致しなければ受け付けない。
        if not self._pending_state or state != self._pending_state:
            self.status = self.FAILED
            self.error = "ログイン要求が一致しませんでした"
            return True
        self._pending_state = None
        self.status = self.EXCHANGING
        try:
            auth_client.exchange_code(code)
            self.status = self.LOGGED_IN
            self.error = None
        except BackendError as e:
            self.status = self.FAILED
            self.error = str(e)
        return True

    def logout(self) -> None:
        """ログアウト（セッション破棄）。"""
        auth_client.logout()
        self._pending_state = None
        self.status = self.IDLE
        self.error = None

    @staticmethod
    def parse_auth_url(url: str) -> Optional[Tuple[str, str]]:
        """voicekey://auth?code=&state= を解析する（純粋関数）。

        scheme/host が一致し code・state が揃っていれば (code, state) を返す。
        それ以外（別スキーム・別ホスト・欠落）は None。

        Args:
            url: 受信 URL 文字列

        Returns:
            (code, state) または None
        """
        try:
            p = urlparse(url)
        except Exception:
            return None
        if p.scheme != "voicekey" or p.netloc != "auth":
            return None
        q = parse_qs(p.query)
        code = (q.get("code") or [""])[0]
        state = (q.get("state") or [""])[0]
        if not code or not state:
            return None
        return code, state
