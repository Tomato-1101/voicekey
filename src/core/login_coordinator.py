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

# ログイン/ログアウトを各ストア（実績など）へ伝える観測者（Mac 版の
# NotificationCenter .voicekeyDidLogin/.voicekeyDidLogout に相当）。
# Qt 非依存のまま疎結合にするため、コールバックを登録して発火する。
_login_observers: list = []
_logout_observers: list = []


def add_login_observer(callback) -> None:
    """ログイン成立時に呼ぶコールバックを登録する（多重登録は無視）。"""
    if callback not in _login_observers:
        _login_observers.append(callback)


def add_logout_observer(callback) -> None:
    """ログアウト時に呼ぶコールバックを登録する（多重登録は無視）。"""
    if callback not in _logout_observers:
        _logout_observers.append(callback)


def _notify(observers: list) -> None:
    """観測者を順に呼ぶ。1 つ落ちても他へ波及させない。"""
    for cb in list(observers):
        try:
            cb()
        except Exception as e:  # 通知失敗は本筋（ログイン/アウト）を妨げない
            logger.warning(f"ログイン状態の通知に失敗: {e}")


class LoginCoordinator:
    """ブラウザ経由ログインの進行を管理する（保留 state の保持・state 照合・交換）。"""

    # 進行状態
    IDLE = "idle"            # 未ログイン・待機なし
    WAITING = "waiting"      # ブラウザでログイン待ち
    EXCHANGING = "exchanging"  # コードをトークンに交換中
    LOGGED_IN = "logged_in"  # ログイン済み
    FAILED = "failed"        # 失敗（error にメッセージ）

    # 利用権（アクティベーションキー）の状態
    ENT_UNKNOWN = "unknown"    # 未確認（未ログイン時など）
    ENT_CHECKING = "checking"  # 確認中
    ENT_ACTIVE = "active"      # 有効（active_until に期限。無期限なら None）
    ENT_NONE = "none"          # ログイン済みだが未登録（キー入力が必要）
    ENT_ERROR = "error"        # 確認失敗（entitlement_error にメッセージ）

    def __init__(self):
        self._pending_state: Optional[str] = None
        # 起動時に保存済みセッションがあればログイン済みから始める
        logged_in = bool(auth_client.secrets.get_auth_session())
        self.status: str = self.LOGGED_IN if logged_in else self.IDLE
        self.error: Optional[str] = None
        # 利用権の状態（UI 表示用。ネットワーク確認は refresh_entitlement で行う）
        self.entitlement: str = self.ENT_UNKNOWN
        self.entitlement_active_until: Optional[str] = None
        self.entitlement_error: Optional[str] = None
        self.account_email: Optional[str] = None

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
            # ログイン直後に利用権を確認（同じワーカースレッド内で続けて取得）
            self.refresh_entitlement()
            # 各ストア（実績など）にログインを通知して端末横断同期を促す（#10）。
            # このワーカースレッド内で呼ばれるので、同期の HTTP は UI を止めない。
            _notify(_login_observers)
        except BackendError as e:
            self.status = self.FAILED
            self.error = str(e)
        return True

    def refresh_entitlement(self) -> None:
        """ログイン中アカウントの利用権を再確認する（/api/v1/me を同期で叩く）。

        ネットワーク I/O を伴うため、UI からはワーカースレッドで呼ぶこと。
        結果は self.entitlement / entitlement_active_until / account_email に反映する。
        """
        from . import backend_client

        if not auth_client.secrets.get_auth_session():
            self.entitlement = self.ENT_UNKNOWN
            return
        self.entitlement = self.ENT_CHECKING
        try:
            s = backend_client.fetch_account_status()
            self.account_email = s.get("email")
            if s.get("active"):
                self.entitlement = self.ENT_ACTIVE
                self.entitlement_active_until = s.get("active_until")
            else:
                self.entitlement = self.ENT_NONE
                self.entitlement_active_until = None
        except BackendError as e:
            self.entitlement = self.ENT_ERROR
            self.entitlement_error = str(e)

    def redeem(self, code: str) -> Tuple[bool, str]:
        """アクティベーションキーを登録する（/api/v1/activation/redeem を同期で叩く）。

        成功すると利用権がアカウントに紐付き、self.entitlement を ACTIVE に更新する。
        ネットワーク I/O を伴うため、UI からはワーカースレッドで呼ぶこと。

        Args:
            code: アクティベーションキー

        Returns:
            (成功か, ユーザー向けメッセージ)
        """
        from . import backend_client

        trimmed = (code or "").strip()
        if not trimmed:
            return False, "キーを入力してください"
        try:
            until = backend_client.redeem_activation_key(trimmed)
            self.entitlement = self.ENT_ACTIVE
            self.entitlement_active_until = until
            self.entitlement_error = None
            return True, "アクティベーションキーを登録しました"
        except BackendError as e:
            return False, str(e)

    def logout(self) -> None:
        """ログアウト（セッション破棄）。"""
        auth_client.logout()
        self._pending_state = None
        self.status = self.IDLE
        self.error = None
        self.entitlement = self.ENT_UNKNOWN
        self.entitlement_active_until = None
        self.entitlement_error = None
        self.account_email = None
        # 各ストアにログアウトを通知して端末横断の取り込み結果を破棄させる（#10）
        _notify(_logout_observers)

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


# アプリ全体で 1 つの司令塔を共有する（設定 UI のログインボタンと deep link ハンドラが
# 同じ保留 state を見る必要があるため）。初回アクセス時に生成する（import 時に keyring を
# 触らないよう遅延生成。Mac 版の LoginCoordinator.shared と対応）。
_shared: Optional[LoginCoordinator] = None


def shared() -> LoginCoordinator:
    """アプリ共有の LoginCoordinator を返す（遅延生成）。"""
    global _shared
    if _shared is None:
        _shared = LoginCoordinator()
    return _shared
