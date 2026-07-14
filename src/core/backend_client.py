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
# キャッシュ再利用は、サーバーが cacheable:true と返したときだけ行う。段階3では有料・無料とも
# 再利用可トークン（cacheable:true・free は meter:true）を返す＝録音開始の往復ゼロ。無料枠の消費は
# 「トークン発行」ではなく「録音成立後の非同期 confirm」で数えるので、TTL 内でトークンを使い回して
# 大丈夫（N 録音 → N 回 confirm → N 消費）。cacheable 不明（旧サーバー）は録音ごとに取り直す。
# Deepgram は接続確立時にのみトークンを検証するため、TTL 内の再利用は接続に十分。
# ロック保持中に取得することで、同時呼び出しは待って同じトークンを共有する（二重発行を防ぐ）。
_token_lock = threading.Lock()
_cached_token: Optional[dict] = None  # 値に _expires_monotonic（time.monotonic 基準の失効時刻）を持たせる

# warm 先読みがキャッシュを更新する残 TTL 閾値（秒）。暖機間隔 240s ＋ 満了前マージン 120s。
# warm ループ／録音後先読みはトークン残がこれ未満なら強制再取得するので、キャッシュは常に満了
# 120 秒以上前に差し替わる＝「満了〜次 tick」の周期コールド窓（録音がトークン往復＋WS 接続を待つ）が
# 消える。録音開始のクリティカルパスは従来どおり残 15 秒で再利用する（Mac の warmMinRemaining と対）。
WARM_MIN_REMAINING = 360.0


def fetch_ephemeral_token(min_remaining: float = 15.0) -> dict:
    """Deepgram「高速リアルタイム」用の短命 JWT を取得する。

    段階3: 有料・無料ともキャッシュ有効なら往復ゼロで即返す（2回目以降の録音を高速化）。
    無料体験の消費は録音成立後の非同期 confirm（jti なし）で数えるので、TTL 内で使い回してよい。

    Args:
        min_remaining: キャッシュを再利用する最小残 TTL（秒）。これ未満なら強制再取得する。
            録音開始のクリティカルパスは既定 15 秒（従来どおり）。warm 先読みは WARM_MIN_REMAINING
            (360s) を渡し、満了のはるか手前で更新して「満了〜次 warm tick」の周期コールド窓を無くす。

    Returns:
        {"token", "expires_in", "expires_at", "provider", "cacheable", "meter"} の dict

    Raises:
        BackendError: 未ログイン・サブスク無効・台数上限・通信失敗など
    """
    global _cached_token
    from . import auth_client

    gen = auth_client.auth_generation()  # 取得開始時の認証世代を控える
    with _token_lock:
        c = _cached_token
        # 残 min_remaining 超のキャッシュがあれば即返す（warm 先読みは 360・録音開始は 15・往復ゼロ）
        if c and (c.get("_expires_monotonic", 0.0) - time.monotonic()) > min_remaining:
            # free の保留トークン(jti 付き)は「1保留=1録音=1confirm」を守るため 1 回使い切り
            # （取り出したらキャッシュから除去）。paid(jti なし)は TTL 内で使い回す。
            if c.get("jti"):
                _cached_token = None
            return c
        # キャッシュ無効 → 取得（ロック保持中＝同時呼び出しは待って新トークンを共有する）。
        # 段階3: 「録音成功後に /usage/confirm（jti なし）で回数を数えられる新クライアント」を宣言。
        # サーバーは free でも即時消費せず、有料と同じ再利用可トークン（cacheable:true・meter:true）を
        # 返す＝録音開始のサーバー往復を消す。無料枠の +1 は録音後の非同期 confirm で行う。
        resp = _post(constants.API_EPHEMERAL_PATH, headers={**_auth_headers(), "x-vk-confirm": "2"})
        data = resp.json()
        # 取得中にログアウト/別アカウントのログインが割り込んでいたら、旧アカウントの
        # トークンをキャッシュ・返却しない（別アカウントでの再利用を防ぐ）。
        if auth_client.auth_generation() != gen:
            raise BackendError("ログアウトしました。再度ログインしてください", status=401)
        expires_in = data.get("expires_in") or 60
        data["_expires_monotonic"] = time.monotonic() + float(expires_in)
        # 段階3: サーバーが cacheable（有料 or 無料の再利用トークン）と返したときだけキャッシュする。
        # 無料枠の消費は confirm（jti なし）で数えるので、キャッシュ再利用しても数え落ちない。
        # cacheable 不明（旧サーバー）のときはキャッシュしない＝毎録音で取り直す（安全側）。
        _cached_token = data if data.get("cacheable") is True else None
        return data


def clear_token_cache() -> None:
    """短命トークンのキャッシュを破棄する（ログアウト時。別アカウントでの再利用を防ぐ）。"""
    global _cached_token
    with _token_lock:
        _cached_token = None


def confirm_usage(jti: str) -> None:
    """無料体験の消費を確定する（録音成功後に保留 jti を送る・ベストエフォート）。

    録音開始のクリティカルパスから消費を外すための後段。失敗しても無視
    （サーバー側で保留は TTL 経過後に整合する）。未ログイン/ jti なしなら何もしない。

    Args:
        jti: ephemeral が無料体験に対して返した保留 ID
    """
    if not jti or not is_logged_in():
        return
    try:
        _send(
            "POST",
            constants.API_USAGE_CONFIRM_PATH,
            headers={**_auth_headers(), "Content-Type": "application/json"},
            json={"jti": jti},
            raise_on_error=False,  # ベストエフォート（失敗してもユーザー操作を止めない）
        )
    except Exception:
        pass


def confirm_usage_count() -> None:
    """無料体験の消費を確定する（段階3・再利用トークン用＝jti なし）。

    録音成立ごとに送り、サーバーは increment_free_used で free_used を +1 する（paid は no-op）。
    録音開始のクリティカルパスから消費計測を外すための後段＝「録音開始のサーバー往復ゼロ」を
    実現する要。失敗しても無視（ベストエフォート・多少の数え落ちは許容）。未ログインなら何もしない。
    """
    if not is_logged_in():
        return
    try:
        _send(
            "POST",
            constants.API_USAGE_CONFIRM_PATH,
            headers={**_auth_headers(), "Content-Type": "application/json"},
            json={},  # jti なし＝サーバーは「段階3の回数確定」と解釈して increment する
            raise_on_error=False,  # ベストエフォート
        )
    except Exception:
        pass


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

    multipart は ElevenLabs がそのまま受け取れる形（file + model_id=scribe_v1 + language_code）で
    組み、x-vk-passthrough: 1 を付ける。サーバーはこのヘッダを見たらボディを read せず EL へ
    ストリーム透過する（formData の全量バッファ＋再構築をやめる＝中継の二度手間を削減）。

    Args:
        wav_bytes: WAV バイト列
        language: 言語コード（空なら送らない＝サーバー/プロバイダ自動判定）

    Returns:
        文字起こし結果テキスト

    Raises:
        BackendError: 認証・サブスク・通信エラー
    """
    files = {"file": ("audio.wav", wav_bytes, "audio/wav")}
    # モデルは scribe_v1 固定（製品版仕様）。サーバーが透過するため EL 形式で組む。
    data = {"model_id": "scribe_v1"}
    if language:
        data["language_code"] = language
    resp = _post(
        constants.API_ELEVENLABS_PROXY_PATH,
        headers={**_auth_headers(), "x-vk-passthrough": "1"},
        files=files,
        data=data,
        # 文字起こしは本質的に時間がかかる（cold start＋長文＋日米越境往復）。録音直前の token 取得用に
        # 短く設定した共有クライアント既定(15s)のままだと長文で応答前に切れ「接続に失敗」になるため、
        # この呼び出しだけ余裕を持たせる（接続は短く、全体は長く）。
        timeout=httpx.Timeout(90.0, connect=5.0),
    )
    return resp.json().get("text", "") or ""


def warm_elevenlabs() -> None:
    """ElevenLabs「正確性」プロキシ（/api/v1/transcribe/elevenlabs）の serverless 関数を温める。

    「正確性」初回利用時の Vercel 関数 cold start（最大数秒）が遅延の主因なので、起動時・
    数分間隔で消費なしの GET を叩いて同じ関数を温存し、録音後の文字起こし POST を warm path に
    乗せる。GET は無料枠を消費せず・EL も Supabase も叩かない（即 200）。認証ヘッダは付けない。
    失敗は無視。ブロッキング I/O なので呼び出し側はバックグラウンドスレッドで呼ぶ。
    """
    if not is_logged_in():
        return
    try:
        # headers={} ＝認証なし。空ヘッダなので 401 リフレッシュ再試行も発生しない。
        _send("GET", constants.API_ELEVENLABS_PROXY_PATH, headers={}, raise_on_error=False)
    except Exception:
        pass


def transcribe_groq(wav_bytes: bytes, language: str = "", server_format: bool = False) -> str:
    """Groq「高速」プロキシで文字起こしする（普通入力・WAV を multipart 送信）。

    Groq は Deepgram のような client 直叩き用の短命トークンが無いので、ElevenLabs と同じく
    サーバープロキシ経由。サーバー(Edge・東京)は formData(file + language)を受けて Groq 形式
    (model=whisper-large-v3-turbo / response_format=text)へ組み直して中継する＝クライアントは
    file と language だけ送る（EL の passthrough とは違い透過フラグ・model_id は付けない）。
    サーバーは consume:true で無料枠を同期消費するので、クライアント側の confirm は不要。

    Args:
        wav_bytes: WAV バイト列
        language: 言語コード（空なら送らない＝サーバー/プロバイダ自動判定）
        server_format: multipart に format=1 を付けるか。True にするとサーバー内で STT→整形まで
            行い、整形済みテキストを text で返す（STT と整形を 1 リクエストに統合＝録音後の
            クライアント整形の往復を省く）。整形失敗時でも text に STT 原文が入って返るので、
            呼び出し側は text をそのまま最終テキストとして使う（再整形はしない）。

    Returns:
        文字起こし結果テキスト（server_format 時は整形済み）

    Raises:
        BackendError: 認証・サブスク・通信エラー
    """
    files = {"file": ("audio.wav", wav_bytes, "audio/wav")}
    data = {}
    if language:
        data["language"] = language
    # サーバー統合整形の依頼フラグ。サーバーはこれを見たら STT→Groq 整形まで行い整形済みを返す。
    if server_format:
        data["format"] = "1"
    resp = _post(
        constants.API_GROQ_TRANSCRIBE_PROXY_PATH,
        headers=_auth_headers(),
        files=files,
        data=data,
        # 普通入力は短尺（ホールド発話）だが、cold start・越境往復に備え少し余裕を持たせる
        # （長文の EL=90s とは別事情）。統合整形時はサーバーで LLM 整形も走るため 90s に広げる。
        timeout=httpx.Timeout(90.0 if server_format else 60.0, connect=5.0),
    )
    return resp.json().get("text", "") or ""


def warm_groq_transcribe() -> None:
    """Groq「高速」プロキシ（/api/v1/transcribe/groq）の serverless 関数を温める。

    Edge なので cold start はほぼ無いが、EL/format と同じ暖機導線で TLS・接続を先に温めておく。
    GET は無料枠を消費せず・Groq も Supabase も叩かない（即 200）。認証ヘッダは付けない。
    失敗は無視。ブロッキング I/O なので呼び出し側はバックグラウンドスレッドで呼ぶ。
    """
    if not is_logged_in():
        return
    try:
        _send("GET", constants.API_GROQ_TRANSCRIBE_PROXY_PATH, headers={}, raise_on_error=False)
    except Exception:
        pass


def warm_session() -> None:
    """放置後初回の「セッション期限切れ→更新往復」を録音のクリティカルパスから外す。

    暖機のたびに先回りでセッションを有効化しておく。Groq/EL プロキシ呼び出しは内部で
    ensure_valid_session を呼ぶが、それを事前に済ませておけば録音時の更新往復が消える。
    未ログインなら何もしない。失敗は無視（録音時に通常経路で更新される）。
    """
    if not is_logged_in():
        return
    try:
        from . import auth_client
        auth_client.ensure_valid_session()
    except Exception:
        pass


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
    # 整形も長文だと時間がかかる。token 取得用の短い共有クライアント既定(15s)を上書きして余裕を持たせる。
    resp = _post(
        constants.API_FORMAT_PROXY_PATH,
        headers=headers,
        json={"text": text},
        timeout=httpx.Timeout(60.0, connect=5.0),
    )
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
         "total_characters": int, "total_sessions": int, "total_recording_seconds": float,
         "self_daily": {...} or None}
        （サーバーのミリ秒は秒へ戻す）
        self_daily は #11 のサーバー baseline（この端末ぶんのサーバー値）。サーバーが
        返さない旧サーバーでは None（呼び出し側は従来の max 合成にフォールバックする）。

    Raises:
        BackendError: 認証・通信エラー
    """
    resp = _send("GET", constants.API_STATS_PATH, headers=_auth_headers())
    body = resp.json() if resp.content else {}

    def _parse_daily(rows: list) -> dict:
        out: dict = {}
        for d in rows or []:
            day = d.get("day")
            if not day:
                continue
            out[day] = {
                "characters": int(d.get("chars") or 0),
                "recording_seconds": float(d.get("duration_ms") or 0) / 1000.0,
                "sessions": int(d.get("sessions") or 0),
            }
        return out

    daily = _parse_daily(body.get("daily") or [])
    # self_daily はキーが在るときだけ dict（空配列でも {}）にする。キーが無い旧サーバーは None。
    self_daily = _parse_daily(body.get("self_daily") or []) if "self_daily" in body else None
    totals = body.get("totals") or {}
    return {
        "daily": daily,
        "total_characters": int(totals.get("chars") or 0),
        "total_sessions": int(totals.get("sessions") or 0),
        "total_recording_seconds": float(totals.get("duration_ms") or 0) / 1000.0,
        "self_daily": self_daily,
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
