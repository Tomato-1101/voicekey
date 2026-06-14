"""
音声録音モジュール

sounddevice (PortAudio) によるマイク録音を、専用の音声制御スレッドで管理する。

設計方針（macOS PortAudio フリーズ問題の根治）:

1. **PortAudio 呼び出しの完全隔離**: open/start/stop/close はすべて単一の
   AudioControl スレッドだけが実行する。ホットキーリスナー（macOS の
   CGEventTap コールバックスレッド）からはコマンドを投げるだけで一切
   ブロックしない。リスナー上で CoreAudio がブロックすると macOS が
   イベントタップを無効化し「ホットキー永久無反応」になるため、これが
   旧設計の最重要欠陥だった。

2. **永続ストリーム**: 録音のたびに open/close せず、ストリームは開いた
   まま start/stop だけを繰り返す。macOS で最もハングしやすいのは
   close であり、open/close の回数自体を減らすことでハングの発生機会を
   大幅に減らす。停止中は IO が止まるため OS のマイクインジケーターも消える。

3. **アイドル時の自動クローズ**: 一定時間録音がなければストリームを close
   してマイクを OS に完全返却する（デバイス占有を最小化）。

4. **ウォッチドッグによる自己回復**: 万一 PortAudio 呼び出しがハングしても
   ブロックするのは AudioControl スレッドだけ。health() で検知でき、
   recover() で制御スレッドごと作り直して録音機能を復旧できる
   （ハングしたストリームは leak するが、従来の「アプリ再起動が唯一の
   回復手段」より大幅に改善）。

5. **末尾の取りこぼし防止**: stop は AudioControl スレッド上で同期的に
   stream.stop() を呼んでから queue をドレインする。PortAudio 内部
   バッファの末尾チャンクが確実に回収される（リスナーはブロックしない）。
"""

import queue
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Union

import numpy as np
import numpy.typing as npt

from ..config.constants import SAMPLE_RATE, AUDIO_CHANNELS, AUDIO_DTYPE
from ..utils.logger import get_logger

logger = get_logger(__name__)

# 録音せずにこの秒数が経過したらストリームを close してマイクを完全解放する
_IDLE_CLOSE_SEC: float = 30.0
# 音声レベルコールバックの最小間隔（HUD 描画用、約 30fps）
_LEVEL_INTERVAL_SEC: float = 0.033


class AudioRecorder:
    """
    永続ストリーム + 専用制御スレッドによる音声録音管理。

    公開メソッドはすべてノンブロッキング（コマンド投函のみ）。
    結果はコールバックで非同期に受け取る。

    Attributes:
        sample_rate: サンプリングレート（Hz）
        is_recording: 録音中かどうか（意図ではなく実際のストリーム状態）
    """

    def __init__(
        self,
        sample_rate: int = SAMPLE_RATE,
        input_device: Optional[Union[int, str]] = "default",
        level_callback: Optional[Callable[[float], None]] = None,
        chunk_callback: Optional[Callable[[npt.NDArray[np.float32]], None]] = None,
    ) -> None:
        """
        AudioRecorderを初期化する。

        Args:
            sample_rate: サンプリングレート（デフォルト: 16000Hz）
            input_device: 入力デバイス（"default" / デバイスID / デバイス名）
            level_callback: 録音中の音声レベル（0.0-1.0）を受け取るコールバック。
                            audio callback スレッドから throttle 付きで呼ばれる
            chunk_callback: 録音中の生 PCM チャンク（float32 モノラル）を受け取る
                            コールバック。Deepgram ストリーミングへ逐次送るために使う。
                            録音ごとにアプリ側で差し替え/解除する（録音中のみ発火）
        """
        self.sample_rate = sample_rate
        self._level_callback = level_callback
        # ストリーミング送出用フック。録音ごとにアプリ側が set/clear する公開属性
        self.chunk_callback = chunk_callback
        self._input_device = self.normalize_device_setting(input_device)

        # 録音データ（audio callback → stop 時にドレイン）
        self._audio_q: queue.Queue = queue.Queue()
        # 制御コマンド（公開メソッド → AudioControl スレッド）
        self._commands: queue.Queue = queue.Queue()

        self._stream: Optional[Any] = None      # sd.InputStream（制御スレッドのみが触る）
        self._stream_device: Optional[Union[int, str]] = None  # 現ストリームのデバイス
        self._recording = False
        self._last_record_end = time.monotonic()

        # セッション ID: ハングして leak した旧ストリームのゾンビ callback を弾く
        self._session_id = 0
        self._callback_logged = False
        self._last_level_time = 0.0

        # ウォッチドッグ用: 実行中の PortAudio 操作と開始時刻
        self._busy_op: Optional[str] = None
        self._busy_since: float = 0.0
        # recover() で世代を進めると旧制御スレッドはコマンド処理後に自然終了する
        self._generation = 0
        # stop コマンドのコールバック退避（ハング時に recover() が代行発火するため）
        self._pending_stop_cb: Optional[Callable] = None

        self._state_lock = threading.Lock()
        self._start_control_thread()

    # ------------------------------------------------------------------
    # 公開 API（すべてノンブロッキング）
    # ------------------------------------------------------------------

    @property
    def is_recording(self) -> bool:
        """録音中かどうかを返す。"""
        return self._recording

    def start_async(self, on_started: Optional[Callable[[bool], None]] = None) -> None:
        """
        録音開始をリクエストする（即座に返る）。

        Args:
            on_started: 開始結果（成功 True）を受け取るコールバック。
                        AudioControl スレッド上で呼ばれる
        """
        self._commands.put(("start", on_started))

    def stop_async(self, on_audio: Callable[[npt.NDArray[np.float32]], None]) -> None:
        """
        録音停止をリクエストする（即座に返る）。

        Args:
            on_audio: 確定した録音データを受け取るコールバック。
                      AudioControl スレッド上で呼ばれる（重い処理は受け側で逃がすこと）
        """
        self._commands.put(("stop", on_audio))

    def set_input_device(self, device: Any) -> None:
        """使用する入力デバイス設定を更新する（次回ストリーム再作成時に適用）。"""
        normalized = self.normalize_device_setting(device)
        self._input_device = normalized
        device_label = "default" if normalized is None else str(normalized)
        logger.info(f"入力デバイス設定: {device_label}")
        # 録音中でなければ即座に作り直す（録音中なら次の start 時に適用される）
        self._commands.put(("reopen_if_idle", None))

    def shutdown(self, timeout: float = 1.0) -> None:
        """
        制御スレッドを停止しストリームを閉じる（アプリ終了時用）。

        ハング中でも timeout 秒以上はブロックしない。
        プロセス終了時に OS がハンドルを回収するため、待ちきれなくても実害はない。
        """
        self._commands.put(("shutdown", None))
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)

    def health(self) -> Dict[str, Any]:
        """
        ウォッチドッグ用の健全性情報を返す。

        Returns:
            {"busy_op": 実行中操作名 or None, "busy_sec": 経過秒, "alive": スレッド生存}
        """
        busy_op = self._busy_op
        busy_sec = (time.monotonic() - self._busy_since) if busy_op else 0.0
        alive = self._thread is not None and self._thread.is_alive()
        return {"busy_op": busy_op, "busy_sec": busy_sec, "alive": alive}

    def recover(self) -> None:
        """
        ハングした制御スレッドを見捨てて新しい制御スレッドを立てる。

        ハング中のストリーム/スレッドは leak する（プロセス終了まで残る）が、
        録音機能自体は即座に復旧する。ウォッチドッグ（app 側）から呼ばれる。
        """
        with self._state_lock:
            health = self.health()
            logger.warning(
                f"音声制御スレッドを再生成します (busy_op={health['busy_op']}, "
                f"busy_sec={health['busy_sec']:.1f}s)"
            )
            # 世代を進める → 旧スレッドは現在の操作から戻り次第、静かに終了する
            self._generation += 1
            self._session_id += 1  # 旧ストリームの callback をゾンビ化
            self._stream = None    # 旧ストリームへの参照を放棄（close は不可能なので leak）
            self._recording = False
            self._busy_op = None

            # 完了待ちだった stop コールバックには「ここまでに取れた音声」を渡す
            pending = self._pending_stop_cb
            self._pending_stop_cb = None

            # コマンドキューを作り直す（旧スレッドが復帰しても新コマンドを食わせない）
            self._commands = queue.Queue()
            self._start_control_thread()

        if pending is not None:
            try:
                pending(self._drain_audio())
            except Exception as e:
                logger.error(f"recover 時の stop コールバックでエラー: {e}")

    # ------------------------------------------------------------------
    # デバイス一覧（設定 UI 用。PortAudio のデバイス列挙はスナップショット読みで安全）
    # ------------------------------------------------------------------

    @staticmethod
    def normalize_device_setting(device: Any) -> Optional[Union[int, str]]:
        """
        設定値をsounddeviceで扱える入力デバイス形式に正規化する。

        Returns:
            None: システムデフォルトを使用
            int/str: sounddeviceのdevice引数として使用
        """
        if device is None:
            return None
        if isinstance(device, str):
            value = device.strip()
            if not value or value.lower() == "default":
                return None
            if value.isdigit():
                return int(value)
            return value
        if isinstance(device, (int, np.integer)):
            return int(device)
        return None

    @staticmethod
    def list_input_devices() -> List[Dict[str, Any]]:
        """利用可能な入力デバイス一覧を取得する。"""
        import sounddevice as sd

        try:
            devices = sd.query_devices()
            hostapis = sd.query_hostapis()
        except Exception as e:
            logger.warning(f"入力デバイス一覧の取得に失敗: {e}")
            return []

        results: List[Dict[str, Any]] = []
        for index, device in enumerate(devices):
            max_input_channels = int(device.get("max_input_channels", 0))
            if max_input_channels <= 0:
                continue
            name = str(device.get("name", f"Input {index}")).strip() or f"Input {index}"
            hostapi_name = ""
            hostapi_index = device.get("hostapi")
            if isinstance(hostapi_index, int) and 0 <= hostapi_index < len(hostapis):
                hostapi_name = str(hostapis[hostapi_index].get("name", "")).strip()
            label = f"{name} ({hostapi_name})" if hostapi_name else name
            results.append({
                "id": index,
                "name": name,
                "label": label,
                "hostapi": hostapi_name,
                "max_input_channels": max_input_channels,
            })
        return results

    # ------------------------------------------------------------------
    # AudioControl スレッド（PortAudio を触るのはここだけ）
    # ------------------------------------------------------------------

    def _start_control_thread(self) -> None:
        """制御スレッドを起動する。"""
        self._thread = threading.Thread(
            target=self._control_loop,
            args=(self._generation, self._commands),
            daemon=True,
            name=f"AudioControl-{self._generation}",
        )
        self._thread.start()

    def _control_loop(self, my_generation: int, commands: queue.Queue) -> None:
        """コマンドを順次実行するループ。PortAudio 呼び出しはすべてここで直列化される。"""
        while True:
            try:
                cmd, arg = commands.get(timeout=_IDLE_CLOSE_SEC)
            except queue.Empty:
                # アイドルタイムアウト: 録音していなければストリームを完全解放
                if self._generation != my_generation:
                    return
                self._idle_close()
                continue

            # recover() 後の旧世代スレッドは静かに退出（コマンドは新キューに積まれている）
            if self._generation != my_generation:
                return

            self._busy_op = cmd
            self._busy_since = time.monotonic()

            # shutdown はループ脱出を伴うため個別に処理する
            if cmd == "shutdown":
                try:
                    self._close_stream()
                except Exception as e:
                    logger.exception(f"shutdown 時のストリーム解放でエラー: {e}")
                self._busy_op = None
                return

            try:
                if cmd == "start":
                    self._do_start(arg)
                elif cmd == "stop":
                    self._do_stop(arg)
                elif cmd == "reopen_if_idle":
                    self._do_reopen_if_idle()
            except Exception as e:
                logger.exception(f"音声制御コマンド '{cmd}' でエラー: {e}")
            self._busy_op = None

            # recover() が走っていたら退出
            if self._generation != my_generation:
                return

    def _do_start(self, on_started: Optional[Callable[[bool], None]]) -> None:
        """録音を開始する（制御スレッド上）。"""
        if self._recording:
            logger.info("既に録音中です。")
            if on_started:
                on_started(True)
            return

        import sounddevice as sd  # 遅延 import（起動パスから外す）

        ok = False
        try:
            # デバイス設定が変わっていたらストリームを作り直す
            if self._stream is not None and self._stream_device != self._input_device:
                self._close_stream()

            if self._stream is None:
                self._open_stream(sd)

            # ドレイン漏れ対策: セッションを進めてから queue をクリアする
            # （逆順だと、クリア後にゾンビ callback の旧データが混入し得る）
            self._session_id += 1
            self._callback_logged = False
            self._drain_audio()

            self._stream.start()
            self._recording = True
            ok = True
            logger.info(
                f"録音開始 (device={self._stream_device or 'default'}, "
                f"session={self._session_id})"
            )
        except Exception as e:
            logger.error(f"録音開始に失敗: {e}")
            self._close_stream()
        finally:
            if on_started:
                try:
                    on_started(ok)
                except Exception as e:
                    logger.error(f"録音開始コールバックでエラー: {e}")

    def _do_stop(self, on_audio: Callable[[npt.NDArray[np.float32]], None]) -> None:
        """録音を停止して音声データを確定する（制御スレッド上）。"""
        self._pending_stop_cb = on_audio
        audio = np.array([], dtype=np.float32)
        try:
            if self._recording:
                self._recording = False
                self._last_record_end = time.monotonic()
                stream = self._stream
                if stream is not None:
                    # stop() は内部バッファの callback 完了を待つ。
                    # ここで同期的に待つことで録音末尾の取りこぼしを防ぐ
                    # （ブロックするのは制御スレッドのみ。ハングは watchdog が回収）。
                    stream.stop()
            audio = self._drain_audio()
            duration = len(audio) / self.sample_rate if self.sample_rate else 0.0
            logger.info(f"録音停止 (samples={len(audio)}, duration={duration:.2f}s)")
        except Exception as e:
            logger.error(f"録音停止でエラー: {e}")
            # ストリームが不調なら捨てて次回作り直す
            self._close_stream()
            audio = self._drain_audio()
        finally:
            self._pending_stop_cb = None
            try:
                on_audio(audio)
            except Exception as e:
                logger.exception(f"録音停止コールバックでエラー: {e}")

    def _do_reopen_if_idle(self) -> None:
        """録音中でなければストリームを閉じる（デバイス変更の即時反映用）。"""
        if not self._recording:
            self._close_stream()

    def _idle_close(self) -> None:
        """アイドル状態が続いたらストリームを close してマイクを完全解放する。"""
        if self._recording or self._stream is None:
            return
        if time.monotonic() - self._last_record_end >= _IDLE_CLOSE_SEC:
            logger.info("アイドルのためマイクストリームを解放します")
            self._close_stream()

    def _open_stream(self, sd) -> None:
        """ストリームを開く（制御スレッド上）。"""
        callback = self._make_audio_callback(self._session_id + 1)
        stream_kwargs = {
            "samplerate": self.sample_rate,
            "channels": AUDIO_CHANNELS,
            "dtype": AUDIO_DTYPE,
            "callback": callback,
        }
        if self._input_device is not None:
            stream_kwargs["device"] = self._input_device

        try:
            self._stream = sd.InputStream(**stream_kwargs)
            self._stream_device = self._input_device
        except Exception as e:
            if self._input_device is None:
                raise
            # 指定デバイスが消えた場合（抜き差し等）はデフォルトにフォールバック
            logger.warning(
                f"指定入力デバイス({self._input_device})で開けません。"
                f"デフォルトデバイスへフォールバックします: {e}"
            )
            stream_kwargs.pop("device", None)
            self._stream = sd.InputStream(**stream_kwargs)
            self._stream_device = None

    def _close_stream(self) -> None:
        """ストリームを閉じる（制御スレッド上）。ハングしてもブロックは本スレッドのみ。"""
        stream = self._stream
        self._stream = None
        if stream is None:
            return
        try:
            stream.stop()
        except Exception as e:
            logger.warning(f"ストリーム stop() エラー: {e}")
        try:
            stream.close()
            logger.debug("マイクストリームを close しました")
        except Exception as e:
            logger.warning(f"ストリーム close() エラー: {e}")

    # ------------------------------------------------------------------
    # audio callback（PortAudio のコールバックスレッドから呼ばれる）
    # ------------------------------------------------------------------

    def _make_audio_callback(self, my_session: int) -> Callable:
        """セッション識別子付きの audio callback を生成する。

        recover() で leak した旧ストリームの callback はセッション不一致で
        即 return し、新しい録音データを汚染しない。
        """

        def _callback(indata: np.ndarray, frames: int, time_info: Any, status) -> None:
            # 録音中でない・旧セッションの callback はデータを捨てる
            # （ハング中のゾンビ callback による無制限メモリ増加の防止）
            if not self._recording or self._session_id != my_session:
                return

            if status:
                logger.warning(f"音声コールバック ステータス: {status}")

            if not self._callback_logged:
                self._callback_logged = True
                logger.info(f"音声 callback 初回受信 (frames={frames}, session={my_session})")

            # データをコピーしてキューに追加（元バッファは PortAudio が再利用するため）
            self._audio_q.put(indata.copy())

            # ストリーミング送出（Deepgram へ逐次送る。録音ごとに差し替わる）。
            # REST 経路（audio_q）とは独立。フックが無ければ何もしない
            chunk_cb = self.chunk_callback
            if chunk_cb is not None:
                try:
                    chunk_cb(indata.reshape(-1).copy())
                except Exception:
                    pass  # 送出側の例外で録音を止めない

            # HUD 用の音声レベル通知（約 30fps に間引き）
            cb = self._level_callback
            if cb is not None:
                now = time.monotonic()
                if now - self._last_level_time >= _LEVEL_INTERVAL_SEC:
                    self._last_level_time = now
                    # RMS を 0.0-1.0 に正規化（経験的に 0.15 をフルスケールとみなす）
                    level = float(np.sqrt(np.mean(indata ** 2)))
                    try:
                        cb(min(1.0, level / 0.15))
                    except Exception:
                        pass  # HUD 側の例外で録音を止めない

        return _callback

    def _drain_audio(self) -> npt.NDArray[np.float32]:
        """キューに溜まった音声チャンクをすべて回収して 1 本の配列にする。"""
        chunks: List[np.ndarray] = []
        while True:
            try:
                chunks.append(self._audio_q.get_nowait())
            except queue.Empty:
                break
        if not chunks:
            return np.array([], dtype=np.float32)
        try:
            return np.concatenate(chunks, axis=0).flatten()
        except Exception as e:
            logger.error(f"音声データ結合エラー: {e}")
            return np.array([], dtype=np.float32)
