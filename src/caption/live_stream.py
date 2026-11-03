"""
ライブ字幕・議事録用の連続ストリーミング認識（Soniox / OpenAI ライブ）

Mac 版 SonioxCaptionStream.swift の移植。音声入力のライブ（`core/live_transcriber.py`）は
「押している間の 1 回」だが、こちらは字幕を出している間ずっと繋ぎ続けるので、次を持つ:

- 切断したら上限つきの指数バックオフで張り直す（`ReconnectPolicy`。上限を超えたら `on_give_up` で知らせて止まる）。
  401 / 402 / 403 は張り直しても直らないので即諦め、理由（キー無効・残高不足）を返す。
- **無音が 30 秒続いたら WebSocket だけ切り、声が戻ったら張り直す**（`IdleTracker`）。接続時間・送った音声で
  課金されるので、無音のあいだも繋ぎ続けると課金され続ける。この切断は失敗に数えない。
- 接続前・張り直し中の音声は 3 秒ぶんだけ溜め、繋がったら先に送る（話し始めを欠かさない）。

スレッドは 1 本だけ（`_run`）。送信・受信・キープアライブ・時間切れの判定をすべてこの中で順に行う
（websockets の同期クライアントを複数スレッドから触らないため）。`send()` は音声スレッドから呼ばれ、
変換してキューに積むだけ（ロックは一瞬・通信なし）。

キー・原文・訳文はログに出さない（接続の出来事とエラーコードだけ）。
"""

import base64
import json
import threading
import time
from collections import deque
from typing import Any, Callable, Deque, List, Optional, Tuple

import numpy as np

from ..core import soniox_region
from ..core.live_transcriber import Upsampler16To24, open_websocket, pcm16
from ..utils import secrets
from ..utils.logger import get_logger
from . import soniox_assembler as sa

logger = get_logger(__name__)

SAMPLE_RATE = 16000
# 接続前に溜めておく音声の上限（秒）
MAX_PENDING_SECONDS = 3.0
# 受信ループが詰まったときに音声キューへ溜める上限（秒）。超えたら古い分から捨てる
MAX_QUEUED_SECONDS = 10.0
# 受信の待ち（秒）。短いほど送信の遅れが減る。30ms なら 1 秒 33 回で CPU はほぼ使わない
RECV_POLL = 0.03
# 音声を送らない間のキープアライブ間隔（Soniox は 20 秒以上何も送らないと切る）
KEEPALIVE_INTERVAL = 10.0
# 停止時に、終端を送ってから残りの確定を待つ上限（秒）
FINISH_WAIT_LIMIT = 1.5
# 切れ目が来てから訳を待つ上限（秒）。過ぎたら訳の有無にかかわらず行を確定する
TRANSLATION_WAIT_LIMIT = 2.0
# 接続を張るときの待ちの上限（秒）
OPEN_TIMEOUT = 5.0


class _Fatal(Exception):
    """張り直しても直らない失敗（理由はユーザー向け）。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def handshake_status(error: Exception) -> Optional[int]:
    """WebSocket の接続（HTTP ハンドシェイク）で返った HTTP ステータス（取れなければ None）。"""
    response = getattr(error, "response", None)
    status = getattr(response, "status_code", None)
    if status is None:
        status = getattr(error, "status_code", None)
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


class CaptionLiveStream:
    """連続ストリーミング認識の共通部分（1 字幕セッション = 1 インスタンス）。

    サブクラスが決めるもの: PROVIDER / KEY_ITEM / MODEL、`_connect_target` / `_opening_messages` /
    `_encode` / `_keepalive_message` / `_end_messages` / `_handle` / `_flush_lines` / `_tick`。

    Attributes:
        on_live: ライブ行の更新 (原文, 訳)。訳が無ければ空文字
        on_confirmed: 確定した 1 行 (原文, 訳)。文字起こしだけなら訳は空文字
        on_give_up: 張り直しを諦めた（ユーザー向けの理由）。1 回だけ呼ぶ
        on_link_state: 接続状態（sa.LINK_*）が変わった
        on_usage: 1 接続ぶんの送った音声の秒数（API 料金の記録用）
    """

    PROVIDER = "live"
    KEY_ITEM = ""
    MODEL = ""

    def __init__(self, endpoint: Optional[str] = None) -> None:
        """
        Args:
            endpoint: 接続先の明示（テスト用の模擬サーバー）。None ならプロバイダーの既定
        """
        self._endpoint = endpoint
        self._audio_lock = threading.Lock()
        self._audio: Deque[Tuple[Any, int, bool]] = deque()
        self._queued_samples = 0
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._gave_up = False
        self.on_live: Optional[Callable[[str, str], None]] = None
        self.on_confirmed: Optional[Callable[[str, str], None]] = None
        self.on_give_up: Optional[Callable[[str], None]] = None
        self.on_link_state: Optional[Callable[[str], None]] = None
        self.on_usage: Optional[Callable[[float], None]] = None
        # 時刻の読み方（テストで差し替えて 30 秒の無音を一瞬で再現する）
        self.clock: Callable[[], float] = time.monotonic

    # --- 外から使う口 ---

    def start(self, api_key: Optional[str] = None) -> bool:
        """接続スレッドを起動する（即座に返る）。キー未設定・websockets 未導入なら False。"""
        key = api_key if api_key is not None else secrets.api_key(self.KEY_ITEM)
        if not key:
            logger.warning(f"{self.PROVIDER} 字幕: API キーが未設定のため開始しません")
            return False
        try:
            from websockets.sync.client import connect  # noqa: F401
        except Exception as e:
            logger.warning(f"websockets が利用できないため字幕の接続を開始しません: {e}")
            return False
        self._thread = threading.Thread(target=self._run_safely, args=(key,), daemon=True,
                                        name=f"Caption{self.PROVIDER}")
        self._thread.start()
        return True

    def send(self, samples: np.ndarray) -> None:
        """16kHz モノラル Float32 を積む（音声スレッドから呼ばれる。変換して積むだけ）。"""
        if samples is None or len(samples) == 0 or self._stop_event.is_set():
            return
        samples = np.asarray(samples, dtype=np.float32)
        voiced = float(np.sqrt(np.mean(samples * samples))) >= sa.IdleTracker.VOICE_RMS
        frame = self._encode(samples)
        if frame is None:
            return
        count = int(samples.size)
        limit = int(MAX_QUEUED_SECONDS * SAMPLE_RATE)
        with self._audio_lock:
            self._audio.append((frame, count, voiced))
            self._queued_samples += count
            while self._queued_samples > limit and self._audio:
                _, dropped, _ = self._audio.popleft()
                self._queued_samples -= dropped

    def stop(self, timeout: float = 3.0) -> None:
        """止める。接続中なら終端を送って残りの確定を待ち、残りを 1 行に確定してから閉じる。

        停止の瞬間に喋っていた最後の語を議事録から落とさないため（Mac と同じ）。戻った後はコールバックを呼ばない。
        """
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

    @property
    def is_running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    # --- サブクラスが実装する部分 ---

    def _connect_target(self, key: str) -> Tuple[str, dict]:
        raise NotImplementedError

    def _opening_messages(self, key: str) -> List[Any]:
        return []

    def _encode(self, samples: np.ndarray) -> Any:
        raise NotImplementedError

    def _keepalive_message(self) -> Optional[Any]:
        return None

    def _end_messages(self) -> List[Any]:
        return []

    def _handle(self, message: Any, finishing: bool) -> Optional[str]:
        """応答 1 件を処理する。"finished" を返したら停止待ちを終える。致命的なら _Fatal を投げる。"""
        raise NotImplementedError

    def _flush_lines(self) -> None:
        """切断・停止のときに、残っている分を確定行として出す（落とさない）。"""

    def _tick(self, now: float) -> None:
        """受信の合間に呼ぶ（訳待ちの時間切れなど）。"""

    def _frame_audio_seconds(self, samples: int) -> float:
        return samples / float(SAMPLE_RATE)

    # --- 通知 ---

    def _emit_live(self, source: str, translation: str) -> None:
        if self._stop_event.is_set() and self.on_live is None:
            return
        callback = self.on_live
        if callback is not None:
            try:
                callback(source, translation)
            except Exception:
                pass

    def _emit_confirmed(self, source: str, translation: str) -> None:
        if not source and not translation:
            return
        callback = self.on_confirmed
        if callback is not None:
            try:
                callback(source, translation)
            except Exception:
                pass

    def _set_link_state(self, state: str) -> None:
        callback = self.on_link_state
        if callback is not None:
            try:
                callback(state)
            except Exception:
                pass

    def _give_up(self, reason: str) -> None:
        if self._gave_up:
            return
        self._gave_up = True
        logger.error(f"{self.PROVIDER} 字幕の再接続を諦めます: {reason}")
        callback = self.on_give_up
        if callback is not None:
            try:
                callback(reason)
            except Exception:
                pass

    # --- 内部 ---

    def _take_audio(self) -> List[Tuple[Any, int, bool]]:
        with self._audio_lock:
            if not self._audio:
                return []
            frames = list(self._audio)
            self._audio.clear()
            self._queued_samples = 0
            return frames

    def _run_safely(self, key: str) -> None:
        try:
            self._run(key)
        except Exception as e:  # 想定外でもアプリを落とさない
            logger.error(f"{self.PROVIDER} 字幕の接続処理で予期しないエラー: {type(e).__name__}: {e}")
            self._give_up(f"{self.PROVIDER} の接続処理でエラーが起きました")

    def _open(self, key: str):
        """接続して設定フレームを送る（失敗は例外。401/402/403 は _Fatal）。"""
        url, headers = self._connect_target(key)
        try:
            ws = open_websocket(url, headers, open_timeout=OPEN_TIMEOUT)
        except Exception as e:
            status = handshake_status(e)
            if sa.ReconnectPolicy.is_fatal(status):
                raise _Fatal(sa.ReconnectPolicy.give_up_reason(status, self.PROVIDER))
            raise
        for message in self._opening_messages(key):
            ws.send(message)
        return ws

    def _close(self, ws) -> None:
        try:
            ws.close()
        except Exception:
            pass

    def _report_usage(self, samples: int) -> None:
        if samples <= 0 or self.on_usage is None:
            return
        try:
            self.on_usage(self._frame_audio_seconds(samples))
        except Exception:
            pass

    def _run(self, key: str) -> None:
        """接続・送信・受信・張り直しを 1 本のスレッドで回す。"""
        reconnect = sa.ReconnectPolicy()
        idle = sa.IdleTracker()
        ws = None
        pending: Deque[Tuple[Any, int, bool]] = deque()
        pending_samples = 0
        next_connect_at: Optional[float] = self.clock()
        connected_at = 0.0
        last_sent = 0.0
        sent_samples = 0
        pending_limit = int(MAX_PENDING_SECONDS * SAMPLE_RATE)

        while True:
            now = self.clock()
            stopping = self._stop_event.is_set()
            frames = self._take_audio()

            if ws is None:
                if stopping:
                    self._flush_lines()
                    return
                wake = False
                for frame in frames:
                    if idle.note_frame(frame[2], now):
                        wake = True
                    pending.append(frame)
                    pending_samples += frame[1]
                    while pending_samples > pending_limit and pending:
                        pending_samples -= pending.popleft()[1]
                if idle.is_idle:
                    if not wake:
                        time.sleep(0.05)
                        continue
                    # 無音で切っていたなら、有音が来た時点で張り直す（失敗ではないので回数に数えない）
                    logger.info(f"音が戻ったので {self.PROVIDER} 字幕を張り直します")
                    next_connect_at = now
                if next_connect_at is None or now < next_connect_at:
                    time.sleep(0.05)
                    continue
                try:
                    ws = self._open(key)
                except _Fatal as e:
                    self._flush_lines()
                    self._give_up(e.reason)
                    return
                except Exception as e:
                    logger.warning(f"{self.PROVIDER} 字幕の接続に失敗: {type(e).__name__}")
                    delay = reconnect.next_delay(False)
                    if delay is None:
                        self._give_up(f"{self.PROVIDER} に接続できません（ネットワークを確認してください）")
                        return
                    next_connect_at = self.clock() + delay
                    self._set_link_state(sa.LINK_RECONNECTING)
                    continue
                connected_at = last_sent = self.clock()
                sent_samples = 0
                idle.note_connected(connected_at)
                try:
                    # 溜めた音声ごと送るので話し始めは欠けない
                    for frame, count, _ in pending:
                        ws.send(frame)
                        sent_samples += count
                except Exception:
                    pass
                pending.clear()
                pending_samples = 0
                logger.info(f"{self.PROVIDER} 字幕を接続しました")
                self._set_link_state(sa.LINK_CONNECTED)
                continue

            # --- 接続中 ---
            broken = False
            for frame, count, voiced in frames:
                idle.note_frame(voiced, now)
                try:
                    ws.send(frame)
                    sent_samples += count
                    last_sent = now
                except Exception:
                    broken = True
                    break

            if stopping and not broken:
                self._finish(ws)
                self._report_usage(sent_samples)
                return

            if not broken and idle.should_disconnect(now):
                # 無音が続いたので WebSocket だけ閉じる（課金を止める）。失敗ではないので回数に数えない
                self._flush_lines()
                self._close(ws)
                ws = None
                self._report_usage(sent_samples)
                logger.info(f"無音が続いたため {self.PROVIDER} 字幕を切断（音が来たら張り直す）")
                self._set_link_state(sa.LINK_IDLE)
                continue

            if not broken and now - last_sent >= KEEPALIVE_INTERVAL:
                keepalive = self._keepalive_message()
                last_sent = now
                if keepalive is not None:
                    try:
                        ws.send(keepalive)
                    except Exception:
                        broken = True

            message = None
            fatal_reason: Optional[str] = None
            server_closed = False
            if not broken:
                try:
                    message = ws.recv(timeout=RECV_POLL)
                except TimeoutError:
                    message = None
                except Exception:
                    broken = True
            if message is not None:
                try:
                    if self._handle(message, finishing=False) == "finished":
                        server_closed = True
                    elif self.clock() - connected_at >= sa.ReconnectPolicy.HEALTHY_DURATION:
                        # しばらく安定して受信できていれば、失敗の回数を数え直す
                        reconnect.note_healthy()
                except _Fatal as e:
                    fatal_reason = e.reason
                except Exception as e:
                    logger.debug(f"{self.PROVIDER} 字幕: 応答の処理でエラー（無視）: {e}")
            self._tick(self.clock())

            if fatal_reason is not None:
                self._flush_lines()
                self._close(ws)
                self._report_usage(sent_samples)
                self._give_up(fatal_reason)
                return
            if broken or server_closed:
                # 切れる直前までの分は確定させておく（張り直しで話が飛ばないように）
                self._flush_lines()
                self._close(ws)
                ws = None
                self._report_usage(sent_samples)
                if self._stop_event.is_set():
                    return
                delay = reconnect.next_delay(False)
                if delay is None:
                    self._give_up(f"{self.PROVIDER} との接続が何度も切れました")
                    return
                logger.info(f"{self.PROVIDER} 字幕を {delay:.1f} 秒後に再接続（{reconnect.failures} 回目）")
                next_connect_at = self.clock() + delay
                self._set_link_state(sa.LINK_RECONNECTING)

    def _finish(self, ws) -> None:
        """終端を送り、残りの確定を上限つきで待ってから閉じる。"""
        try:
            for message in self._end_messages():
                ws.send(message)
        except Exception:
            self._flush_lines()
            self._close(ws)
            return
        deadline = self.clock() + FINISH_WAIT_LIMIT
        while self.clock() < deadline:
            try:
                message = ws.recv(timeout=RECV_POLL)
            except TimeoutError:
                continue
            except Exception:
                break
            try:
                if self._handle(message, finishing=True) == "finished":
                    break
            except _Fatal:
                break
            except Exception:
                pass
        self._flush_lines()
        self._close(ws)


# ----------------------------------------------------------------------
# Soniox（認識だけ・認識＋翻訳）
# ----------------------------------------------------------------------

class SonioxCaptionStream(CaptionLiveStream):
    """Soniox の連続ストリーミング（翻訳モードなら英→日の訳も同じ接続で受け取る）。"""

    PROVIDER = "Soniox"
    KEY_ITEM = "soniox"
    MODEL = "stt-rt-v5"
    # エンドポイント検出の最大待ち（ミリ秒）。字幕は速さ重視なので許される最小の 500ms（Mac と同じ）
    MAX_ENDPOINT_DELAY_MS = 500

    def __init__(self, translates: bool, language: str, endpoint: Optional[str] = None) -> None:
        """
        Args:
            translates: 英→日の翻訳もするか（翻訳エンジンが Soniox のとき）
            language: 認識言語のヒント（"en" / "ja"）
            endpoint: 接続先の明示（テスト用）
        """
        super().__init__(endpoint)
        self.translates = translates
        self.language = language
        self._assembler = sa.SonioxCaptionAssembler(translates)
        self._wait_started: Optional[float] = None

    @staticmethod
    def config_json(api_key: str, translates: bool, language: str) -> str:
        """接続直後に送る設定 JSON（Mac の configJSON と同じ中身・キー順）。"""
        payload = {
            "api_key": api_key,
            "model": SonioxCaptionStream.MODEL,
            "audio_format": "pcm_s16le",
            "sample_rate": SAMPLE_RATE,
            "num_channels": 1,
            "language_hints": [language],
            "enable_endpoint_detection": True,
            "max_endpoint_delay_ms": SonioxCaptionStream.MAX_ENDPOINT_DELAY_MS,
        }
        if translates:
            payload["translation"] = {"type": "one_way", "target_language": "ja"}
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)

    def _connect_target(self, key: str) -> Tuple[str, dict]:
        return (self._endpoint or soniox_region.websocket_url_for(key)), {}

    def _opening_messages(self, key: str) -> List[Any]:
        return [self.config_json(key, self.translates, self.language)]

    def _encode(self, samples: np.ndarray) -> Any:
        return pcm16(samples)

    def _keepalive_message(self) -> Optional[Any]:
        return '{"type":"keepalive"}'

    def _end_messages(self) -> List[Any]:
        # finalize（未確定トークンを確定させる）→ 長さ 0 のフレーム（音声の終わり）。音声入力と同じ作法
        return ['{"type":"finalize"}', b""]

    def _handle(self, message: Any, finishing: bool) -> Optional[str]:
        response = sa.parse(message)
        if response is None:
            return None
        if response.error_code is not None:
            code = response.error_code
            logger.error(f"Soniox 字幕のエラー code={code}")
            if sa.ReconnectPolicy.is_fatal(code):
                raise _Fatal(sa.ReconnectPolicy.give_up_reason(code))
            return "finished"
        for kind, source, translation in self._assembler.apply(response):
            if kind == sa.CONFIRMED:
                self._emit_confirmed(source, translation)
            elif not finishing:
                self._emit_live(source, translation)
        return "finished" if response.finished else None

    def _flush_lines(self) -> None:
        line = self._assembler.flush()
        self._wait_started = None
        if line is not None:
            self._emit_confirmed(line[1], line[2])

    def _tick(self, now: float) -> None:
        # 切れ目の後に訳を待っている間だけ、時間切れの確定を仕掛ける
        if not self._assembler.is_waiting_for_translation:
            self._wait_started = None
            return
        if self._wait_started is None:
            self._wait_started = now
            return
        if now - self._wait_started >= TRANSLATION_WAIT_LIMIT:
            self._wait_started = None
            # 確定済みの分だけを行にする（未確定分は後で確定トークンとして届くので二重にしない）
            line = self._assembler.flush_confirmed()
            if line is not None:
                self._emit_confirmed(line[1], line[2])


# ----------------------------------------------------------------------
# OpenAI ライブ（認識だけ）
# ----------------------------------------------------------------------

class OpenAICaptionStream(CaptionLiveStream):
    """OpenAI Realtime の連続文字起こし（サーバー側の発話区切りで 1 発話ずつ確定する）。

    音声入力のライブ（手動 commit）と違い、字幕は話が途切れずに続くので server_vad で区切らせる。
    音声は 24kHz 以上しか受けないので 16kHz を 1.5 倍にして base64 で JSON に載せる。
    """

    PROVIDER = "OpenAI"
    KEY_ITEM = "openai"
    MODEL = "gpt-live-transcribe"
    OUTPUT_RATE = 24000
    URL = "wss://api.openai.com/v1/realtime?intent=transcription"
    # 話の切れ目とみなす無音（ミリ秒）。字幕は速さ重視なので短め
    SILENCE_DURATION_MS = 400

    # 張り直しても直らないエラー（キー・残高）
    _FATAL_CODES = {
        "invalid_api_key": "OpenAI のキーが無効",
        "insufficient_quota": "OpenAI の残高不足",
    }

    def __init__(self, language: str, endpoint: Optional[str] = None) -> None:
        super().__init__(endpoint)
        self.language = language or "ja"
        self._upsampler = Upsampler16To24()
        # 発話ごとの途中経過（item_id → テキスト）。届いた順に並べてライブ行にする
        self._items: "dict[str, str]" = {}

    def session_update_json(self) -> str:
        """セッション設定（transcription・PCM 24kHz・サーバー側の発話区切り）。"""
        return json.dumps({
            "type": "session.update",
            "session": {
                "type": "transcription",
                "audio": {
                    "input": {
                        "format": {"type": "audio/pcm", "rate": self.OUTPUT_RATE},
                        "transcription": {"model": self.MODEL, "language": self.language, "delay": "minimal"},
                        "turn_detection": {
                            "type": "server_vad",
                            "prefix_padding_ms": 300,
                            "silence_duration_ms": self.SILENCE_DURATION_MS,
                        },
                    }
                },
            },
        })

    def _connect_target(self, key: str) -> Tuple[str, dict]:
        return (self._endpoint or self.URL), {"Authorization": f"Bearer {key}"}

    def _opening_messages(self, key: str) -> List[Any]:
        return [self.session_update_json()]

    def _encode(self, samples: np.ndarray) -> Any:
        pcm = pcm16(self._upsampler.process(samples))
        if not pcm:
            return None
        return '{"type":"input_audio_buffer.append","audio":"' + base64.b64encode(pcm).decode("ascii") + '"}'

    def _end_messages(self) -> List[Any]:
        return ['{"type":"input_audio_buffer.commit"}']

    def _live_text(self) -> str:
        return " ".join(text.strip() for text in self._items.values() if text.strip())

    def _handle(self, message: Any, finishing: bool) -> Optional[str]:
        if isinstance(message, (bytes, bytearray)):
            message = message.decode("utf-8", errors="replace")
        try:
            msg = json.loads(message)
        except Exception:
            return None
        if not isinstance(msg, dict):
            return None
        mtype = str(msg.get("type") or "")
        if mtype == "error":
            err = msg.get("error") if isinstance(msg.get("error"), dict) else {}
            code = str(err.get("code") or "")
            logger.error(f"OpenAI 字幕のエラー type={err.get('type', '-')} code={code or '-'}")
            if code in self._FATAL_CODES:
                raise _Fatal(self._FATAL_CODES[code])
            # 空の commit など、続けてよいエラー。停止待ちなら待つのをやめる
            return "finished" if finishing else None
        item_id = str(msg.get("item_id") or "")
        if mtype.endswith("transcription.delta"):
            delta = msg.get("delta") or ""
            if delta:
                self._items[item_id] = self._items.get(item_id, "") + delta
                if not finishing:
                    self._emit_live(self._live_text(), "")
            return None
        if mtype.endswith("transcription.completed"):
            transcript = str(msg.get("transcript") or "").strip()
            self._items.pop(item_id, None)
            if transcript:
                self._emit_confirmed(transcript, "")
            if not finishing:
                self._emit_live(self._live_text(), "")
            return "finished" if finishing and not self._items else None
        return None

    def _flush_lines(self) -> None:
        # 確定しなかった途中経過も落とさず確定行にする
        rest = self._live_text()
        self._items.clear()
        if rest:
            self._emit_confirmed(rest, "")

    def _frame_audio_seconds(self, samples: int) -> float:
        # 数えているのは 16kHz での元のサンプル数（送ったのは 1.5 倍だが時間は同じ）
        return samples / float(SAMPLE_RATE)


def make_stream(recognizer: str, translates: bool, language: str) -> CaptionLiveStream:
    """設定の認識エンジンからストリームを作る（"soniox" / "openai_live"）。"""
    if recognizer == "openai_live":
        return OpenAICaptionStream(language)
    return SonioxCaptionStream(translates, language)
