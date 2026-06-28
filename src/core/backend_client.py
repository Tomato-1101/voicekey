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
import time
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
        402: "無料体験を使い切りました。続けて使うにはアクティベーションキーの登録が必要です（設定 → アカウント）",
        403: "利用するにはアクティベーションキーの登録が必要です（設定 → アカウント）",
        409: "利用できるデバイス数の上限に達しました",
        429: "リクエストが多すぎます。少し待ってからお試しください",
        503: "サーバー側の設定エラーです。時間をおいてお試しください",
    }.get(status, f"サーバーエラー (HTTP {status})")


def _send(
    method: str,
    path: str,
    *,
    headers: dict,
    _allow_refresh: bool = True,
    raise_on_error: bool = True,
    **kwargs,
) -> httpx.Response:
    """指定メソッドでパスへ送信する。既定では非 200 を BackendError に写す。

    401 かつ Authorization 付きなら、一度だけトークンをリフレッシュして再試行する
    （失効間際を ensure_valid_session で先回りしきれなかった場合の保険）。
    Authorization の無い呼び出し（exchange/refresh/匿名フィードバック）は再試行しない
    ＝リフレッシュの無限再帰も防ぐ。

    raise_on_error=False のときは非 200 でも例外にせず Response を返す
    （redeem のようにサーバーが返す日本語の {error} 本文を呼び出し側で読みたい場合）。
    """
    url = secrets.get_server_base_url() + path
    try:
        resp = _get_client().request(method, url, headers=headers, **kwargs)
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
            return _send(
                method,
                path,
                headers=retry_headers,
                _allow_refresh=False,
                raise_on_error=raise_on_error,
                **kwargs,
            )
    if raise_on_error and resp.status_code != 200:
        raise BackendError(_message_for_status(resp.status_code), status=resp.status_code)
    return resp


def _post(path: str, *, headers: dict, _allow_refresh: bool = True, **kwargs) -> httpx.Response:
    """指定パスへ POST し、非 200 は BackendError に写す（_send の薄いラッパ）。"""
    return _send("POST", path, headers=headers, _allow_refresh=_allow_refresh, **kwargs)


def is_logged_in() -> bool:
    """製品版サーバー経由（短命トークン / プロキシ）を使うべきかを返す。

    ローカルに認証セッション（access_token）があれば True ＝ ログイン済み。
    各文字起こし/整形プリミティブはこれが True のときだけサーバー経路に切り替え、
    False のときは従来の埋め込み/設定キーによる直叩きを使う（段階3 の並存ガード）。
    """
    session = secrets.get_auth_session()
    return bool(session and session.get("access_token"))


def fetch_account_status() -> dict:
    """ログイン中アカウントの状態（email / active / active_until / 無料体験残量）を取得する。

    Returns:
        {"email": Optional[str], "active": bool, "active_until": Optional[str],
         "free_used": int, "free_quota": int, "free_remaining": int}
        active は entitlements が有効（active_until が未来）かどうか。未契約でも
        200 で active:False が返る。active:False かつ free_remaining>0 なら無料体験中、
        free_remaining=0 なら使い切り（呼び出し側はこれで「キー入力が必要」を出す）。

    Raises:
        BackendError: 未ログイン・通信失敗など
    """
    resp = _send("GET", constants.API_ME_PATH, headers=_auth_headers())
    data = resp.json()
    return {
        "email": data.get("email"),
        "active": bool(data.get("active")),
        "active_until": data.get("active_until"),
        "free_used": int(data.get("free_used") or 0),
        "free_quota": int(data.get("free_quota") or 0),
        "free_remaining": int(data.get("free_remaining") or 0),
    }


def redeem_activation_key(code: str) -> Optional[str]:
    """アクティベーションキーを登録（消費）する。成功で利用権がアカウントに紐付く。

    Args:
        code: アクティベーションキー（前後空白は除去して送る）

    Returns:
        利用権の期限（ISO 文字列。サーバーが返さなければ None）

    Raises:
        BackendError: 失敗。サーバーが返す日本語の {error} 本文を優先してメッセージにする
                      （無ければステータスから既定メッセージを引く）
    """
    code = (code or "").strip()
    headers = {**_auth_headers(), "Content-Type": "application/json"}
    # 400 等でもサーバーの {error}（無効なキー / 使用済み 等）を読むため握らない
    resp = _send(
        "POST",
        constants.API_REDEEM_PATH,
        headers=headers,
        json={"code": code},
        raise_on_error=False,
    )
    try:
        body = resp.json()
    except Exception:
        body = {}
    if resp.status_code == 200 and body.get("ok"):
        return body.get("active_until")
    msg = body.get("error") if isinstance(body, dict) else None
    raise BackendError(msg or _message_for_status(resp.status_code), status=resp.status_code)


# 短命トークンのキャッシュと取得の集約（録音ごとの往復を省く）。
# TTL(60秒)内なら録音をまたいで同じ JWT を再利用する。Deepgram は接続確立時にのみ
# トークンを検証し、確立後の長い録音中は再検証しないため、残時間が短くても接続には十分。
# ロック保持中に取得することで、同時呼び出しは待って同じトークンを共有する（二重発行を防ぐ）。
_token_lock = threading.Lock()
_cached_token: Optional[dict] = None  # 値に _expires_monotonic（time.monotonic 基準の失効時刻）を持たせる


def fetch_ephemeral_token() -> dict:
    """Deepgram「高速リアルタイム」用の短命 JWT を取得する。

    残時間が十分なキャッシュがあればネットワーク往復ゼロで即返す（2回目以降の録音を高速化）。

    Returns:
        {"token", "expires_in", "expires_at", "provider"} の dict

    Raises:
        BackendError: 未ログイン・サブスク無効・台数上限・通信失敗など
    """
    global _cached_token
    from . import auth_client

    gen = auth_client.auth_generation()  # 取得開始時の認証世代を控える
    with _token_lock:
        c = _cached_token
        # 残 15 秒超のキャッシュがあれば即返す（往復ゼロ）
        if c and (c.get("_expires_monotonic", 0.0) - time.monotonic()) > 15:
            return c
        # キャッシュ無効 → 取得（ロック保持中＝同時呼び出しは待って新トークンを共有する）
        resp = _post(constants.API_EPHEMERAL_PATH, headers=_auth_headers())
        data = resp.json()
        # 取得中にログアウト/別アカウントのログインが割り込んでいたら、旧アカウントの
        # トークンをキャッシュ・返却しない（別アカウントでの再利用を防ぐ）。
        if auth_client.auth_generation() != gen:
            raise BackendError("ログアウトしました。再度ログインしてください", status=401)
        expires_in = data.get("expires_in") or 60
        data["_expires_monotonic"] = time.monotonic() + float(expires_in)
        _cached_token = data
        return data


def clear_token_cache() -> None:
    """短命トークンのキャッシュを破棄する（ログアウト時。別アカウントでの再利用を防ぐ）。"""
    global _cached_token
    with _token_lock:
        _cached_token = None


def warm_format_proxy() -> None:
    """整形プロキシ（/api/v1/format）の接続を温める（録音開始時に呼ぶ）。

    製品版の整形はこのプロキシ経由なので、TLS・接続・サーバー関数（lambda）を先に温めて
    おくと、録音後の整形の往復（特に最初の cold start）が短くなる。空テキストはサーバーで
    400 になる＝Groq を呼ばず lambda・認証だけ温まる（無料枠の消費もない＝consume:false）。
    失敗（400 含む）は無視。未ログインなら何もしない。ブロッキング I/O のため呼び出し側は
    バックグラウンドスレッドで呼ぶ（既存の prewarm と同じ）。
    """
    if not is_logged_in():
        return
    try:
        _send(
            "POST",
            constants.API_FORMAT_PROXY_PATH,
            headers={**_auth_headers(), "Content-Type": "application/json"},
            json={"text": ""},
            raise_on_error=False,  # 空テキストは 400。暖機が目的なので例外にしない。
        )
    except Exception:
        pass


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


def sync_stats(days: list) -> None:
    """この端末の日次実績（絶対値）をサーバーへ送る（POST /api/v1/stats/sync）。

    サーバーは (user_id, device_id, day) で絶対値 upsert するので、同じ値を再送しても
    二重計上されない（冪等）。最大 60 日。

    Args:
        days: [{"day": "yyyy-MM-dd", "chars": int, "sessions": int, "duration_ms": int}, ...]

    Raises:
        BackendError: 認証・通信エラー（呼び出し側はだいたい握りつぶす）
    """
    if not days:
        return
    headers = {**_auth_headers(), "Content-Type": "application/json"}
    _post(constants.API_STATS_SYNC_PATH, headers=headers, json={"days": days[:60]})


def fetch_stats() -> dict:
    """アカウント横断の使用実績を取得する（GET /api/v1/stats）。

    Returns:
        {"daily": {"yyyy-MM-dd": {characters, recording_seconds, sessions}},
         "total_characters": int, "total_sessions": int, "total_recording_seconds": float}
        （サーバーのミリ秒は秒へ戻す）

    Raises:
        BackendError: 認証・通信エラー
    """
    resp = _send("GET", constants.API_STATS_PATH, headers=_auth_headers())
    body = resp.json() if resp.content else {}
    daily: dict = {}
    for d in body.get("daily") or []:
        day = d.get("day")
        if not day:
            continue
        daily[day] = {
            "characters": int(d.get("chars") or 0),
            "recording_seconds": float(d.get("duration_ms") or 0) / 1000.0,
            "sessions": int(d.get("sessions") or 0),
        }
    totals = body.get("totals") or {}
    return {
        "daily": daily,
        "total_characters": int(totals.get("chars") or 0),
        "total_sessions": int(totals.get("sessions") or 0),
        "total_recording_seconds": float(totals.get("duration_ms") or 0) / 1000.0,
    }


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
