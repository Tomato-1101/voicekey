"""
リアルタイム文字起こしモジュール（録音しながら WebSocket で送るエンジン）

録音中の 16kHz 音声を逐次送り、途中経過と確定のテキストを受け取る。離した瞬間にほぼ確定して
いるので、録音後にまとめて送る方式より速い。対応エンジンは Soniox と OpenAI ライブ。

アプリ全体がスレッドで動く設計（asyncio 不使用）なので、websockets の同期クライアントを使う:

- send(): 録音コールバックのスレッドから呼ばれる。キューに積むだけで通信はしない
- 送信スレッド: キューの音声をエンジンの形式に直して送る（通信はここに集める）
- 受信スレッド: 接続 → 途中経過・確定の受信 → 終わりの検出
- start() / finish(): アプリのワーカースレッドから呼ばれる

録音を失わないための決まり:
途中で失敗したら finish() は空文字を返し、呼び出し側が手元の録音全体を送り直す
（OpenAI ライブは REST、Soniox は SonioxTranscriber が新しい接続へ流し直す）。
失敗までに取れていた文字は partial_text に残すので、送り直しも失敗したときの最後の手段に使える。
"""

import base64
import hashlib
import json
import queue
import threading
import time
from typing import Callable, Dict, List, Optional

import httpx
import numpy as np
import numpy.typing as npt

from . import engines
from .api_transcriber import TranscriptionError
from .text_utils import strip_cjk_spaces
from ..config.constants import SAMPLE_RATE
from ..utils.logger import get_logger

logger = get_logger(__name__)

# 接続確立のタイムアウト（秒）
_OPEN_TIMEOUT: float = 5.0
# finish() で確定を待つ最大時間（秒）。接続が固まったときの保険
_FINISH_TIMEOUT: float = 3.0
# 受信ループが「閉じられたか」を見直す間隔（秒）
_RECV_POLL_SEC: float = 0.5
# finish() が接続確立前に呼ばれたとき、接続を待つ猶予（秒）。
# 接続にかかる時間より短いキータップでも取りこぼさないため
_CONNECT_GRACE_SEC: float = 1.0

# 送信キューの上限（チャンク数）。回線が遅く送信が録音に追いつかなくなったら、音声を黙って
# 捨てずに失敗として扱い、録音全体の送り直しへ移す。1 チャンク数十 ms 換算で十数秒ぶん
_SEND_QUEUE_MAX: int = 512

# 送信キューに積む「終わりの合図を送れ」マーカー（finish が音声の後ろに積む）
_END_OF_AUDIO = object()

# Soniox の接続先（リージョン別）。キーはリージョンごとに発行されるので、合う方へつなぐ
_SONIOX_WS_URLS: Dict[str, str] = {
    "us": "wss://stt-rt.soniox.com/transcribe-websocket",
    "jp": "wss://stt-rt.jp.soniox.com/transcribe-websocket",
}
_SONIOX_PROBE_URLS: Dict[str, str] = {
    "jp": "https://api.jp.soniox.com/v1/models",
    "us": "https://api.soniox.com/v1/models",
}
# リージョン判定の 1 回あたりの待ち上限（秒）
_SONIOX_PROBE_TIMEOUT: float = 2.0
# Soniox へ送る末尾の無音（200ms）。語尾が切れて認識されないのを防ぐ
_SONIOX_TAIL_SILENCE: bytes = bytes(int(SAMPLE_RATE * 0.2) * 2)
# 録音を流し直すときの 1 回の量（100ms）と、接続を待つ上限（秒。判定 + 接続の合計を見込む）
_SONIOX_RESEND_CHUNK: int = SAMPLE_RATE // 10
_SONIOX_RESEND_CONNECT_GRACE: float = _SONIOX_PROBE_TIMEOUT + _OPEN_TIMEOUT + 1.0

# OpenAI ライブの接続先と、受け付ける音声のサンプリングレート
_OPENAI_LIVE_URL: str = "wss://api.openai.com/v1/realtime?intent=transcription"
_OPENAI_LIVE_RATE: int = 24000

# キーの指紋 → 判定済みの Soniox リージョン（アプリが動いている間だけ覚える）
_soniox_regions: Dict[str, str] = {}
_soniox_regions_lock = threading.Lock()


def _float32_to_pcm16le(samples: npt.NDArray[np.float32]) -> bytes:
    """
    float32[-1,1] のモノラル音声を 16bit リトルエンディアン PCM のバイト列に変換する。

    範囲外のサンプルは切り詰める（そのまま int16 にすると折り返して轟音になるため）。
    """
    clipped = np.clip(samples, -1.0, 1.0)
    return (clipped * 32767).astype("<i2").tobytes()


def _connect_ws(url: str, headers: Optional[dict] = None):
    """
    WebSocket に接続して返す（テストはこの関数を偽物に差し替える）。

    Args:
        url: 接続先
        headers: 追加の HTTP ヘッダー

    Returns:
        send / recv / close を持つ接続
    """
    # websockets は使うときに読む（未導入の環境でもアプリの起動を止めないため）
    from websockets.sync.client import connect
    return connect(url, additional_headers=headers or {}, open_timeout=_OPEN_TIMEOUT)


def _probe_status(url: str, key: str) -> int:
    """
    Soniox のリージョン判定用に GET して HTTP ステータスを返す（テストは偽物に差し替える）。

    Args:
        url: 判定用の URL
        key: API キー

    Returns:
        HTTP ステータス。通信できなければ 0
    """
    try:
        resp = httpx.get(
            url, headers={"Authorization": f"Bearer {key}"}, timeout=_SONIOX_PROBE_TIMEOUT
        )
        return resp.status_code
    except Exception:
        return 0


def soniox_region(key: str) -> Optional[str]:
    """
    その API キーが使える Soniox のリージョンを返す。

    キーはリージョン（日本 / 米国）ごとに別で、違う方へつなぐと認証エラーになる。利用者に
    リージョンを選ばせる代わりに、両方へ同時に問い合わせて通った方を使う。結果はキーごとに
    覚えるので、問い合わせるのは最初の 1 回だけ。

    Args:
        key: API キー

    Returns:
        "jp" か "us"。どちらも通らなければ None（通信できないだけかもしれないので覚えない）
    """
    # キーそのものを辞書のキーにして持ち回らないよう、指紋にする
    fingerprint = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    with _soniox_regions_lock:
        known = _soniox_regions.get(fingerprint)
    if known:
        return known

    results: Dict[str, int] = {}

    def probe(region: str) -> None:
        results[region] = _probe_status(_SONIOX_PROBE_URLS[region], key)

    threads = [threading.Thread(target=probe, args=(r,), daemon=True) for r in _SONIOX_PROBE_URLS]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    region = next((r for r in ("jp", "us") if results.get(r) == 200), None)
    if region:
        with _soniox_regions_lock:
            _soniox_regions[fingerprint] = region
        logger.info(f"Soniox のリージョンを判定しました: {region}")
    return region


class StreamingTranscriber:
    """
    WebSocket による逐次文字起こしセッションの共通部分（1 録音 = 1 インスタンス）。

    エンジンごとの違い（接続先・最初に送る設定・音声の形式・終わりの合図・応答の読み方）は
    サブクラスが決める。キュー・スレッド・finish の待ち方・失敗の扱いはここにまとめる。

    Attributes:
        on_interim: 全文（確定 + 途中経過）が変わるたびに呼ぶコールバック。HUD 用。
                    受信スレッドから呼ばれるので、UI へ反映する側はメインスレッドへ渡すこと。
        failure_reason: 失敗したときの短い理由（通知にそのまま出せる日本語。無ければ空）
    """

    engine_id: str = ""

    def __init__(
        self,
        model: str,
        language: str = "ja",
        prompt: str = "",
        on_interim: Optional[Callable[[str], None]] = None,
        queue_max: int = _SEND_QUEUE_MAX,
    ) -> None:
        """
        セッションを初期化する（ネットワークアクセスはしない）。

        Args:
            model: モデル名
            language: 言語コード
            prompt: 認識のヒント（固有名詞など）
            on_interim: 途中経過の更新コールバック
            queue_max: 送信キューの上限（0 で無制限。録音全体を流し直すときに使う）
        """
        self.model = model
        self.language = language
        self.prompt = prompt
        self.on_interim = on_interim
        self.failure_reason: str = ""

        self._lock = threading.Lock()
        self._ws = None                       # 接続確立後に入る
        self._finals: List[str] = []          # 確定した断片
        self._interim: str = ""               # 直近の途中経過
        # 接続前に届いた音声もここに積まれ、接続後に送信スレッドが順に送る
        self._send_q: "queue.Queue" = queue.Queue(maxsize=queue_max)
        self._cancelled = False               # 接続前に finish / cancel された
        self._closed = False                  # こちらから閉じた（受信・送信ループを止める）
        self._failed = False                  # 途中で失敗した（録音全体の送り直しへ）
        self._completed = False               # サーバーが最後まで確定した
        self._end_sent = False                # 終わりの合図を送った
        self._sent_samples = 0                # サーバーへ送り終えた音声のサンプル数（料金の記録用）
        self._done_event = threading.Event()  # 確定・切断・失敗の通知
        self._thread: Optional[threading.Thread] = None
        self._sender: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    # エンジンごとに決める部分
    # ------------------------------------------------------------------

    def _open(self, key: str):
        """接続して WebSocket を返す。"""
        raise NotImplementedError

    def _opening_frames(self, key: str) -> list:
        """接続直後、音声より先に送るフレーム。"""
        return []

    def _encode(self, samples: npt.NDArray[np.float32]) -> list:
        """音声チャンク 1 つを、送るフレームの並びに直す。"""
        raise NotImplementedError

    def _closing_frames(self) -> list:
        """音声を送り終えたときに送るフレーム（残りを確定させる合図）。"""
        raise NotImplementedError

    def _handle(self, msg: dict) -> None:
        """応答 1 件を読んで状態を更新する。"""
        raise NotImplementedError

    # ------------------------------------------------------------------
    # 呼び出し側が使う部分
    # ------------------------------------------------------------------

    @property
    def _name(self) -> str:
        """ログ・通知の主語に使うエンジン名。"""
        return engines.engine(self.engine_id).name

    @property
    def sent_seconds(self) -> float:
        """サーバーへ送り終えた音声の長さ（秒）。料金の記録に使う（接続できなかった回は 0）。"""
        return self._sent_samples / SAMPLE_RATE

    @property
    def failed(self) -> bool:
        """途中で失敗したか（True なら finish() は空文字を返している）。"""
        return self._failed

    @property
    def partial_text(self) -> str:
        """いままでに取れている文字（失敗していても読める）。"""
        return strip_cjk_spaces(self._current_text()).strip()

    def start(self) -> bool:
        """
        受信スレッドを起動する（すぐ返る。接続は受信スレッド側で行う）。

        Returns:
            開始できれば True。キー未設定・websockets 未導入なら False
            （呼び出し側は録音後にまとめて送る経路へ回る）
        """
        key = engines.api_key(self.engine_id)
        if not key:
            logger.warning(f"{self._name} の API キーが未設定のためストリーミングを開始できません")
            return False
        try:
            from websockets.sync.client import connect  # noqa: F401
        except Exception as e:
            logger.warning(f"websockets が利用できないためストリーミングを無効化します: {e}")
            return False

        self._thread = threading.Thread(
            target=self._run, args=(key,), daemon=True, name=f"{self.engine_id}-stream"
        )
        self._thread.start()
        return True

    def send(self, samples: npt.NDArray[np.float32]) -> None:
        """
        16kHz モノラルの音声チャンクを送信キューへ積む（録音スレッドから呼ばれる）。

        通信もロック取得もしないので、回線が遅くても録音コールバックはすぐ返る。
        キューがあふれたら音声を黙って捨てず、失敗として記録する。
        """
        if samples is None or len(samples) == 0:
            return
        if self._cancelled or self._closed or self._failed:
            return
        try:
            # 録音側がバッファを使い回しても壊れないよう複製して積む
            self._send_q.put_nowait(np.array(samples, dtype=np.float32))
        except queue.Full:
            logger.warning(f"{self._name}: 送信キューが上限に達しました。録音全体を送り直します")
            self._mark_failed()

    def finish(
        self, timeout: float = _FINISH_TIMEOUT, connect_grace: float = _CONNECT_GRACE_SEC
    ) -> str:
        """
        送信を打ち切り、確定テキストを返す（ホットキーを離したときに呼ぶ）。

        終わりの合図を送り、サーバーが残りを確定するのを最大 timeout 秒待つ。
        ワーカースレッドから呼ばれるので待ってよい（UI・キー監視は止めない）。

        Args:
            timeout: 確定を待つ上限（秒）
            connect_grace: まだ接続できていないときに接続を待つ上限（秒）

        Returns:
            確定テキスト。途中で失敗した・接続できなかったときは空文字
            （待ち時間切れは失敗ではないので、届いているぶんを返す）
        """
        with self._lock:
            ws = self._ws

        # 接続前に呼ばれたら少しだけ接続を待つ（短い発話で毎回送り直しになるのを防ぐ）
        if ws is None:
            grace_end = time.monotonic() + connect_grace
            while time.monotonic() < grace_end and not self._done_event.is_set():
                with self._lock:
                    ws = self._ws
                    closed = self._closed
                if ws is not None or closed:
                    break
                time.sleep(0.02)

        with self._lock:
            ws = self._ws
            if ws is None:
                # 猶予内に接続できなかった → 受信スレッドに後始末させる
                self._cancelled = True

        if ws is not None:
            # 終わりの合図はキュー経由で積む（残っている音声の後に送られ、順序が保たれる）
            try:
                self._send_q.put_nowait(_END_OF_AUDIO)
            except queue.Full:
                self._mark_failed()

        self._done_event.wait(timeout)
        self._shutdown_socket()
        if self._failed:
            return ""
        return self.partial_text

    def cancel(self) -> None:
        """結果を使わずに接続を破棄する（録音を捨てるときなど）。"""
        with self._lock:
            self._cancelled = True
        self._shutdown_socket()
        self._done_event.set()

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _mark_failed(self, reason: str = "") -> None:
        """
        失敗として確定し、finish() の待ちを解く。

        Args:
            reason: 通知に出せる短い理由（最初に記録されたものを残す）
        """
        with self._lock:
            if self._failed:
                return
            self._failed = True
            self._closed = True
            self.failure_reason = reason
        self._done_event.set()

    def _mark_completed(self) -> None:
        """サーバーが最後まで確定したことを記録し、finish() の待ちを解く。"""
        with self._lock:
            self._completed = True
        self._done_event.set()

    def _shutdown_socket(self) -> None:
        """受信・送信ループを止めて WebSocket を閉じる（何度呼んでもよい）。"""
        with self._lock:
            self._closed = True
            ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception as e:
                logger.debug(f"WebSocket close エラー（無視）: {type(e).__name__}")

    def _current_text(self) -> str:
        """確定 + 途中経過をつないだ現在の全文。"""
        with self._lock:
            return "".join(self._finals) + self._interim

    def _notify_interim(self) -> None:
        """HUD へ現在の全文を渡す。"""
        cb = self.on_interim
        if cb is None:
            return
        try:
            cb(self.partial_text)
        except Exception:
            pass  # HUD 側の例外で文字起こしを止めない

    def _run(self, key: str) -> None:
        """接続 → 送信スレッド起動 → 受信ループ（受信スレッド上）。"""
        t_start = time.monotonic()
        try:
            ws = self._open(key)
        except Exception as e:
            # 例外の本文には接続先の応答が入りうるので、ログには種類だけ残す
            logger.warning(f"{self._name} ストリーミング接続に失敗: {type(e).__name__}")
            self._mark_failed(f"{self._name} に接続できませんでした（ネットワークを確認してください）")
            return
        logger.info(
            f"{self._name} ストリーミング開始 (model={self.model}): "
            f"接続 {(time.monotonic() - t_start) * 1000:.0f}ms"
        )

        with self._lock:
            close_now = self._cancelled or self._closed
            if not close_now:
                self._ws = ws
        if close_now:
            try:
                ws.close()
            except Exception:
                pass
            self._done_event.set()
            return

        self._sender = threading.Thread(
            target=self._sender_loop, args=(ws, key), daemon=True, name=f"{self.engine_id}-send"
        )
        self._sender.start()

        self._receive_loop(ws)

        # こちらから閉じたのでも、最後まで確定したのでもないのに受信が終わった＝途中で切れた。
        # 前半しか取れていないので失敗にして、録音全体を送り直させる
        with self._lock:
            dropped = not self._completed and not self._closed
        if dropped:
            logger.warning(f"{self._name}: 確定前に接続が切れました")
            self._mark_failed(f"{self._name} に接続できませんでした（ネットワークを確認してください）")
        self._done_event.set()

    def _sender_loop(self, ws, key: str) -> None:
        """
        キューの音声を WebSocket へ送る専用スレッド。

        音声の形式変換もここで行う（OpenAI ライブの変換は前のチャンクの続きを覚えているので、
        1 つのスレッドに閉じておく）。送信に失敗したら失敗として記録する。
        """
        try:
            for frame in self._opening_frames(key):
                ws.send(frame)
        except Exception as e:
            logger.debug(f"{self._name}: 設定の送信エラー: {type(e).__name__}")
            self._mark_failed()
            return

        while True:
            if self._closed or self._failed:
                return
            try:
                item = self._send_q.get(timeout=_RECV_POLL_SEC)
            except queue.Empty:
                continue  # 閉じられたかを見直すために回り続ける
            try:
                if item is _END_OF_AUDIO:
                    with self._lock:
                        self._end_sent = True
                    for frame in self._closing_frames():
                        ws.send(frame)
                    return  # この後は送るものが無い（受信ループが確定を待つ）
                for frame in self._encode(item):
                    ws.send(frame)
                # 送信スレッドだけが書き、読むのは finish() の後なのでロックは要らない
                self._sent_samples += len(item)
            except Exception as e:
                logger.debug(f"{self._name}: 送信エラー: {type(e).__name__}")
                self._mark_failed()
                return

    def _receive_loop(self, ws) -> None:
        """応答を受信し続けるループ（切断・確定・こちらからの close で抜ける）。"""
        while True:
            with self._lock:
                if self._closed or self._completed:
                    return
            try:
                message = ws.recv(timeout=_RECV_POLL_SEC)
            except TimeoutError:
                continue
            except Exception:
                return  # 切断（正常な終わりを含む）
            try:
                if isinstance(message, (bytes, bytearray)):
                    message = message.decode("utf-8", errors="ignore")
                self._handle(json.loads(message))
            except Exception as e:
                logger.debug(f"{self._name}: 応答の処理でエラー（無視）: {type(e).__name__}")


class SonioxStreamer(StreamingTranscriber):
    """
    Soniox のリアルタイム文字起こし。

    最初のテキストフレームで設定（キー・モデル・音声形式）を送り、以降は 16bit PCM を
    そのままバイナリで送る。応答は「トークン」の並びで、確定したものは二度と変わらない。
    """

    engine_id = "soniox"

    def _open(self, key: str):
        """キーに合うリージョンへ接続する（判定できなければ米国へ）。"""
        region = soniox_region(key) or "us"
        return _connect_ws(_SONIOX_WS_URLS[region])

    def _opening_frames(self, key: str) -> list:
        """設定フレームを作る（Soniox はキーをヘッダーではなくここで渡す）。"""
        config = {
            "api_key": key,
            "model": self.model,
            "audio_format": "pcm_s16le",
            "sample_rate": SAMPLE_RATE,
            "num_channels": 1,
        }
        if self.language:
            config["language_hints"] = [self.language]
        if self.prompt.strip():
            config["context"] = {"text": self.prompt.strip()}
        return [json.dumps(config)]

    def _encode(self, samples: npt.NDArray[np.float32]) -> list:
        """16bit PCM のバイナリ 1 フレームにする。"""
        return [_float32_to_pcm16le(samples)]

    def _closing_frames(self) -> list:
        """
        末尾の無音 → finalize（残りをいますぐ確定）→ 空のフレーム（音声の終わり）。

        finalize を送らないと、サーバーが発話の終わりを自分で判断するまで確定が遅れる。
        """
        return [_SONIOX_TAIL_SILENCE, json.dumps({"type": "finalize"}), b""]

    def _handle(self, msg: dict) -> None:
        """
        応答 1 件を読む。

        確定トークンは届いた順につなぎ、未確定は毎回まるごと置き換える。
        <fin>（finalize の完了）と <end>（発話の終わり）は目印なので本文には入れない。
        """
        if msg.get("error_code") is not None or msg.get("error_type") is not None:
            code = msg.get("error_code")
            # エラーの本文は通知に出さない（ログにも種類と番号だけ）
            logger.warning(f"Soniox エラー: code={code} type={msg.get('error_type')}")
            self._mark_failed(_soniox_reason(code))
            return

        saw_fin = False
        interim_parts: List[str] = []
        with self._lock:
            for token in msg.get("tokens") or []:
                text = token.get("text") or ""
                if text == "<fin>":
                    saw_fin = True
                    continue
                if text == "<end>":
                    continue
                if token.get("is_final"):
                    self._finals.append(text)
                else:
                    interim_parts.append(text)
            self._interim = "".join(interim_parts)
            done = bool(msg.get("finished")) or (saw_fin and self._end_sent)
        self._notify_interim()
        if done:
            self._mark_completed()


def _soniox_reason(code) -> str:
    """Soniox のエラー番号を、通知に出す短い理由に直す。"""
    if code in (401, 403):
        return "Soniox の API キーが無効です（設定 › API キー を確認してください）"
    if code == 402:
        return "Soniox の残高・利用上限が尽きています"
    if code == 429:
        return "Soniox の同時接続・回数の上限に達しました"
    return "Soniox に接続できませんでした（ネットワークを確認してください）"


class _Resampler16to24:
    """
    16kHz → 24kHz の線形補間（チャンクをまたいで続きから計算する）。

    チャンクごとに独立して補間すると境界で波形が途切れ、プチッという雑音になって認識が落ちる。
    前のチャンクの最後のサンプルと「次に出力する位置」を持ち越して、1 本の音として補間する。
    位置は 1/3 サンプル単位の整数で持つ（出力 1 個ごとに入力 2/3 個ぶん進むので誤差が溜まらない）。
    """

    def __init__(self) -> None:
        """最初のチャンク用の状態で初期化する。"""
        self._prev: Optional[np.float32] = None
        self._pos3: int = 0

    def process(self, samples: npt.NDArray[np.float32]) -> npt.NDArray[np.float32]:
        """
        チャンク 1 つを 24kHz に直す。

        Args:
            samples: 16kHz の音声

        Returns:
            24kHz の音声（長さはおよそ 1.5 倍）
        """
        if self._prev is None:
            src = samples
        else:
            src = np.concatenate(([self._prev], samples))
        last3 = (len(src) - 1) * 3  # このチャンクで補間できる最後の位置
        count = (last3 - self._pos3) // 2 + 1 if last3 >= self._pos3 else 0
        idx = self._pos3 + 2 * np.arange(count)
        left = idx // 3
        frac = (idx % 3) / 3.0
        right = np.minimum(left + 1, len(src) - 1)
        out = src[left] * (1.0 - frac) + src[right] * frac
        self._pos3 = self._pos3 + 2 * count - last3
        self._prev = src[-1]
        return out.astype(np.float32)


class OpenAILiveStreamer(StreamingTranscriber):
    """
    OpenAI のライブ文字起こし（Realtime API の transcription セッション）。

    音声は 24kHz の 16bit PCM を base64 にして JSON で送る。サーバー側の発話検出は切り、
    離したときの commit でだけ確定させる（押している間が 1 つの発話＝ホットキー操作と一致する）。
    """

    engine_id = "openai_live"

    def __init__(self, *args, **kwargs) -> None:
        """共通の初期化に加えて、音声変換の状態を用意する。"""
        super().__init__(*args, **kwargs)
        self._resampler = _Resampler16to24()

    def _open(self, key: str):
        """Bearer 認証で接続する。"""
        return _connect_ws(_OPENAI_LIVE_URL, {"Authorization": f"Bearer {key}"})

    def _opening_frames(self, key: str) -> list:
        """セッション設定（音声より先に届く必要がある）。"""
        return [json.dumps({
            "type": "session.update",
            "session": {
                "type": "transcription",
                "audio": {
                    "input": {
                        "format": {"type": "audio/pcm", "rate": _OPENAI_LIVE_RATE},
                        "transcription": {
                            "model": self.model,
                            "language": self.language or "ja",
                            "delay": "minimal",
                        },
                        # null＝サーバーの発話検出を使わず、commit したときだけ確定させる
                        "turn_detection": None,
                    }
                },
            },
        })]

    def _encode(self, samples: npt.NDArray[np.float32]) -> list:
        """24kHz に直し、base64 にして append イベントにする。"""
        pcm = _float32_to_pcm16le(self._resampler.process(samples))
        if not pcm:
            return []
        audio = base64.b64encode(pcm).decode("ascii")
        return [json.dumps({"type": "input_audio_buffer.append", "audio": audio})]

    def _closing_frames(self) -> list:
        """commit で残りを確定させる。"""
        return [json.dumps({"type": "input_audio_buffer.commit"})]

    def _handle(self, msg: dict) -> None:
        """
        応答 1 件を読む。

        delta は増分なので足していき、completed は全文なので置き換える（ここで終わり）。
        イベント名の前半は API の版で変わるため、末尾だけで見分ける。
        """
        kind = str(msg.get("type") or "")
        if kind == "error":
            # エラーの本文は通知に出さない（録音は REST で送り直す）
            logger.warning(f"OpenAI ライブ エラー: code={(msg.get('error') or {}).get('code')}")
            self._mark_failed()
            return
        if kind.endswith("transcription.delta"):
            with self._lock:
                self._interim += str(msg.get("delta") or "")
            self._notify_interim()
        elif kind.endswith("transcription.completed"):
            with self._lock:
                self._finals = [str(msg.get("transcript") or "")]
                self._interim = ""
            self._notify_interim()
            self._mark_completed()


_STREAMER_CLASSES = {cls.engine_id: cls for cls in (SonioxStreamer, OpenAILiveStreamer)}


def create_streamer(
    engine_id: str,
    model: str,
    language: str = "ja",
    prompt: str = "",
    on_interim: Optional[Callable[[str], None]] = None,
) -> StreamingTranscriber:
    """
    エンジン id からリアルタイム文字起こしのセッションを作る。

    Args:
        engine_id: 録音しながら送るエンジンの id（"soniox" / "openai_live"）
        model: モデル名
        language: 言語コード
        prompt: 認識のヒント
        on_interim: 途中経過の更新コールバック

    Returns:
        start / send / finish / cancel を持つセッション
    """
    return _STREAMER_CLASSES[engine_id](model, language, prompt, on_interim)


class SonioxTranscriber:
    """
    録音全体を Soniox へ流し直して文字起こしする（ライブで文字が取れなかったときの退避）。

    Soniox には録音後にまとめて送る速い API が無いので、新しい WebSocket 接続へ録音を
    一気に流し込む。api_transcriber のクライアントと同じ使い方ができるようにしてある。

    Attributes:
        allows_split: 長い録音を分けて並列に送らない（同時接続の上限に当たるため）
    """

    engine_id = "soniox"
    allows_split = False

    def __init__(self, model: str, language: str = "ja", prompt: str = "") -> None:
        """
        初期化する（ネットワークアクセスはしない）。

        Args:
            model: モデル名
            language: 言語コード
            prompt: 認識のヒント
        """
        self.model = model
        self.language = language
        self.prompt = prompt
        self.last_api_time: float = 0.0

    def is_available(self) -> bool:
        """API キーが設定されているかを返す。"""
        return bool(engines.api_key(self.engine_id))

    def prewarm(self) -> None:
        """リージョンの判定を先に済ませておく（最初の録音で接続が遅れないように）。"""
        key = engines.api_key(self.engine_id)
        if key:
            soniox_region(key)

    def transcribe(self, audio_data: npt.NDArray[np.float32]) -> str:
        """
        録音全体を新しい接続へ流して文字起こしする。

        Args:
            audio_data: 音声データ（16kHz・float32・モノラル）

        Returns:
            文字起こし結果。途中で失敗しても、取れたぶんがあればそれを返す

        Raises:
            TranscriptionError: キー未設定、または 1 文字も取れずに失敗したとき
        """
        if len(audio_data) == 0:
            return ""
        if not self.is_available():
            raise TranscriptionError(
                "Soniox の API キーが未設定です（設定 › API キー で入力してください）"
            )

        started = time.perf_counter()
        # 録音を一気に積むので、キューの上限は外す（上限があると長い録音で必ずあふれる）
        session = SonioxStreamer(self.model, self.language, self.prompt, queue_max=0)
        if not session.start():
            raise TranscriptionError("Soniox に接続できませんでした（ネットワークを確認してください）")
        for offset in range(0, len(audio_data), _SONIOX_RESEND_CHUNK):
            session.send(audio_data[offset:offset + _SONIOX_RESEND_CHUNK])

        # 実時間より速く処理されるので、長い録音でも待つのは音声の半分 + 3 秒まで（上限 15 秒）
        seconds = len(audio_data) / SAMPLE_RATE
        text = session.finish(
            timeout=min(15.0, 3.0 + seconds * 0.5),
            connect_grace=_SONIOX_RESEND_CONNECT_GRACE,
        ) or session.partial_text
        self.last_api_time = (time.perf_counter() - started) * 1000

        if not text and session.failed:
            raise TranscriptionError(
                session.failure_reason
                or "Soniox に接続できませんでした（ネットワークを確認してください）"
            )
        logger.info(f"Soniox 送り直し完了: {self.last_api_time:.0f}ms, {len(text)} 文字")
        return text

    def close(self) -> None:
        """保持している接続は無いので何もしない（他のクライアントと呼び方をそろえるため）。"""
