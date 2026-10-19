"""
ライブ（WebSocket）文字起こしモジュール — Soniox / OpenAI ライブ（Mac 版 SonioxLiveTranscriber.swift /
OpenAILiveTranscriber.swift の移植）

録音中の 16kHz PCM を WebSocket で逐次送り、離鍵時に確定テキストを受け取る。
Deepgram 版（streaming_transcriber.StreamingTranscriber）と同じ使い方（start / send / finish / cancel）で、
app.py から同じ扱いで差し替えられる。

スレッド構成（アプリ全体がスレッドベースなので websockets の同期クライアントを使う）:
- send(): audio コールバックスレッドから呼ばれる。変換して固定上限キューへ積むだけ（ロック・通信なし）
- _run(): 接続 → 設定フレーム送信 → sender スレッド起動 → 受信ループ
- finish(): ワーカースレッドから呼ばれる。終端を積んで確定（または切断・タイムアウト）まで待つ

Soniox は終わり方（正常終了／エラー・切断・タイムアウト）を区別する（ending）。
正常終了で空なら「本当に無言」なので REST へ回さない。途中で壊れたときは取れた分を返し、
呼び出し側が録音全体の再送（SonioxTranscriber）で救う。OpenAI ライブは Mac と同じく区別しない（unknown）。
"""

import base64
import json
import queue
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, List, Optional, Tuple

import numpy as np
import numpy.typing as npt

from . import soniox_region
from .text_utils import strip_cjk_spaces
from ..utils import secrets
from ..utils.logger import get_logger

logger = get_logger(__name__)

# 接続確立のタイムアウト（秒）
_OPEN_TIMEOUT: float = 5.0
# 受信ループのポーリング間隔（秒）。closed フラグを定期確認するため
_RECV_POLL_SEC: float = 0.5
# 離鍵後に確定を待つ上限（秒）。Mac と同じ
DEFAULT_FINISH_TIMEOUT: float = 3.0
# 送信キューの固定上限（チャンク数）。溢れたら黙って捨てず失敗として扱う（Deepgram 版と同じ考え）
_SEND_QUEUE_MAX: int = 2048
# 送信キューに積む「終端を送れ」マーカー
_END = object()


def open_websocket(url: str, headers: Optional[dict] = None, open_timeout: float = _OPEN_TIMEOUT):
    """WebSocket を開いて接続を返す（websockets の同期クライアント）。

    websockets 16 以降は connect() を with 文なしで使うと非推奨の警告が出る（将来は動かなくなる）。
    with を使わず直接つなぐ正式な書き方は legacy=True なので、それを渡す。
    legacy を知らない古い版（13〜15）は TypeError になるので、そのときは従来どおり呼ぶ。
    """
    from websockets.sync.client import connect

    kwargs = {"additional_headers": headers or None, "open_timeout": open_timeout}
    try:
        return connect(url, legacy=True, **kwargs)
    except TypeError:
        return connect(url, **kwargs)


# ----------------------------------------------------------------------
# 終わり方
# ----------------------------------------------------------------------

@dataclass(frozen=True)
class LiveEnding:
    """ライブ文字起こしの終わり方（Mac の LiveEnding / LiveFailure）。

    Attributes:
        kind: "unknown"（区別しない）/ "completed"（最後まで処理し終えた）/ "failed"（途中で失敗）
        failure: 失敗理由 "error" / "disconnect" / "timeout"（失敗時のみ）
        code: サーバーのエラーコード（文字列にそろえる。本文はキー等が混ざりうるので持たない）
        error_type: サーバーのエラー種別
    """

    kind: str = "unknown"
    failure: Optional[str] = None
    code: Optional[str] = None
    error_type: Optional[str] = None

    @property
    def is_completed(self) -> bool:
        return self.kind == "completed"

    @property
    def is_failed(self) -> bool:
        return self.kind == "failed"

    @property
    def log_label(self) -> str:
        """行動ログ用の短い表記（障害時にログだけで終わり方を追えるようにする）。"""
        if self.kind == "failed":
            if self.failure == "error":
                return f"failed:error(code={self.code or '-'} type={self.error_type or '-'})"
            return f"failed:{self.failure}"
        return self.kind


ENDING_UNKNOWN = LiveEnding()
ENDING_COMPLETED = LiveEnding("completed")


def failed(failure: str, code: Optional[str] = None, error_type: Optional[str] = None) -> LiveEnding:
    """失敗の終わり方を作る。"""
    return LiveEnding("failed", failure, code, error_type)


def soniox_failure_message(ending: LiveEnding) -> str:
    """Soniox の失敗を、次に何をすればよいか分かる文言へ写す（Mac の sonioxFailureMessage と同じ）。"""
    if ending.failure == "error":
        if ending.code in ("401", "403"):
            return "Soniox の API キーが無効です（設定 › API キー を確認してください）"
        if ending.code == "402":
            return "Soniox の残高・利用上限が尽きています"
        if ending.code == "429":
            return "Soniox の同時接続・回数の上限に達しました"
    return "Soniox に接続できませんでした（ネットワークを確認してください）"


def is_invalid_key_failure(ending: LiveEnding) -> bool:
    """Soniox の失敗がキーの無効（401/403）か。設定 › API キー へ案内するかの判定に使う。"""
    return ending.failure == "error" and ending.code in ("401", "403")


def should_replay_after_live_failure(backend: str, ending: LiveEnding, streamed_is_empty: bool) -> bool:
    """ライブ（Soniox）が途中で切れて前半しか取れていないとき、録音全体を再送して救うか。

    空のときは従来どおり REST 経路が再送するのでここでは扱わない。タイムアウトは確定待ちが
    長引いただけで後半も届いている見込みが高いので対象外（Mac と同じ）。
    """
    if backend != "soniox" or streamed_is_empty or not ending.is_failed:
        return False
    return ending.failure != "timeout"


# ----------------------------------------------------------------------
# Soniox の応答の組み立て（純ロジック）
# ----------------------------------------------------------------------

class SonioxTranscript:
    """Soniox の応答を確定文・途中文へ組み立てる純ロジック（通信と切り離してテストする）。

    tokens は is_final のものを到着順に足し、非確定のものは毎回置き換える。
    """

    FIN_TOKEN = "<fin>"
    CONTROL_TOKENS = frozenset({FIN_TOKEN, "<end>"})

    UPDATED = "updated"
    FINALIZED = "finalized"
    FINISHED = "finished"
    ERROR = "error"
    IGNORED = "ignored"

    def __init__(self) -> None:
        self.final_text = ""
        self.interim_text = ""

    @property
    def full_text(self) -> str:
        """いま見せるべき全文（確定 + 途中）。"""
        return self.final_text + self.interim_text

    def apply(self, message: Any) -> Tuple[str, Optional[str], Optional[str]]:
        """応答 JSON 1 件を取り込む。

        Returns:
            (結果, エラーコード, エラー種別)。結果は UPDATED / FINALIZED / FINISHED / ERROR / IGNORED
        """
        if isinstance(message, (bytes, bytearray)):
            message = message.decode("utf-8", errors="replace")
        try:
            obj = json.loads(message) if isinstance(message, str) else message
        except Exception:
            return self.IGNORED, None, None
        if not isinstance(obj, dict):
            return self.IGNORED, None, None
        # "error_code": null のようにキーだけあるものはエラー扱いにしない
        code = obj.get("error_code")
        etype = obj.get("error_type")
        if code is not None or etype is not None:
            # コードは数値・文字列のどちらで来ても文字列にそろえる（401 も "401" も同じに扱う）
            return (
                self.ERROR,
                None if code is None else str(code),
                None if etype is None else str(etype),
            )
        before = (self.final_text, self.interim_text)
        saw_fin = False
        tokens = obj.get("tokens")
        if isinstance(tokens, list):
            interim = ""
            for token in tokens:
                if not isinstance(token, dict):
                    continue
                text = token.get("text")
                if not isinstance(text, str):
                    continue
                if text == self.FIN_TOKEN:
                    saw_fin = True
                if text in self.CONTROL_TOKENS:
                    continue
                if token.get("is_final") is True:
                    self.final_text += text
                else:
                    interim += text
            self.interim_text = interim
        # 最後の応答にも tokens が載ることがあるので、取り込んでから終端を判定する
        if obj.get("finished") is True:
            return self.FINISHED, None, None
        if saw_fin:
            return self.FINALIZED, None, None
        if (self.final_text, self.interim_text) == before:
            return self.IGNORED, None, None
        return self.UPDATED, None, None


# ----------------------------------------------------------------------
# 音声の変換
# ----------------------------------------------------------------------

def pcm16(samples: npt.NDArray[np.float32]) -> bytes:
    """float32 [-1, 1] → Int16 LE（pcm_s16le）。範囲外はクリップして轟音化を防ぐ。"""
    clipped = np.clip(np.asarray(samples, dtype=np.float32), -1.0, 1.0)
    return (clipped * 32767).astype("<i2").tobytes()


class Upsampler16To24:
    """16kHz → 24kHz の線形補間リサンプラ（OpenAI Realtime は 24kHz 以上しか受けない）。

    前チャンク末尾のサンプルと出力位置を持ち越し、チャンク境界に段差（クリック音になり
    認識精度を落とす）を作らない。audio スレッドからだけ呼ぶ（ロック不要）。Mac の upsample と同じ計算。
    """

    STEP = 16000 / 24000  # 入力 1 サンプルあたり 2/3 進む

    def __init__(self) -> None:
        # 出力位置（[前チャンク末尾] + 今回のチャンク の連結配列上の座標）
        self._pos = 1.0
        self._prev = 0.0

    def process(self, samples: npt.NDArray[np.float32]) -> npt.NDArray[np.float32]:
        samples = np.asarray(samples, dtype=np.float32)
        if samples.size == 0:
            return samples
        buf = np.concatenate(([np.float32(self._prev)], samples))
        limit = float(buf.size - 1)
        count = int(np.ceil((limit - self._pos) / self.STEP)) if self._pos < limit else 0
        positions = self._pos + np.arange(count, dtype=np.float64) * self.STEP
        # 浮動小数の誤差で limit ちょうどに届いた点は次回へ回す（右端は次回の左端として残す）
        positions = positions[positions < limit]
        idx = positions.astype(np.int64)
        frac = (positions - idx).astype(np.float32)
        out = buf[idx] * (1 - frac) + buf[idx + 1] * frac
        next_pos = (positions[-1] + self.STEP) if positions.size else self._pos
        # 次チャンクの連結配列（index 0 = 今回の末尾）での座標に読み替える
        self._pos = next_pos - samples.size
        self._prev = float(samples[-1])
        return out.astype(np.float32)


# ----------------------------------------------------------------------
# 共通の土台
# ----------------------------------------------------------------------

class LiveSessionBase:
    """ライブ文字起こし 1 セッション（1 録音 = 1 インスタンス）の共通部分。

    サブクラスが決めるもの:
        PROVIDER: ログ用の名前
        _connect_target(key) -> (url, headers): 接続先（Soniox はここでリージョン判定が走りうる）
        _opening_messages(key) -> list: 接続直後に音声より先に送るフレーム
        _encode(samples) -> フレーム: 音声チャンクの送り方
        _end_messages() -> list: 離鍵時の終端フレーム
        _handle(message): 応答 1 件の処理（確定・失敗は _resolve で知らせる）
        _text() -> str: 現在の全文

    Attributes:
        on_interim: 全文（確定 + 途中）の更新通知。受信スレッドから呼ばれる（UI 側でメインへホップすること）
    """

    PROVIDER = "live"
    KEY_ITEM = ""

    def __init__(self, on_interim: Optional[Callable[[str], None]] = None) -> None:
        self.on_interim = on_interim
        self._lock = threading.Lock()
        self._send_q: "queue.Queue" = queue.Queue(maxsize=_SEND_QUEUE_MAX)
        self._ws = None
        self._cancelled = False
        self._closed = False
        self._close_requested = False
        self._audio_queued = False
        self._done = threading.Event()
        self._ending: LiveEnding = ENDING_UNKNOWN
        self._sent_bytes = 0
        self._thread: Optional[threading.Thread] = None
        self._created_at = time.monotonic()
        self._first_result_logged = False
        # 1 セッション分の利用量の通知先（API 料金の記録用。秒数だけ渡す・本文は渡さない）
        self.on_usage: Optional[Callable[[float], None]] = None

    # --- 外から使う口 ---

    @property
    def ending(self) -> LiveEnding:
        """finish() がどう終わったか（finish() の後に読む）。"""
        with self._lock:
            return self._ending

    def start(self, api_key: Optional[str] = None) -> bool:
        """接続スレッドを起動する（即座に返る）。

        Args:
            api_key: キーを明示する（録音全体の再送・テスト用）。None なら設定から読む

        Returns:
            キー未設定・websockets 未導入なら False（呼び出し側は REST 経路へ回り、
            そこでキー未設定のエラーが出る）
        """
        # 配布版（製品版）にはライブ型の新エンジンを提供しない（personal 限定・Mac と同じ）
        if secrets.is_dist_build():
            logger.warning(f"{self.PROVIDER}: 配布版では利用できません")
            return False
        key = api_key if api_key is not None else secrets.api_key(self.KEY_ITEM)
        if not key:
            logger.warning(f"{self.PROVIDER}: API キーが未設定のためライブ接続を開始しません")
            return False
        try:
            from websockets.sync.client import connect  # noqa: F401
        except Exception as e:
            logger.warning(f"websockets が利用できないためライブ接続を無効化します: {e}")
            return False
        self._thread = threading.Thread(
            target=self._run, args=(key,), daemon=True, name=f"{self.PROVIDER}Stream"
        )
        self._thread.start()
        return True

    def send(self, samples: npt.NDArray[np.float32]) -> None:
        """16kHz モノラル Float32 チャンクを送信キューへ積む（audio スレッドから呼ばれる）。"""
        if samples is None or len(samples) == 0:
            return
        if self._cancelled or self._closed or self._close_requested:
            return
        frame = self._encode(samples)
        if frame is None:
            return
        try:
            self._send_q.put_nowait(frame)
            self._audio_queued = True
        except queue.Full:
            # 送信が音声生成に追いつかない。取りこぼしを隠さず失敗にして再送・REST に委ねる
            logger.warning(f"{self.PROVIDER}: 送信キューが上限に達しました")
            self._resolve(failed("disconnect"), "queue-full")

    def finish(self, timeout: float = DEFAULT_FINISH_TIMEOUT) -> str:
        """送信を打ち切り、確定テキストを返す（ホットキーを離したときに呼ぶ・ブロックする）。

        接続済みなら終端を送って確定を最大 timeout 秒待つ。未接続なら終端を積んでおき、
        接続後に sender が退避音声の後に送る。未接続かつ音声ゼロなら確定は来ないので即座に返す
        （接続していない＝サーバーが処理し終えた証拠は無いので、Soniox は失敗として返す）。
        """
        with self._lock:
            self._close_requested = True
            ws = self._ws
        if ws is None and not self._audio_queued:
            self._resolve(failed("disconnect"), "empty-noconnect")
        else:
            try:
                self._send_q.put(_END, timeout=0.5)
            except queue.Full:
                pass
            if not self._done.wait(timeout):
                self._resolve(failed("timeout"), "timeout")
        self._shutdown()
        # タイムアウトで確定が揃わなかった場合も、届いている途中文までは使う（取りこぼしを減らす）
        return strip_cjk_spaces(self._text()).strip()

    def cancel(self) -> None:
        """結果を使わずに接続を破棄する（録音破棄時など）。"""
        with self._lock:
            self._cancelled = True
        self._resolve(ENDING_UNKNOWN, "cancelled")
        self._shutdown()

    # --- サブクラスが実装する部分 ---

    def _connect_target(self, key: str) -> Tuple[str, dict]:
        raise NotImplementedError

    def _opening_messages(self, key: str) -> List[Any]:
        return []

    def _encode(self, samples: npt.NDArray[np.float32]) -> Any:
        raise NotImplementedError

    def _end_messages(self) -> List[Any]:
        return []

    def _handle(self, message: Any) -> None:
        raise NotImplementedError

    def _text(self) -> str:
        raise NotImplementedError

    def _usage_seconds(self, sent_bytes: int) -> float:
        """実際に送った音声バイト数から秒数を出す（API 料金の記録用）。"""
        return 0.0

    # --- 内部処理 ---

    def _notify_interim(self, text: str) -> None:
        """途中経過を HUD へ（受信スレッドから。UI 側の例外でセッションを止めない）。"""
        self._log_first_result_once()
        cb = self.on_interim
        if cb is not None:
            try:
                cb(strip_cjk_spaces(text))
            except Exception:
                pass

    def _log_first_result_once(self) -> None:
        """録音開始から「最初の文字が出るまで」の体感遅延を 1 度だけログ。"""
        if self._first_result_logged:
            return
        self._first_result_logged = True
        logger.info(
            f"{self.PROVIDER} 最初の文字まで {(time.monotonic() - self._created_at) * 1000:.0f}ms（録音開始から）"
        )

    def _resolve(self, ending: LiveEnding, reason: str) -> None:
        """finish() の待ちを一度だけ解く。終わり方は最初の解決だけを記録する。"""
        with self._lock:
            was_done = self._done.is_set()
            if not was_done:
                self._ending = ending
            sent = self._sent_bytes
        if was_done:
            return
        self._done.set()
        logger.info(f"{self.PROVIDER} finish 解決: {reason}（終わり方={ending.log_label}）")
        if sent > 0 and self.on_usage is not None:
            try:
                self.on_usage(self._usage_seconds(sent))
            except Exception:
                pass

    def _shutdown(self) -> None:
        """受信ループを止め、WebSocket を閉じる（多重呼び出し安全）。"""
        with self._lock:
            self._closed = True
            ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception as e:
                logger.debug(f"{self.PROVIDER}: WebSocket close エラー（無視）: {e}")

    def _send_frame(self, ws, frame: Any) -> None:
        """1 フレーム送る（音声なら送ったバイト数を数える）。"""
        ws.send(frame)
        if isinstance(frame, (bytes, bytearray)):
            with self._lock:
                self._sent_bytes += len(frame)
        else:
            extra = self._frame_audio_bytes(frame)
            if extra:
                with self._lock:
                    self._sent_bytes += extra

    def _frame_audio_bytes(self, frame: Any) -> int:
        """テキストフレームに載った音声のバイト数（OpenAI ライブは base64 で載せる）。"""
        return 0

    def _run(self, key: str) -> None:
        """接続 → 設定送信 → sender 起動 → 受信ループ（専用スレッド上）。"""
        t_start = time.monotonic()
        try:
            url, headers = self._connect_target(key)
            ws = open_websocket(url, headers)
        except Exception as e:
            # 接続失敗 → finish() を解決して救済（再送・REST）に委ねる。例外の本文にキーは載らない
            logger.warning(f"{self.PROVIDER}: 接続に失敗: {type(e).__name__}")
            self._resolve(failed("disconnect"), "connect-failed")
            return
        t_conn = time.monotonic()

        with self._lock:
            close_now = self._cancelled or self._closed
            if not close_now:
                self._ws = ws
        if close_now:
            try:
                ws.close()
            except Exception:
                pass
            self._resolve(ENDING_UNKNOWN, "cancelled")
            return

        try:
            # 設定は最初のフレームでなければならない（音声より先に届く必要がある）
            for message in self._opening_messages(key):
                ws.send(message)
        except Exception as e:
            logger.warning(f"{self.PROVIDER}: 設定の送信に失敗: {type(e).__name__}")
            self._resolve(failed("disconnect"), "config-send-failed")
            self._shutdown()
            return
        logger.info(f"{self.PROVIDER} 開始: WS接続 {(t_conn - t_start) * 1000:.0f}ms")

        threading.Thread(
            target=self._sender_loop, args=(ws,), daemon=True, name=f"{self.PROVIDER}Send"
        ).start()
        self._receive_loop(ws)

    def _sender_loop(self, ws) -> None:
        """送信キューの音声を順に送る（終端マーカーで終端フレームを送って終わる）。"""
        while True:
            with self._lock:
                if self._closed or self._cancelled:
                    return
            try:
                item = self._send_q.get(timeout=_RECV_POLL_SEC)
            except queue.Empty:
                continue
            try:
                if item is _END:
                    for message in self._end_messages():
                        self._send_frame(ws, message)
                    return
                self._send_frame(ws, item)
            except Exception as e:
                # ここでは解決しない。サーバーがエラー（例 401）を送ってから閉じたとき、送信の失敗が
                # 受信より先に来ると、エラーコードの無い「切断」で確定してしまい、キー無効の案内が出なくなる。
                # 終わり方は受信ループに任せる（エラー応答を読むか、切断を検知して解決する）
                logger.debug(f"{self.PROVIDER}: 送信エラー: {type(e).__name__}")
                return

    def _receive_loop(self, ws) -> None:
        """応答を受け続ける（切断で抜け、未解決なら失敗として解決する）。"""
        while True:
            with self._lock:
                if self._closed:
                    return
            try:
                message = ws.recv(timeout=_RECV_POLL_SEC)
            except TimeoutError:
                continue
            except Exception:
                # 正常終了（<fin> / finished）が先に届いていれば _resolve は 2 回目以降を無視する
                self._resolve(failed("disconnect"), "disconnect")
                return
            try:
                self._handle(message)
            except Exception as e:
                logger.debug(f"{self.PROVIDER}: 応答の処理でエラー（無視）: {e}")


# ----------------------------------------------------------------------
# Soniox
# ----------------------------------------------------------------------

class SonioxLiveSession(LiveSessionBase):
    """Soniox WebSocket による逐次文字起こしセッション（stt-rt-v5）。

    プロトコルの要点（Mac と同じ）:
    - 接続直後の最初のテキストフレームで設定 JSON（api_key・モデル・音声形式・言語ヒント）を送る
    - 音声は Int16 LE の生バイナリフレームで送る
    - 停止は 200ms の無音 → {"type":"finalize"} → 長さ 0 のバイナリフレーム。
      <fin> トークンが来た時点で確定文が揃う（"finished" を待たずに返す）
    """

    PROVIDER = "Soniox"
    KEY_ITEM = "soniox"
    SAMPLE_RATE = 16000
    # finalize の直前に送る 200ms の無音（16kHz・Int16 で 6,400 バイト）
    TRAILING_SILENCE = bytes(SAMPLE_RATE // 5 * 2)

    def __init__(
        self,
        model: str,
        language: str = "ja",
        prompt: str = "",
        on_interim: Optional[Callable[[str], None]] = None,
        endpoint: Optional[str] = None,
    ) -> None:
        """
        Args:
            model: モデル名（stt-rt-v5）
            language: 言語ヒント（空なら自動判定＝ヒントを送らない）
            prompt: 固有名詞などの文脈（スロットのプロンプト）
            on_interim: 途中経過の通知
            endpoint: 接続先の明示（テスト用）。None ならキーからリージョンを判定して決める
        """
        super().__init__(on_interim)
        self.model = model
        self.language = language
        self.prompt = prompt
        self._endpoint = endpoint
        self._transcript = SonioxTranscript()

    @staticmethod
    def replay_finish_timeout(audio_seconds: float) -> float:
        """録音全体を流し直したときに完了を待つ上限（3 秒 + 音声長の半分・最大 15 秒）。"""
        return min(15.0, 3.0 + max(0.0, audio_seconds) * 0.5)

    @staticmethod
    def config_json(api_key: str, model: str, language: str, prompt: str) -> str:
        """接続直後に送る設定 JSON（Mac の configJSON と同じ中身・キー順）。

        言語が空（自動判定）なら language_hints 自体を送らない。language_hints_strict は送らない
        ＝ヒント外の言語（英単語の混在など）も認識させる。
        """
        payload: dict = {
            "api_key": api_key,
            "audio_format": "pcm_s16le",
            "model": model,
            "num_channels": 1,
            "sample_rate": SonioxLiveSession.SAMPLE_RATE,
        }
        lang = (language or "").strip()
        if lang:
            payload["language_hints"] = [lang]
        trimmed = (prompt or "").strip()
        if trimmed:
            # 固有名詞・専門用語のヒント（スロットのプロンプトをそのまま文脈として渡す）
            payload["context"] = {"text": trimmed}
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)

    def _connect_target(self, key: str) -> Tuple[str, dict]:
        url = self._endpoint or soniox_region.websocket_url_for(key)
        return url, {}

    def _opening_messages(self, key: str) -> List[Any]:
        logger.info(
            f"Soniox 開始要求 (model={self.model}, lang={self.language or 'auto'})"
        )
        return [self.config_json(key, self.model, self.language, self.prompt)]

    def _encode(self, samples):
        return pcm16(samples)

    def _end_messages(self) -> List[Any]:
        # 200ms の無音 → finalize（未確定トークンを確定させる）→ 長さ 0 のフレーム（音声の終わり）
        return [self.TRAILING_SILENCE, json.dumps({"type": "finalize"}), b""]

    def _usage_seconds(self, sent_bytes: int) -> float:
        return sent_bytes / float(self.SAMPLE_RATE * 2)

    def _handle(self, message: Any) -> None:
        with self._lock:
            outcome, code, etype = self._transcript.apply(message)
            snapshot = self._transcript.full_text
            finalize_sent = self._close_requested
        if outcome == SonioxTranscript.IGNORED:
            return
        if outcome == SonioxTranscript.ERROR:
            # メッセージ本文は出さない（設定内容が混ざる可能性があるため。コードと種別で切り分ける）
            logger.error(f"Soniox エラー code={code or '-'} type={etype or '-'}")
            self._resolve(failed("error", code, etype), "error")
            return
        if snapshot:
            self._notify_interim(snapshot)
        if outcome == SonioxTranscript.FINALIZED:
            # finalize を送った後の <fin> ＝送った音声の確定文はすべて揃った。ここで返す
            if finalize_sent:
                self._resolve(ENDING_COMPLETED, "fin")
        elif outcome == SonioxTranscript.FINISHED:
            self._resolve(ENDING_COMPLETED, "finished")

    def _text(self) -> str:
        with self._lock:
            return self._transcript.full_text


# ----------------------------------------------------------------------
# OpenAI ライブ
# ----------------------------------------------------------------------

class OpenAILiveSession(LiveSessionBase):
    """OpenAI Realtime のライブ文字起こし（gpt-live-transcribe）。

    Deepgram との違い（Mac と同じ）:
    - 音声は base64 を JSON に載せて送る（input_audio_buffer.append）
    - 24kHz 以上しか受けないので 16kHz を 1.5 倍にリサンプルする
    - 終端は input_audio_buffer.commit（turn_detection=null の手動確定）
    終わり方は区別しない（Mac の OpenAILiveTranscriber と同じく unknown）。
    """

    PROVIDER = "OpenAI ライブ"
    KEY_ITEM = "openai"
    OUTPUT_RATE = 24000
    # 遅延と精度のトレードオフ。実測で minimal が最速（Mac と同じ固定値）
    DELAY = "minimal"
    URL = "wss://api.openai.com/v1/realtime?intent=transcription"

    def __init__(
        self,
        model: str,
        language: str = "ja",
        on_interim: Optional[Callable[[str], None]] = None,
    ) -> None:
        super().__init__(on_interim)
        self.model = model
        self.language = language or "ja"
        self._upsampler = Upsampler16To24()
        self._deltas = ""
        self._final = ""

    def session_update_json(self) -> str:
        """セッション設定 JSON（transcription セッション・PCM 24kHz・手動 commit）。"""
        payload = {
            "type": "session.update",
            "session": {
                "type": "transcription",
                "audio": {
                    "input": {
                        "format": {"type": "audio/pcm", "rate": self.OUTPUT_RATE},
                        "transcription": {
                            "model": self.model,
                            "language": self.language,
                            "delay": self.DELAY,
                        },
                        # null＝サーバー VAD を使わず、commit したときだけ確定させる
                        # （押している間が 1 ターン＝ホットキー操作と一致する）
                        "turn_detection": None,
                    }
                },
            },
        }
        return json.dumps(payload)

    def _connect_target(self, key: str) -> Tuple[str, dict]:
        return self.URL, {"Authorization": f"Bearer {key}"}

    def _opening_messages(self, key: str) -> List[Any]:
        return [self.session_update_json()]

    def _encode(self, samples):
        pcm = pcm16(self._upsampler.process(samples))
        if not pcm:
            return None
        # 手組みするのはチャンクごとの JSON 生成の負担を避けるため（base64 は JSON で安全な文字だけ）
        return '{"type":"input_audio_buffer.append","audio":"' + base64.b64encode(pcm).decode("ascii") + '"}'

    def _frame_audio_bytes(self, frame: Any) -> int:
        if isinstance(frame, str) and frame.startswith('{"type":"input_audio_buffer.append"'):
            # base64 の長さから元のバイト数を出す（4 文字 = 3 バイト）
            b64_len = len(frame) - len('{"type":"input_audio_buffer.append","audio":"') - len('"}')
            return max(0, b64_len * 3 // 4)
        return 0

    def _end_messages(self) -> List[Any]:
        return ['{"type":"input_audio_buffer.commit"}']

    def _usage_seconds(self, sent_bytes: int) -> float:
        return sent_bytes / float(self.OUTPUT_RATE * 2)

    def _handle(self, message: Any) -> None:
        if isinstance(message, (bytes, bytearray)):
            message = message.decode("utf-8", errors="replace")
        try:
            msg = json.loads(message)
        except Exception:
            return
        if not isinstance(msg, dict):
            return
        mtype = str(msg.get("type") or "")
        if mtype == "error":
            # 本文はキー等が混ざりうるので出さない（種別だけ）
            err = msg.get("error") if isinstance(msg.get("error"), dict) else {}
            logger.error(f"OpenAI ライブ エラー type={err.get('type', '-')} code={err.get('code', '-')}")
            self._resolve(ENDING_UNKNOWN, "error")
            return
        if mtype.endswith("transcription.delta"):
            delta = msg.get("delta") or ""
            if not delta:
                return
            with self._lock:
                self._deltas += delta
                snapshot = self._deltas
            self._notify_interim(snapshot)
            return
        if mtype.endswith("transcription.completed"):
            transcript = str(msg.get("transcript") or "").strip()
            if transcript:
                with self._lock:
                    self._final = transcript
                self._notify_interim(transcript)
            # 手動 commit なので completed ＝このターンの確定＝終端
            self._resolve(ENDING_UNKNOWN, "completed")

    def _resolve(self, ending: LiveEnding, reason: str) -> None:
        """OpenAI ライブは終わり方を区別しない（Mac と同じ）。理由はログにだけ残す。"""
        super()._resolve(ENDING_UNKNOWN, reason)

    def _text(self) -> str:
        with self._lock:
            return self._final or self._deltas
