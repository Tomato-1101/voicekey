"""
製品版バックエンドクライアントモジュール（Phase 5 段階2）

製品版は長期 API キーをアプリに持たない。Supabase の access_token で自社サーバーに
認証し、サーバーがサブスク有効性を検証する。Deepgram は短命 JWT を受け取って
アプリが直叩き（低レイテンシ核心を維持）、ElevenLabs/Groq はサーバープロキシ経由で叩く。

このモジュールは「クライアントの提供」だけを担う。既存の api_transcriber /
streaming_transcriber / text_formatter への配線は段階3 で行う。
サーバー契約は voicekey-site の app/api/v1/{auth/ephemeral,transcribe/elevenlabs,format} と一致させる。
"""

import threading
from typing import Optional

import httpx

from ..config import constants
from ..utils import secrets
from ..utils.logger import get_logger

logger = get_logger(__name__)

# 接続は短め、読み取り（プロキシ経由の文字起こし）は長めに許容する
_TIMEOUT = httpx.Timeout(15.0, connect=5.0, read=60.0)

# プロキシ呼び出しの HTTP クライアントは使い回す（TLS keep-alive 再利用）。
# 認証ヘッダはリクエストごとに変わる（トークン更新があるため）ので付けない。
_client: Optional[httpx.Client] = None
_client_lock = threading.Lock()


class BackendError(Exception):
    """自社バックエンド呼び出しのエラー（ユーザー向け日本語メッセージ付き）。"""

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


def _get_client() -> httpx.Client:
    """共有 httpx.Client を取得または生成する（テストでは差し替え可能）。"""
    global _client
    with _client_lock:
        if _client is None:
            _client = httpx.Client(
                timeout=_TIMEOUT,
                limits=httpx.Limits(
                    max_connections=5,
                    max_keepalive_connections=2,
                    keepalive_expiry=60.0,
                ),
                transport=httpx.HTTPTransport(retries=2),
            )
        return _client


def _auth_headers() -> dict:
    """Bearer access_token + device_id + platform のヘッダーを組み立てる。

    失効間際なら送信前にトークンをリフレッシュする（先回り）。

    Raises:
        BackendError: ローカルに認証セッションが無い（未ログイン）場合
    """
    # 失効間際なら先にリフレッシュ（遅延 import で循環回避）。
    # 未ログイン・リフレッシュ失敗はここでは握り、下の存在チェックで 401 を出す。
    from . import auth_client

    try:
        auth_client.ensure_valid_session()
    except BackendError:
        pass

    session = secrets.get_auth_session()
    if not session or not session.get("access_token"):
        raise BackendError("ログインが必要です", status=401)
    return {
        "Authorization": f"Bearer {session['access_token']}",
        "x-device-id": secrets.get_device_id(),
        "x-platform": "windows",
    }


def _message_for_status(status: int) -> str:
    """HTTP ステータスをユーザー向け日本語メッセージへ写す。"""
    return {
        401: "ログインの有効期限が切れました。再度ログインしてください",
        403: "サブスクリプションが有効ではありません",
        409: "利用できるデバイス数の上限に達しました",
        429: "リクエストが多すぎます。少し待ってからお試しください",
        503: "サーバー側の設定エラーです。時間をおいてお試しください",
    }.get(status, f"サーバーエラー (HTTP {status})")


def _post(path: str, *, headers: dict, _allow_refresh: bool = True, **kwargs) -> httpx.Response:
    """指定パスへ POST し、非 200 は BackendError に写す。

    401 かつ Authorization 付きなら、一度だけトークンをリフレッシュして再試行する
    （失効間際を ensure_valid_session で先回りしきれなかった場合の保険）。
    Authorization の無い呼び出し（exchange/refresh/匿名フィードバック）は再試行しない
    ＝リフレッシュの無限再帰も防ぐ。
    """
    url = secrets.get_server_base_url() + path
    try:
        resp = _get_client().post(url, headers=headers, **kwargs)
    except httpx.TimeoutException:
        raise BackendError("サーバーへの接続がタイムアウトしました", status=None)
    except httpx.HTTPError as e:
        raise BackendError(f"サーバーへの接続に失敗しました: {e}", status=None)
    if resp.status_code == 401 and _allow_refresh and "Authorization" in headers:
        from . import auth_client

        try:
            new_session = auth_client.refresh()
        except BackendError:
            new_session = None
        if new_session and new_session.get("access_token"):
            retry_headers = {**headers, "Authorization": f"Bearer {new_session['access_token']}"}
            return _post(path, headers=retry_headers, _allow_refresh=False, **kwargs)
    if resp.status_code != 200:
        raise BackendError(_message_for_status(resp.status_code), status=resp.status_code)
    return resp


def is_logged_in() -> bool:
    """製品版サーバー経由（短命トークン / プロキシ）を使うべきかを返す。

    ローカルに認証セッション（access_token）があれば True ＝ ログイン済み。
    各文字起こし/整形プリミティブはこれが True のときだけサーバー経路に切り替え、
    False のときは従来の埋め込み/設定キーによる直叩きを使う（段階3 の並存ガード）。
    """
    session = secrets.get_auth_session()
    return bool(session and session.get("access_token"))


def fetch_ephemeral_token() -> dict:
    """Deepgram「高速リアルタイム」用の短命 JWT を取得する。

    Returns:
        {"token", "expires_in", "expires_at", "provider"} の dict

    Raises:
        BackendError: 未ログイン・サブスク無効・台数上限・通信失敗など
    """
    resp = _post(constants.API_EPHEMERAL_PATH, headers=_auth_headers())
    return resp.json()


def transcribe_elevenlabs(wav_bytes: bytes, language: str = "") -> str:
    """ElevenLabs「正確性」プロキシで文字起こしする（WAV を multipart 送信）。

    Args:
        wav_bytes: WAV バイト列
        language: 言語コード（空なら送らない＝サーバー/プロバイダ自動判定）

    Returns:
        文字起こし結果テキスト

    Raises:
        BackendError: 認証・サブスク・通信エラー
    """
    files = {"file": ("audio.wav", wav_bytes, "audio/wav")}
    data = {"language": language} if language else {}
    resp = _post(
        constants.API_ELEVENLABS_PROXY_PATH,
        headers=_auth_headers(),
        files=files,
        data=data,
    )
    return resp.json().get("text", "") or ""


def format_text(text: str) -> str:
    """Groq テキスト整形プロキシ（モデル/プロンプトはサーバー固定。text のみ送る）。

    Args:
        text: 整形前テキスト

    Returns:
        整形後テキスト（失敗時は呼び出し側で原文フォールバックする想定）

    Raises:
        BackendError: 認証・サブスク・通信エラー
    """
    headers = {**_auth_headers(), "Content-Type": "application/json"}
    resp = _post(constants.API_FORMAT_PROXY_PATH, headers=headers, json={"text": text})
    return resp.json().get("text", text) or text


def submit_feedback(message: str) -> None:
    """アプリ内フィードバックを自社サーバーへ送る（認証は任意）。

    ログイン済みなら Bearer を付けて user_id に紐付け、未ログインでも
    device_id + app_version で送れる（サブスク有効性は問わない＝誰でも要望を出せる）。

    Args:
        message: フィードバック本文

    Raises:
        BackendError: 通信失敗・非 200（呼び出し側でユーザーに表示する）
    """
    headers = {
        "Content-Type": "application/json",
        "x-device-id": secrets.get_device_id(),
        "x-platform": "windows",
    }
    # ログイン済みなら Bearer を付ける（未ログインでも送れるよう必須にしない）
    session = secrets.get_auth_session()
    if session and session.get("access_token"):
        headers["Authorization"] = f"Bearer {session['access_token']}"
    _post(
        constants.API_FEEDBACK_PATH,
        headers=headers,
        json={"message": message, "app_version": constants.APP_VERSION},
    )
