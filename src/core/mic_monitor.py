"""
マイク入力レベルのモニタリング（録音せずにレベルだけを測る）

セットアップガイドの「マイクテスト」や設定画面の「マイクテスト」で、
録音キーを押さずにマイクが拾えているかを確認するための軽量モニタ。

録音経路（AudioRecorder のスレッド／セッション機構）には一切触れず、
独自の sounddevice InputStream を開いて RMS を計算するだけなので、
**文字起こしは走らず、無料枠も消費しない**。

レベルは PortAudio のコールバックスレッドで更新されるため、UI 側は
QTimer で read_level() をポーリングして描画する（クロススレッドの
シグナルを使わず、GIL 下の float 読み書きだけで完結させる）。
"""

from typing import Any, Optional, Union

import numpy as np

from ..config.constants import AUDIO_CHANNELS, AUDIO_DTYPE, SAMPLE_RATE
from ..utils.logger import get_logger
from .audio_recorder import AudioRecorder

logger = get_logger(__name__)


class MicLevelMonitor:
    """録音せずにマイクの入力レベル（0.0-1.0）だけを測る軽量モニタ。

    Attributes:
        _stream: sounddevice の InputStream（未起動時は None）。
        _level: 直近の入力レベル（0.0-1.0・PortAudio スレッドが更新）。
    """

    def __init__(self) -> None:
        self._stream: Optional[Any] = None
        self._level: float = 0.0

    def start(self, device: Any = "default") -> bool:
        """モニタリングを開始する（既に動いていれば一度停止してから開き直す）。

        Args:
            device: 設定値（"default" / デバイス ID / デバイス名）。

        Returns:
            開始に成功したら True（デバイスが無い等で失敗しても例外は投げずに False）。
        """
        self.stop()
        try:
            import sounddevice as sd
        except Exception as e:  # sounddevice 未導入・PortAudio 未初期化など
            logger.warning(f"sounddevice の読み込みに失敗（マイクテスト無効）: {e}")
            return False

        dev = AudioRecorder.normalize_device_setting(device)

        def _callback(indata: np.ndarray, frames: int, time_info: Any, status: Any) -> None:
            # RMS を 0.0-1.0 に正規化（録音時の HUD と同じく 0.15 をフルスケールとみなす）
            try:
                level = float(np.sqrt(np.mean(indata ** 2)))
                self._level = min(1.0, level / 0.15)
            except Exception:
                pass  # 測定側の例外はモニタを止めない

        kwargs = {
            "samplerate": SAMPLE_RATE,
            "channels": AUDIO_CHANNELS,
            "dtype": AUDIO_DTYPE,
            "callback": _callback,
        }
        if dev is not None:
            kwargs["device"] = dev
        try:
            self._stream = sd.InputStream(**kwargs)
            self._stream.start()
            logger.info(f"マイクモニタ開始 (device={dev or 'default'})")
            return True
        except Exception as e:
            # 指定デバイスが開けない場合はデフォルトへフォールバックを一度試す
            if dev is not None:
                try:
                    kwargs.pop("device", None)
                    self._stream = sd.InputStream(**kwargs)
                    self._stream.start()
                    logger.warning(f"指定デバイスで開けずデフォルトへフォールバック: {e}")
                    return True
                except Exception as e2:
                    logger.warning(f"マイクモニタの開始に失敗: {e2}")
            else:
                logger.warning(f"マイクモニタの開始に失敗: {e}")
            self._stream = None
            return False

    def read_level(self) -> float:
        """直近の入力レベル（0.0-1.0）を返す。"""
        return self._level

    @property
    def is_running(self) -> bool:
        """モニタリング中かどうか。"""
        return self._stream is not None

    def stop(self) -> None:
        """モニタリングを停止してストリームを閉じる（未起動なら何もしない）。"""
        stream = self._stream
        self._stream = None
        self._level = 0.0
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception as e:
                logger.warning(f"マイクモニタの停止エラー: {e}")
