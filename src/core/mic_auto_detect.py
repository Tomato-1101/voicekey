"""
マイク自動検出モジュール

「自動検出」ボタンを押した後にユーザーが喋ると、入力デバイスを順番に監視して
声が入っているマイクを自動選択するための検出処理を提供する。

判定アルゴリズム:
    各デバイスで短時間、30ms フレームごとの RMS を収集し、
    score = RMS の 90 パーセンタイル − 10 パーセンタイル とする。
    喋り声は音量変動が大きく（スコア大）、定常ノイズやループバック系の
    仮想デバイスは変動が小さい（スコア小）ため、単純な最大 RMS より誤選択が少ない。

設計（2026-06-14 改訂・Windows の OS クラッシュ(WDF) 対策）:
    入力デバイスを「1 台ずつ順番に」開いてプローブする。同時に開く
    sd.InputStream は常に最大 1 つ。
    旧実装は全デバイスに sd.InputStream を一斉に同時オープンしていたが、
    Windows では同じ物理マイクが host API ごと（特にカーネルストリーミングの
    WDM-KS）に重複列挙され、それらを同時に開くと音声ドライバが WDF レベルで
    クラッシュして OS が再起動する事故があった。
    対策は次の 3 点:
      1. 同時オープンの廃止（順次プローブ）。同時に開くストリームは常に 1 つ。
      2. 危険な host API（WDM-KS / ASIO）と同名重複の除外（Windows）。
         カーネルを直接叩く API を自動検出では触らない。
      3. 開く前に sd.check_input_settings で構成を検証し、非対応なら開かない。
    AudioRecorder の永続ストリーム・制御スレッドには一切触れない。
"""

import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional

import numpy as np

from ..utils.logger import get_logger

logger = get_logger(__name__)

# RMS を集計するフレーム長（ミリ秒）
FRAME_MS = 30

# 監視時間の目安（秒）。1 台あたりの時間はこれを台数で割り、下記の上下限でクランプする
DEFAULT_DURATION_SEC = 4.0

# 1 台あたりのプローブ時間の下限・上限（秒）。
# 順次プローブなのでユーザーには「喋り続けてください」と表示する。
MIN_PROBE_SEC = 0.8
MAX_PROBE_SEC = 1.5

# 発話ありとみなすスコア（p90 − p10）の下限。
# 定常ノイズのみのデバイスは概ね 0.001 未満、マイクへの発話は 0.01 以上になる
SCORE_THRESHOLD = 0.005

# 自動検出で「絶対に開かない」host API（名前の部分一致・小文字比較）。
# WDM-KS はカーネルストリーミングで WDF ドライバを直接叩き、ASIO は排他デバイスを
# 強制オープンするため、自動検出のような総当りオープンと相性が悪く OS を巻き込む。
_UNSAFE_HOSTAPIS = ("wdm-ks", "asio")


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


def _is_unsafe_hostapi(name: str) -> bool:
    """自動検出で開いてはいけない host API（WDM-KS / ASIO）か判定する。"""
    lowered = (name or "").lower()
    return any(tag in lowered for tag in _UNSAFE_HOSTAPIS)


def _safe_probe_candidates(devices: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    自動検出で安全にプローブできるデバイスへ絞り込む。

    - Windows: WDM-KS / ASIO（カーネルストリーミング・排他系）を除外する。
      これらを開くとドライバが WDF クラッシュを起こし OS が再起動することがある。
    - 同一物理デバイス名の重複（host API 違いの重複列挙）は最初の 1 つに集約し、
      同じマイクを何度も開かない。

    Args:
        devices: AudioRecorder.list_input_devices() の戻り（"name"/"hostapi" を含む）

    Returns:
        プローブ対象として安全なデバイスのリスト
    """
    is_windows = sys.platform.startswith("win")
    seen_names = set()
    result: List[Dict[str, Any]] = []
    for dev in devices:
        if is_windows and _is_unsafe_hostapi(dev.get("hostapi", "")):
            logger.info(f"自動検出から除外（危険な host API）: {dev.get('label')}")
            continue
        name = dev.get("name", "")
        if name in seen_names:
            continue
        seen_names.add(name)
        result.append(dev)
    return result


def _probe_one_device(sd, dev: Dict[str, Any], probe_sec: float) -> Optional[float]:
    """
    1 デバイスだけを開いて probe_sec 秒 RMS を集め、スコアを返す。

    同時に開くストリームは常に 1 つ（呼び出し側が直列に呼ぶ）。開く前に
    check_input_settings で構成を検証し、非対応・開けないデバイスは触らずスキップ
    する。次のデバイスを開く前に必ず stop/close で閉じる（同時オープンを作らない）。

    Args:
        sd: sounddevice モジュール
        dev: プローブ対象デバイス
        probe_sec: 監視時間（秒）

    Returns:
        スコア。情報取得不可・構成非対応・開けない場合は None
    """
    try:
        info = sd.query_devices(dev["id"])
        samplerate = float(info.get("default_samplerate") or 16000)
    except Exception as e:
        logger.info(f"自動検出スキップ（情報取得不可）: {dev['label']}: {e}")
        return None

    # 実際に開く前に構成を検証する。ここで弾ければ危険な open 自体を回避できる
    try:
        sd.check_input_settings(
            device=dev["id"], channels=1, samplerate=samplerate, dtype="float32"
        )
    except Exception as e:
        logger.info(f"自動検出スキップ（構成非対応）: {dev['label']}: {e}")
        return None

    probe = _DeviceProbe(dev, samplerate)
    stream = None
    try:
        stream = sd.InputStream(
            device=dev["id"],
            channels=1,
            samplerate=samplerate,
            dtype="float32",
            callback=probe.callback,
        )
        stream.start()
        time.sleep(probe_sec)
    except Exception as e:
        logger.info(f"自動検出スキップ（開けないデバイス）: {dev['label']}: {e}")
        return None
    finally:
        # 次のデバイスを開く前に必ず閉じる。これを怠ると順次プローブの意味がなくなる
        if stream is not None:
            try:
                stream.stop()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass

    return probe.score()


def detect_speaking_device(
    duration: float = DEFAULT_DURATION_SEC,
) -> Optional[Dict[str, Any]]:
    """
    入力デバイスを 1 台ずつ順番に監視し、発話が検出されたデバイスを返す（ブロッキング）。

    使い捨てスレッド（detect_async）から呼ぶ想定。同時に開く InputStream は常に
    最大 1 つで、Windows でのドライバ WDF クラッシュ（OS 再起動）を避ける。

    Args:
        duration: 全体の監視時間の目安（秒）。1 台あたりはこれを台数で割って使う。

    Returns:
        スコア最大のデバイス情報 {"id", "name", "label", "score"}。
        どのデバイスもしきい値未満なら None
    """
    import sounddevice as sd

    from .audio_recorder import AudioRecorder

    candidates = _safe_probe_candidates(AudioRecorder.list_input_devices())
    if not candidates:
        logger.warning("監視できる入力デバイスがありません")
        return None

    # 1 台あたりのプローブ時間（全体を台数で割り、上下限でクランプ）
    per_device = min(MAX_PROBE_SEC, max(MIN_PROBE_SEC, duration / len(candidates)))

    best: Optional[Dict[str, Any]] = None
    for dev in candidates:
        score = _probe_one_device(sd, dev, per_device)
        if score is None:
            continue
        logger.info(f"自動検出スコア {dev['label']}: {score:.4f}")
        if score >= SCORE_THRESHOLD and (best is None or score > best["score"]):
            best = {
                "id": dev["id"],
                "name": dev["name"],
                "label": dev["label"],
                "score": score,
            }

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
