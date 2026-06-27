"""
製品版 認証クライアントモジュール（Phase 5 段階4）

ブラウザ経由ログインと Supabase セッションの維持を担う。
フロー: アプリが state を生成 → 既定ブラウザで /auth/app?state=... を開く →
ユーザーがログイン → サーバーがワンタイムコードを発行 → deep link
voicekey://auth?code=&state= でアプリに戻る → exchange でコードをトークンに交換 →
Credential Manager に保存。トークンは URL に乗らず、ワンタイムコードの交換でのみ取得する。

このモジュールは「ログイン URL 生成 / コード交換 / トークン更新」を担う。
URL スキーム登録・deep link 受信・ログイン UI 配線は後続の増分で行う。
サーバー契約は voicekey-site の app/{auth/app, api/v1/auth/{exchange,refresh}} と一致させる。
エラー型・HTTP クライアントは backend_client と共有する（呼び出し側のエラー処理を一本化）。
"""

import threading
import time
import urllib.parse
from secrets import token_hex  # stdlib（CSRF 用 state 生成）

from ..config import constants
from ..utils import secrets
from ..utils.logger import get_logger
from . import backend_client
from .backend_client import BackendError

logger = get_logger(__name__)


def make_state() -> str:
    """CSRF 対策のランダム state を生成する。

    呼び出し側がこれを保持し、deep link で戻ってきた state と照合する。

    Returns:
        32 桁の hex 文字列
    """
    return token_hex(16)


def make_login_url(state: str) -> str:
    """ブラウザで開くログイン URL を生成する。

    device_id / platform はサーバーがログインコードに紐付けるために渡す。

    Args:
        state: make_state() で生成した CSRF トークン

    Returns:
        完全なログイン URL（例 "https://voicekey.vercel.app/auth/app?state=..."）
    """
    base = secrets.get_server_base_url()
    query = urllib.parse.urlencode(
        {
            "state": state,
            "device_id": secrets.get_device_id(),
            "platform": "windows",
        }
    )
    return f"{base}{constants.AUTH_APP_PATH}?{query}"


def exchange_code(code: str) -> dict:
    """ワンタイムコードをトークンに交換し、Credential Manager に保存する。

    Args:
        code: deep link で受け取ったワンタイムコード

    Returns:
        {"access_token", "refresh_token", "expires_at"} の dict

    Raises:
        BackendError: コード失効/不正（status=410）・通信失敗など
    """
    resp = backend_client._post(
        constants.API_AUTH_EXCHANGE_PATH,
        headers={"Content-Type": "application/json"},
        json={
            "code": code,
            "device_id": secrets.get_device_id(),
            "platform": "windows",
        },
    )
    session = _decode_session(resp.json())
    secrets.save_auth_session(
        session["access_token"], session["refresh_token"], session["expires_at"]
    )
    return session


# 並行リフレッシュ競合を防ぐゲート。同じ refresh_token を同時に複数回使うと
# GoTrue の rotation で refresh_token_already_used となり、reuse 検知で全セッションが
# revoke される（＝ログイン済みでも「ログインが必要です」になる）。RLock にして
# ensure_valid_session() からの再入（ロック下で refresh）を許す。
_refresh_lock = threading.RLock()


def refresh() -> dict:
    """保存済み refresh_token で access_token を更新し、保存する。

    並行呼び出しは直列化し、ロック取得後に最新セッションを読み直すことで
    同じ refresh_token を同時に複数回使わない（rotation 競合を防ぐ）。
    refresh_token も失効していれば（401）セッションを破棄して再ログインを促す。

    Returns:
        更新後の {"access_token", "refresh_token", "expires_at"} の dict

    Raises:
        BackendError: 未ログイン（status=401）・失効（status=401）・通信失敗など
    """
    with _refresh_lock:
        return _perform_refresh()


def _perform_refresh() -> dict:
    """実際のリフレッシュ処理（_refresh_lock 保持下でのみ呼ぶ）。

    409（refresh 競合）は他スレッドが更新済み＝セッションは有効なので破棄しない。
    """
    current = secrets.get_auth_session()
    if not current or not current.get("refresh_token"):
        raise BackendError("ログインが必要です", status=401)
    try:
        resp = backend_client._post(
            constants.API_AUTH_REFRESH_PATH,
            headers={"Content-Type": "application/json"},
            json={"refresh_token": current["refresh_token"]},
        )
    except BackendError as e:
        if e.status == 401:
            secrets.clear_auth_session()  # 復帰不能 → 再ログインへ
        raise
    session = _decode_session(resp.json())
    secrets.save_auth_session(
        session["access_token"], session["refresh_token"], session["expires_at"]
    )
    return session


def ensure_valid_session() -> None:
    """有効な access_token を保証する。失効 60 秒前を切っていればリフレッシュする。

    ロックを取得してから期限を再確認し、直前に他スレッドが更新済みなら
    リフレッシュしない（並行リフレッシュ競合の二重防止）。

    Raises:
        BackendError: 未ログイン・リフレッシュ失敗
    """
    with _refresh_lock:
        current = secrets.get_auth_session()
        if not current:
            raise BackendError("ログインが必要です", status=401)
        expires_at = current.get("expires_at") or 0
        if expires_at - time.time() < 60:
            _perform_refresh()


def logout() -> None:
    """ログアウト（セッション破棄）。device_id は識別子なので残す。"""
    secrets.clear_auth_session()
    backend_client.clear_token_cache()  # 別アカウントでの短命トークン再利用を防ぐ


def _decode_session(data: dict) -> dict:
    """サーバー応答（access_token / refresh_token / expires_at(UNIX秒)）を検証して dict 化。

    Raises:
        BackendError: 必須フィールド欠落
    """
    access = data.get("access_token")
    refresh_token = data.get("refresh_token")
    if not access or not refresh_token:
        raise BackendError("サーバー応答を解釈できませんでした", status=None)
    expires_at = data.get("expires_at")
    if not isinstance(expires_at, (int, float)):
        # expires_at 欠落時は GoTrue 既定 TTL（1時間後）を仮定する
        expires_at = time.time() + 3600
    return {
        "access_token": access,
        "refresh_token": refresh_token,
        "expires_at": float(expires_at),
    }
