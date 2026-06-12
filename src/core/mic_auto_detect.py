"""
マイク自動検出モジュール

「自動検出」ボタンを押した後にユーザーが喋ると、全入力デバイスを同時監視して
声が入っているマイクを自動選択するための検出処理を提供する。

判定アルゴリズム:
    各デバイスで約 4 秒間、30ms フレームごとの RMS を収集し、
    score = RMS の 90 パーセンタイル − 10 パーセンタイル とする。
    喋り声は音量変動が大きく（スコア大）、定常ノイズやループバック系の
    仮想デバイスは変動が小さい（スコア小）ため、単純な最大 RMS より誤選択が少ない。

設計:
    使い捨てスレッドで全デバイスに sd.InputStream を同時オープンする。
    AudioRecorder の永続ストリーム・制御スレッドには一切触れない。
    開けないデバイスはスキップする。
"""

import threading
import time
from typing import Any, Callable, Dict, List, Optional

import numpy as np

from ..utils.logger import get_logger

logger = get_logger(__name__)

# RMS を集計するフレーム長（ミリ秒）
FRAME_MS = 30

# 監視時間（秒）。ボタンを押してから喋り終わるまでの猶予
DEFAULT_DURATION_SEC = 4.0

# 発話ありとみなすスコア（p90 − p10）の下限。
# 定常ノイズのみのデバイスは概ね 0.001 未満、マイクへの発話は 0.01 以上になる
SCORE_THRESHOLD = 0.005


def compute_score(rms_values: List[float]) -> float:
    """
    RMS フレーム列から発話らしさスコア（p90 − p10）を計算する。

    Args:
        rms_values: 30ms フレームごとの RMS 値

    Returns:
        スコア。フレームが 10 未満（判定不能）の場合は 0.0
    """
    if len(rms_values) < 10:
        return 0.0
    arr = np.asarray(rms_values, dtype=np.float32)
    return float(np.percentile(arr, 90) - np.percentile(arr, 10))


def frame_rms(samples: np.ndarray, frame_len: int) -> List[float]:
    """
    サンプル列を frame_len ごとのフレームに切り、各フレームの RMS を返す。

    Args:
        samples: float32 のモノラルサンプル列
        frame_len: 1 フレームのサンプル数

    Returns:
        各フレームの RMS（端数サンプルは捨てる）
    """
    n = len(samples) // frame_len
    if n == 0:
        return []
    frames = samples[: n * frame_len].reshape(n, frame_len).astype(np.float32)
    return np.sqrt(np.mean(frames * frames, axis=1)).tolist()


class _DeviceProbe:
    """1 デバイス専用の使い捨て InputStream。コールバックで RMS フレームを貯める。"""

    def __init__(self, device: Dict[str, Any], samplerate: float) -> None:
        self.device = device
        self.frame_len = max(1, int(samplerate * FRAME_MS / 1000))
        self.samplerate = samplerate
        self.rms: List[float] = []
        # フレーム長に満たない端数サンプルの持ち越し
        self._carry = np.empty(0, dtype=np.float32)
        self._lock = threading.Lock()
        self.stream: Optional[Any] = None

    def callback(self, indata, frames, time_info, status) -> None:
        """sd.InputStream のコールバック（audio スレッドから呼ばれる）。"""
        mono = indata[:, 0] if indata.ndim > 1 else indata
        with self._lock:
            data = np.concatenate([self._carry, mono.astype(np.float32, copy=False)])
            n = len(data) // self.frame_len
            if n:
                self.rms.extend(frame_rms(data[: n * self.frame_len], self.frame_len))
                self._carry = data[n * self.frame_len:]
            else:
                self._carry = data

    def score(self) -> float:
        """これまでに集めた RMS フレームのスコアを返す。"""
        with self._lock:
            return compute_score(self.rms)


def detect_speaking_device(
    duration: float = DEFAULT_DURATION_SEC,
) -> Optional[Dict[str, Any]]:
    """
    全入力デバイスを duration 秒監視し、発話が検出されたデバイスを返す（ブロッキング）。

    使い捨てスレッド（detect_async）から呼ぶ想定。

    Args:
        duration: 監視時間（秒）

    Returns:
        スコア最大のデバイス情報 {"id", "name", "label", "score"}。
        どのデバイスもしきい値未満なら None
    """
    import sounddevice as sd

    from .audio_recorder import AudioRecorder

    candidates = AudioRecorder.list_input_devices()
    if not candidates:
        logger.warning("入力デバイスが見つかりません")
        return None

    probes: List[_DeviceProbe] = []
    for dev in candidates:
        try:
            info = sd.query_devices(dev["id"])
            samplerate = float(info.get("default_samplerate") or 16000)
            probe = _DeviceProbe(dev, samplerate)
            stream = sd.InputStream(
                device=dev["id"],
                channels=1,
                samplerate=samplerate,
                dtype="float32",
                callback=probe.callback,
            )
            stream.start()
            probe.stream = stream
            probes.append(probe)
        except Exception as e:
            # 排他使用中・無効な構成のデバイスは監視対象から外すだけ
            logger.info(f"自動検出スキップ（開けないデバイス）: {dev['label']}: {e}")

    if not probes:
        logger.warning("監視できる入力デバイスがありません")
        return None

    # 使い捨てスレッド内なので sleep でよい（UI はメインスレッドで進捗表示中）
    time.sleep(duration)

    # スコアはストリーム破棄より先に確定する。WASAPI 等で stop()/close() が
    # ハングするデバイスがあると、後置だと結果通知（on_done → UI 復帰）まで
    # 道連れになり「検出中…」のまま固まるため
    best: Optional[Dict[str, Any]] = None
    for probe in probes:
        score = probe.score()
        logger.info(f"自動検出スコア {probe.device['label']}: {score:.4f}")
        if score >= SCORE_THRESHOLD and (best is None or score > best["score"]):
            best = {
                "id": probe.device["id"],
                "name": probe.device["name"],
                "label": probe.device["label"],
                "score": score,
            }

    # ストリーム破棄は別デーモンスレッドで行う（ハングしても結果には影響しない）
    def _teardown() -> None:
        for probe in probes:
            try:
                probe.stream.stop()
                probe.stream.close()
            except Exception:
                pass

    threading.Thread(target=_teardown, daemon=True, name="MicAutoDetectTeardown").start()
    return best


def detect_async(
    on_done: Callable[[Optional[Dict[str, Any]]], None],
    duration: float = DEFAULT_DURATION_SEC,
) -> threading.Thread:
    """
    マイク自動検出を使い捨てスレッドで実行する。

    Args:
        on_done: 完了コールバック（ワーカースレッドから呼ばれる。
                 UI 更新は Qt シグナル経由でメインスレッドへ渡すこと）
        duration: 監視時間（秒）

    Returns:
        開始済みのワーカースレッド
    """

    def _run() -> None:
        result = None
        try:
            result = detect_speaking_device(duration)
        except Exception as e:
            logger.error(f"マイク自動検出に失敗: {e}")
        on_done(result)

    thread = threading.Thread(target=_run, daemon=True, name="MicAutoDetect")
    thread.start()
    return thread
