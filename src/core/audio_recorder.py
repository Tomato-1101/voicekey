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


class _StopCompletion:
    """録音停止の完了コールバックを録音ごとに exactly once 発火させるワンショットガード。

    通常停止（_do_stop）とハング時の代行発火（recover）が競合しても、最初の 1 回だけ
    バッファをドレインして実コールバックを呼ぶ。これにより、停止がハングして recover が
    代行発火した後に古い停止スレッドが復帰しても、同じ録音でコールバックが 2 回呼ばれて
    App 側の未完了タスク数（_outstanding）が負数になる不具合を防ぐ。

    確定音声はこのガード内でドレインするため、勝った側が必ず実データを配り、負けた側は
    何もしない（空音声で上書きしない）。
    """

    def __init__(
        self,
        callback: Callable[[npt.NDArray[np.float32]], None],
        drain: Callable[[], npt.NDArray[np.float32]],
    ) -> None:
        self._callback = callback
        self._drain = drain
        self._fired = False
        self._lock = threading.Lock()

    def fire(self) -> Optional[npt.NDArray[np.float32]]:
        """未発火なら drain→callback を 1 回だけ実行し確定音声を返す。発火済みなら None。"""
        with self._lock:
            if self._fired:
                return None
            self._fired = True
            callback, drain = self._callback, self._drain
            self._callback = None  # 参照を切って二重実行と GC 滞留を避ける
            self._drain = None
        audio = drain()
        callback(audio)
        return audio


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
        # ストリーミング送出フックを録音世代に束縛して保持する（連続録音間の音声混入防止）。
        # (gen, callback) を 1 タプルで原子的に差し替え、audio callback は世代不一致を拒否する。
        # 旧録音の stop ドレイン中に次録音が別 streamer を差し替えても、ドレイン中に受理する
        # 世代（_active_chunk_gen）は進んでいない（_do_start でのみ進む）ため、旧録音末尾の
        # チャンクが次録音の streamer へ流れ込まない。
        self._record_gen = 0                  # set_chunk_callback の採番（listener スレッドのみが進める）
        self._active_chunk_gen = 0            # 現在の物理録音が受理する世代（_do_start で確定）
        self._chunk_entry: tuple = (0, None)  # (gen, callback)。callback が None なら送出しない
        if chunk_callback is not None:
            self.set_chunk_callback(chunk_callback)
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

    def set_chunk_callback(self, callback: Optional[Callable[[npt.NDArray[np.float32]], None]]) -> int:
        """ストリーミング送出フックを新しい録音世代で登録／解除する（listener スレッドから）。

        登録（callback あり）のたびに録音世代を 1 つ進めて束縛する。次の物理録音が
        `_do_start` でこの世代を受理世代として確定するまで、旧録音の stop ドレイン中に
        差し替えても旧 audio callback には拾われない（世代不一致で拒否）。

        Args:
            callback: 生 PCM チャンク（float32 モノラル）を受け取る関数。None で解除。

        Returns:
            登録した録音世代。解除時は 0。
        """
        if callback is None:
            # 解除は callback を None にするだけ（世代は次の登録で進む）
            self._chunk_entry = (0, None)
            return 0
        # 採番は listener スレッドのみが行うのでロック不要。タプル代入は GIL 下で原子的
        self._record_gen += 1
        self._chunk_entry = (self._record_gen, callback)
        return self._record_gen

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

            # 完了待ちだった stop コールバックには「ここまでに取れた音声」を渡す。
            # _StopCompletion はこの時点までの旧キューを束縛済みなので、下で新世代に
            # 別キューを割り当てても旧録音の確定音声は失われない。
            pending = self._pending_stop_cb
            self._pending_stop_cb = None

            # 新世代には別バッファを割り当て、旧世代のドレインと干渉させない
            # （古い停止スレッドが復帰しても新世代の音声を奪えない）
            self._audio_q = queue.Queue()
            # コマンドキューを作り直す（旧スレッドが復帰しても新コマンドを食わせない）
            self._commands = queue.Queue()
            self._start_control_thread()

        # 完了発火はロック外で行う（コールバック内で再入しても自己デッドロックしない）。
        # fire() はワンショットなので、復帰した古い _do_stop と競合しても 1 回だけ実行される。
        if pending is not None:
            try:
                pending.fire()
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
                    self._do_stop(arg, my_generation)
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

            # 直前の残データを除去してから録音を始める。
            # session_id は「ストリーム世代」を表し、ストリームを開くときだけ進める
            # （_open_stream 参照）。録音のたびには進めない——進めてしまうと、開いた
            # ときに採番された callback の my_session と食い違い、2 回目以降の録音で
            # callback が全データを破棄してしまう（永続ストリームの録音不能バグ）。
            # leak したゾンビ callback は recover() 時の世代繰り上げで弾かれるため、
            # ここではクリアのみで足りる。
            self._callback_logged = False
            self._drain_audio()

            # この物理録音が受理するストリーミング世代を確定する（stream.start より前）。
            # 旧録音の stop ドレイン中はこの値が進まないため、ドレイン中に差し替えられた
            # 次録音の streamer（より新しい世代）には旧音声が渡らない。
            self._active_chunk_gen = self._chunk_entry[0]

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

    def _do_stop(
        self,
        on_audio: Callable[[npt.NDArray[np.float32]], None],
        my_generation: Optional[int] = None,
    ) -> None:
        """録音を停止して音声データを確定する（制御スレッド上）。

        Args:
            on_audio: 確定音声を受け取るコールバック（この録音で exactly once 呼ばれる）。
            my_generation: 呼び出し元制御スレッドの世代。停止処理中（stream.stop() の
                ハング中など）に recover() が世代を進めた場合、新世代の録音状態・ストリームを
                破壊しないために使う。None なら世代チェックを省く（単体テスト用）。
        """
        # この録音世代のバッファを束縛する。recover() は新世代に別キューを割り当てるため、
        # ハングから復帰した本スレッドが新世代の音声を奪う/汚すことはない。完了発火と
        # ドレインを _StopCompletion に閉じ込め、recover の代行発火と競合しても exactly once。
        q = self._audio_q
        completion = _StopCompletion(on_audio, lambda: self._drain_queue(q))
        self._pending_stop_cb = completion
        try:
            if self._recording:
                self._last_record_end = time.monotonic()
                stream = self._stream
                if stream is not None:
                    # stop() は内部バッファの callback 完了を待つ。録音末尾の取りこぼしを防ぐため
                    # _recording は stop() の後で False にする（待っている間に届く最後のチャンクも
                    # callback が拾えるようにする）。ブロックするのは制御スレッドのみ（ハングは
                    # watchdog → recover() が回収）。
                    stream.stop()
                # ハング中に recover() が世代を進めていたら、新世代の _recording / _stream へ
                # 触れずに撤退する（自分のバッファ q からの確定は finally の completion が行う）
                if my_generation is None or self._generation == my_generation:
                    self._recording = False
        except Exception as e:
            logger.error(f"録音停止でエラー: {e}")
            # ストリームが不調なら捨てて次回作り直す。ただし自世代のときだけ
            # （recover 後なら新世代のストリームを閉じてはならない）
            if my_generation is None or self._generation == my_generation:
                self._close_stream()
        finally:
            # 自分が積んだガードだけを片付ける（recover 後に新世代が積んだものは消さない）
            if self._pending_stop_cb is completion:
                self._pending_stop_cb = None
            audio = None
            try:
                audio = completion.fire()  # recover が先に発火済みなら None（二重発火しない）
            except Exception as e:
                logger.exception(f"録音停止コールバックでエラー: {e}")
            if audio is not None:
                duration = len(audio) / self.sample_rate if self.sample_rate else 0.0
                logger.info(f"録音停止 (samples={len(audio)}, duration={duration:.2f}s)")

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
        # 新しいストリーム世代を採番し、その callback だけを有効にする。
        # この session_id はストリームの寿命の間ずっと有効で、録音の start/stop を
        # 繰り返しても変わらない。leak した旧ストリームの callback は古い session の
        # ままなので、recover() で世代が繰り上がった後は自動的に弾かれる。
        self._session_id += 1
        callback = self._make_audio_callback(self._session_id)
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
            # REST 経路（audio_q）とは独立。世代が一致しないチャンクは拒否する
            # （旧録音の stop ドレイン中に差し替えられた次録音の streamer へ混入させない）。
            # (gen, cb) を 1 タプルで原子読みして cb/gen の不整合読みを避ける
            gen, chunk_cb = self._chunk_entry
            if chunk_cb is not None and gen == self._active_chunk_gen:
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
        """現在のキューに溜まった音声チャンクを回収する（_do_start のクリア等で使用）。"""
        return self._drain_queue(self._audio_q)

    def _drain_queue(self, q: queue.Queue) -> npt.NDArray[np.float32]:
        """指定キューに溜まった音声チャンクをすべて回収して 1 本の配列にする。

        録音世代ごとに別キューを束縛できるよう、対象キューを引数で受け取る
        （recover() 後に古い停止スレッドが新世代のキューを誤ってドレインしないため）。
        """
        chunks: List[np.ndarray] = []
        while True:
            try:
                chunks.append(q.get_nowait())
            except queue.Empty:
                break
        if not chunks:
            return np.array([], dtype=np.float32)
        try:
            return np.concatenate(chunks, axis=0).flatten()
        except Exception as e:
            logger.error(f"音声データ結合エラー: {e}")
            return np.array([], dtype=np.float32)
