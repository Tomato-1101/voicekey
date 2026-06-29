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
    # 新規ログイン/別アカウント切替。先に世代を +1 して旧アカウントの進行中処理
    # （リフレッシュ/短命トークン取得）を無効化してから保存する（旧結果での上書き防止）。
    global _auth_generation
    with _generation_lock:
        _auth_generation += 1
        ok = secrets.save_auth_session(
            session["access_token"], session["refresh_token"], session["expires_at"]
        )
    if not ok:
        # 保存失敗を握りつぶさない（成功扱いで「ログイン済み」を見せない）
        raise BackendError("認証情報の保存に失敗しました", status=None)
    backend_client.clear_token_cache()  # 旧アカウントの短命トークンを破棄
    return session


# 並行リフレッシュ競合を防ぐゲート。同じ refresh_token を同時に複数回使うと
# GoTrue の rotation で refresh_token_already_used となり、reuse 検知で全セッションが
# revoke される（＝ログイン済みでも「ログインが必要です」になる）。RLock にして
# ensure_valid_session() からの再入（ロック下で refresh）を許す。
_refresh_lock = threading.RLock()

# 認証世代。ログアウト/別アカウントのログインで +1 する。非同期に進行中だった
# リフレッシュ/短命トークン取得が「開始時の世代」と違う世代で完了したら、その結果を
# 保存・採用しない＝ログアウト後のセッション復活・別アカウントのトークン混入を防ぐ。
# _generation_lock は「世代の +1」と「セッションの保存/破棄」を不可分にする専用ロック。
# refresh のネットワーク I/O はこのロックの外で行うため、logout はネットワーク完了を待たない。
_generation_lock = threading.Lock()
_auth_generation = 0


def auth_generation() -> int:
    """現在の認証世代を返す（非同期処理の開始時に控え、保存直前に再確認する）。"""
    with _generation_lock:
        return _auth_generation


def _save_session_if_current(gen: int, session: dict) -> bool:
    """開始時 gen が現行世代と一致するときだけセッションを保存する（不可分）。

    一致して保存できたら True。世代が進んでいたら（ログアウト/別ログインが割り込んだ）
    保存せず False を返す。保存自体に失敗したら BackendError を投げる
    （成功扱いで「ログイン済み」を見せない）。
    """
    with _generation_lock:
        if gen != _auth_generation:
            return False
        ok = secrets.save_auth_session(
            session["access_token"], session["refresh_token"], session["expires_at"]
        )
    if not ok:
        raise BackendError("認証情報の保存に失敗しました", status=None)
    return True


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
    gen = auth_generation()  # 開始時の世代を控える（完了時に変わっていたら保存しない）
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
        if e.status == 409:
            # 409（refresh 競合）＝他スレッド/プロセスが既に refresh_token を回転済み
            # ＝セッションは有効。破棄せず、最新の保存済みセッション（無ければ現行）を返す。
            # これを期限切れ扱いすると「ログインの有効期限が切れました」を誤表示する。
            return secrets.get_auth_session() or current
        raise
    session = _decode_session(resp.json())
    if not _save_session_if_current(gen, session):
        # 取得中にログアウト/別アカウントのログインが割り込んだ → セッションを復活させない
        raise BackendError("ログアウトしました。再度ログインしてください", status=401)
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
    """ログアウト（セッション破棄）。device_id は識別子なので残す。

    世代を +1 してから破棄することで、進行中のリフレッシュ/短命トークン取得が
    後から完了してもセッションを保存し直さない（ログアウト後の復活防止）。
    """
    global _auth_generation
    with _generation_lock:
        _auth_generation += 1
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
