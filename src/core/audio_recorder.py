"""
音声録音モジュール

sounddeviceライブラリを使用してマイクから音声を録音する機能を提供する。
録音データはNumPy配列として返され、Whisperによる文字起こしに使用される。
"""

import queue
import threading
from typing import Any, Dict, List, Optional, Union

import numpy as np
import numpy.typing as npt
import sounddevice as sd

from ..config.constants import SAMPLE_RATE, AUDIO_CHANNELS, AUDIO_DTYPE
from ..utils.logger import get_logger

logger = get_logger(__name__)


class AudioRecorder:
    """
    音声録音を管理するクラス。
    
    sounddeviceを使用して非同期で音声を録音し、
    NumPy配列として取得できる。
    リアルタイムの音声レベル通知機能を提供。
    
    Attributes:
        sample_rate: サンプリングレート（Hz）
        is_recording: 録音中かどうか
    """
    
    def __init__(
        self,
        sample_rate: int = SAMPLE_RATE,
        input_device: Optional[Union[int, str]] = "default"
    ) -> None:
        """
        AudioRecorderを初期化する。
        
        Args:
            sample_rate: サンプリングレート（デフォルト: 16000Hz）
            input_device: 入力デバイス（"default" / デバイスID / デバイス名）
        """
        self.sample_rate = sample_rate
        self._queue: queue.Queue = queue.Queue()  # 録音データを一時保存するキュー
        self._recording = False  # 録音状態フラグ
        self._stream: Optional[sd.InputStream] = None  # 音声入力ストリーム
        self._level_callback: Optional[callable] = None  # 音声レベルコールバック
        self._level_threshold = 0.02  # 音声検出のしきい値
        self._input_device: Optional[Union[int, str]] = None
        # start/stop/_cleanup_stream の競合を防ぐための再入可能ロック
        # （stop 中に start が割り込むと OS マイクが解放されない問題への対策）
        self._lock = threading.RLock()
        # 録音セッション毎に callback の初回受信を1度だけログするためのフラグ
        self._callback_received = False
        # 録音セッション識別子。start() のたびにインクリメントし、
        # 古い PortAudio ストリームのゾンビ callback を弾くために使う。
        # macOS で stream.close() がハング中だと callback は呼ばれ続け、
        # 共通の self._queue に音声が混入して新セッションを汚染するため。
        self._session_id = 0
        self.set_input_device(input_device)

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
        """
        利用可能な入力デバイス一覧を取得する。
        """
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
                "max_input_channels": max_input_channels,
            })

        return results

    def set_input_device(self, device: Any) -> None:
        """
        使用する入力デバイス設定を更新する。
        """
        normalized = self.normalize_device_setting(device)
        self._input_device = normalized

        device_label = "default" if normalized is None else str(normalized)
        logger.info(f"入力デバイス設定: {device_label}")

        if self._recording:
            logger.info("録音中のため、入力デバイス変更は次回録音開始時に適用されます。")

    @property
    def input_device(self) -> Optional[Union[int, str]]:
        """現在の入力デバイス設定を返す。"""
        return self._input_device

    def set_level_callback(self, callback: callable) -> None:
        """
        音声レベルコールバックを設定する。
        
        Args:
            callback: 音声レベル（0.0-1.0）と音声検出フラグを受け取るコールバック
                     callback(level: float, has_voice: bool)
        """
        self._level_callback = callback

    @property
    def is_recording(self) -> bool:
        """録音中かどうかを返す。"""
        return self._recording

    def _make_audio_callback(self, my_session: int) -> "callable":
        """セッション識別子付きの audio callback を生成する。

        各 InputStream に固有のクロージャを渡すことで、
        macOS で stream.close() がハング中でも古い stream の callback を
        セッション ID 不一致で即 return させ、新セッションの queue を汚染させない。

        Args:
            my_session: この callback が属するセッション ID

        Returns:
            sounddevice.InputStream に渡す callback 関数
        """

        def _callback(
            indata: np.ndarray,
            frames: int,
            time_info: Any,
            status: sd.CallbackFlags,
        ) -> None:
            # 旧 stream のゾンビ callback はここで弾く。
            # _session_id は start() で必ずインクリメントされる。
            if self._session_id != my_session:
                return

            if status:
                logger.warning(f"音声コールバック ステータス: {status}")

            # 各セッション最初の callback を 1 度だけログ（実際に I/O が動いている確認）
            if not self._callback_received:
                self._callback_received = True
                logger.info(
                    f"音声 callback 初回受信 "
                    f"(frames={frames}, shape={indata.shape}, dtype={indata.dtype}, session={my_session})"
                )

            # データをコピーしてキューに追加（元データは再利用されるため）
            self._queue.put(indata.copy())

            # 音声レベルを計算してコールバックに通知
            if self._level_callback:
                # RMSで音声レベルを計算
                level = float(np.sqrt(np.mean(indata ** 2)))
                # 正規化（0.0-1.0）- 最大値を0.3程度と仮定
                normalized_level = min(1.0, level / 0.3)
                # しきい値を超えたら音声ありと判定
                has_voice = level > self._level_threshold
                self._level_callback(normalized_level, has_voice)

        return _callback

    def start(self) -> bool:
        """
        録音を開始する。

        Returns:
            成功した場合True、既に録音中の場合False
        """
        with self._lock:
            if self._recording:
                logger.info("既に録音中です。")
                return False

            # 万一前回の stop が中途半端に終わっていてもストリーム残骸を確実に解放
            if self._stream is not None:
                self._cleanup_stream()

            try:
                # キューをクリア & callback 受信フラグをリセット
                self._clear_queue()
                self._callback_received = False

                # セッション ID をインクリメントし、ゾンビ callback を無効化する。
                # 新ストリームには「自分のセッション ID を持った callback」を渡す。
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

                # 音声入力ストリームを作成・開始
                try:
                    self._stream = sd.InputStream(**stream_kwargs)
                except Exception as e:
                    if self._input_device is None:
                        raise

                    logger.warning(
                        f"指定入力デバイス({self._input_device})で録音開始に失敗。"
                        f"デフォルトデバイスへフォールバックします: {e}"
                    )
                    stream_kwargs.pop("device", None)
                    self._stream = sd.InputStream(**stream_kwargs)

                self._stream.start()
                self._recording = True
                device_label = "default" if self._input_device is None else str(self._input_device)
                logger.info(
                    f"録音開始... (input_device={device_label}, stream_id={id(self._stream)})"
                )
                return True

            except Exception as e:
                logger.error(f"録音開始に失敗: {e}")
                self._cleanup_stream()
                self._recording = False
                return False

    def stop(self) -> npt.NDArray[np.float32]:
        """
        録音を停止し、音声データを返す。

        Returns:
            録音した音声データ（float32のNumPy配列）
        """
        with self._lock:
            if not self._recording and self._stream is None:
                return np.array([], dtype=np.float32)

            # フラグ解除を先に行うことで、callback の以降の発火を抑制する
            # （ストリーム close の前にコールバック側で何か処理する場合に備える）
            self._recording = False
            self._cleanup_stream()
            queue_items = self._queue.qsize()
            audio_data = self._collect_audio_data()
            duration = len(audio_data) / self.sample_rate if self.sample_rate else 0.0
            logger.info(
                f"録音停止。 (queue_items={queue_items}, samples={len(audio_data)}, duration={duration:.2f}s, callback_received={self._callback_received})"
            )

            return audio_data

    def _clear_queue(self) -> None:
        """キューをクリアする。"""
        with self._queue.mutex:
            self._queue.queue.clear()

    # PortAudio (CoreAudio) の close が macOS で固まることがあるため、
    # ここで指定した時間を超えたら諦めてバックグラウンドに任せる。
    _CLEANUP_TIMEOUT_SEC: float = 2.0

    def _cleanup_stream(self) -> None:
        """音声ストリームをクリーンアップする。

        macOS の PortAudio で stream.stop()/close() が稀にハングし、
        呼び出し元のロックを巻き込んで全体フリーズに至るため、
        実体の close は別スレッドへ逃がし最大 _CLEANUP_TIMEOUT_SEC まで待つ。
        タイムアウト時は self._stream を None に切り替えて先に進める
        （バックグラウンドの close は継続。OS マイクが短時間残る場合あり）。
        """
        with self._lock:
            stream = self._stream
            if stream is None:
                return
            # 参照を先に切る。以後の start()/stop() は新しいストリーム前提で進む。
            self._stream = None

        def _close_in_background() -> None:
            try:
                stream.stop()
            except Exception as e:
                logger.error(f"ストリーム stop() エラー: {e}")
            try:
                stream.close()
            except Exception as e:
                logger.error(f"ストリーム close() エラー: {e}")

        cleanup_thread = threading.Thread(
            target=_close_in_background,
            daemon=True,
            name="AudioStreamCleanup",
        )
        cleanup_thread.start()
        cleanup_thread.join(timeout=self._CLEANUP_TIMEOUT_SEC)
        if cleanup_thread.is_alive():
            logger.warning(
                f"ストリームの close が {self._CLEANUP_TIMEOUT_SEC}s 以内に完了しませんでした。"
                "バックグラウンドで継続します（OS マイクが一時的に残る場合あり）。"
            )

    def _collect_audio_data(self) -> npt.NDArray[np.float32]:
        """
        キューから全ての音声データを収集する。
        
        Returns:
            結合された音声データ（1次元配列）
        """
        data_list: List[np.ndarray] = []
        
        # キューから全データを取得
        while not self._queue.empty():
            data_list.append(self._queue.get())
            
        if not data_list:
            return np.array([], dtype=np.float32)
            
        try:
            # 全データを結合して1次元配列に変換
            audio_data = np.concatenate(data_list, axis=0)
            return audio_data.flatten()
        except Exception as e:
            logger.error(f"音声データ処理エラー: {e}")
            return np.array([], dtype=np.float32)

    # 後方互換性のためのエイリアス
    def start_recording(self) -> bool:
        """start()のエイリアス。"""
        return self.start()

    def stop_recording(self) -> npt.NDArray[np.float32]:
        """stop()のエイリアス。"""
        return self.stop()
