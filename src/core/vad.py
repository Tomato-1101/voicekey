"""
音声活性検出（VAD）モジュール

Silero VAD の ONNX モデルを onnxruntime で直接実行する。
旧実装は torch + MPS を使っていたが、
- torch のトップレベル import が起動を数百 ms〜数秒ブロックする
- Silero VAD は小さなモデルのため MPS は CPU より約 8 倍遅い（実測）
の 2 点から、onnxruntime(CPU) + numpy のみの実装に置き換えた。

モデルファイルは pip の silero-vad パッケージに同梱されている
silero_vad.onnx を import せずにパス解決して利用する
（silero_vad を import すると torch が読み込まれてしまうため）。
"""

import importlib.util
import os
import threading
from typing import Optional, Tuple

import numpy as np
import numpy.typing as npt

from ..utils.logger import get_logger

logger = get_logger(__name__)

# Silero VAD v5 (16kHz) のフレーム仕様
_FRAME_SAMPLES = 512      # 1 フレーム = 512 サンプル（32ms @16kHz）
_CONTEXT_SAMPLES = 64     # 各フレームの先頭に前フレーム末尾 64 サンプルを連結する
_SPEECH_THRESHOLD = 0.5   # 発話とみなす確率しきい値（公式デフォルトと同じ）
# 発話とみなす連続フレームの最小長。離れた単発クリックノイズ（1 フレームだけ
# しきい値超え）を発話としないため、総数ではなく「連続 run の長さ」で判定する。
_MIN_SPEECH_FRAMES = 2    # 連続 2 フレーム（≒64ms）以上で初めて発話とみなす

# 発話間に保持する無音の最大長（秒）。
# ポーズは句読点・文区切りの推定材料になるため完全には消さない
_KEPT_GAP_SEC = 0.5

# 分割並列送信用の定数
_SPLIT_GAP_SEC = 0.7      # この長さを超える無音をセグメント境界にする（秒）
_MIN_SPLIT_SEC = 12.0     # 全体がこれ未満なら分割しない（秒）
_MIN_SEGMENT_SEC = 2.0    # これ未満のセグメントは直前に結合する（秒）


def _find_model_path() -> Optional[str]:
    """silero-vad パッケージ同梱の ONNX モデルパスを import せずに解決する。"""
    spec = importlib.util.find_spec("silero_vad")
    if spec is None or not spec.origin:
        return None
    path = os.path.join(os.path.dirname(spec.origin), "data", "silero_vad.onnx")
    return path if os.path.exists(path) else None


class SileroVad:
    """
    Silero VAD（ONNX 版）による発話検出。

    スレッドセーフ（推論はロックで直列化）。アプリ全体で 1 インスタンスを
    共有する想定（旧実装はスロットごとに二重ロードしていた）。

    Attributes:
        min_silence_duration_ms: 発話区間を分割する最小無音時間（trim 用パディングに使用）
    """

    def __init__(self, min_silence_duration_ms: int = 500) -> None:
        self.min_silence_duration_ms = min_silence_duration_ms
        self._session = None          # onnxruntime.InferenceSession（遅延ロード）
        self._load_failed = False     # ロード失敗を記憶し、毎回の再試行を避ける
        self._lock = threading.Lock()

    def _load_session(self) -> bool:
        """ONNX セッションを遅延ロードする。失敗時は False（VAD 無効で続行）。"""
        if self._session is not None:
            return True
        if self._load_failed:
            return False
        try:
            # onnxruntime はここで初めて import する（起動パスに乗せない）
            import onnxruntime as ort

            model_path = _find_model_path()
            if model_path is None:
                raise FileNotFoundError("silero-vad パッケージの ONNX モデルが見つかりません")

            opts = ort.SessionOptions()
            # VAD は小モデルなのでスレッドを増やしても速くならない（公式設定に合わせる）
            opts.inter_op_num_threads = 1
            opts.intra_op_num_threads = 1
            self._session = ort.InferenceSession(
                model_path, providers=["CPUExecutionProvider"], sess_options=opts
            )
            logger.info(f"Silero VAD (ONNX) をロードしました: {model_path}")
            return True
        except Exception as e:
            # VAD が使えなくても文字起こし自体は続行できるよう、安全側に倒す
            self._load_failed = True
            logger.error(f"VAD モデルのロードに失敗（VAD 無効で続行）: {e}")
            return False

    def preload(self) -> None:
        """起動時に呼んで初回録音時のロード遅延（数十 ms）を避ける。"""
        with self._lock:
            self._load_session()

    def _frame_probs(self, audio: npt.NDArray[np.float32]) -> np.ndarray:
        """音声全体をフレーム分割し、フレームごとの発話確率を返す。

        呼び出し側で self._lock を保持していること。
        """
        # フレーム長の倍数になるよう末尾をゼロ詰め
        n = len(audio)
        pad = (-n) % _FRAME_SAMPLES
        if pad:
            audio = np.concatenate([audio, np.zeros(pad, dtype=np.float32)])

        state = np.zeros((2, 1, 128), dtype=np.float32)
        context = np.zeros((1, _CONTEXT_SAMPLES), dtype=np.float32)
        sr = np.array(16000, dtype=np.int64)

        probs = []
        for i in range(0, len(audio), _FRAME_SAMPLES):
            frame = audio[i:i + _FRAME_SAMPLES].reshape(1, -1)
            x = np.concatenate([context, frame], axis=1)
            out, state = self._session.run(None, {"input": x, "state": state, "sr": sr})
            context = x[:, -_CONTEXT_SAMPLES:]
            probs.append(float(out[0, 0]))
        return np.array(probs, dtype=np.float32)

    def analyze(
        self, audio: npt.NDArray[np.float32], pad_ms: int = 250
    ) -> Tuple[bool, Optional[npt.NDArray[np.float32]]]:
        """
        1 回の推論で「発話の有無」と「無音圧縮済み音声」をまとめて返す。

        旧実装は has_speech と speech_bounds が同じフレーム推論を 2 回実行していた
        （長い録音ほど VAD 時間が倍かかる）。本メソッドは推論 1 回に統合し、さらに
        前後の無音トリミングに加えて発話間の長い無音も圧縮する。

        圧縮は各発話区間の前後に pad_ms の余白を残し、区間間の無音は最大
        _KEPT_GAP_SEC まで保持する（元の無音が約 1 秒以下ならそのまま）。
        語頭・語尾の余白と句読点推定の手がかりになるポーズを残すため、
        精度には影響せず、音声が短くなる分だけ送信と API 処理が速くなる。

        Args:
            audio: 音声データ（float32, 16kHz モノラル）
            pad_ms: 発話区間の前後に残すパディング（ミリ秒）

        Returns:
            (has_speech, condensed_audio)。
            発話なしは (False, None)。VAD が使えない・推論エラー時は
            (True, None)（安全側: 発話ありとして原音をそのまま使う）
        """
        if len(audio) < _FRAME_SAMPLES:
            return (False, None)
        with self._lock:
            if not self._load_session():
                return (True, None)
            try:
                probs = self._frame_probs(audio)
            except Exception as e:
                logger.error(f"VAD 推論エラー（発話ありとして続行）: {e}")
                return (True, None)

        # 前後 pad 付き・_KEPT_GAP_SEC 以下の無音をマージしたサンプル区間を得る。
        # 区間が無ければ発話なし（クリックノイズ誤検出の除外は _speech_regions 内）
        regions = self._speech_regions(probs, len(audio), pad_ms, _KEPT_GAP_SEC)
        if not regions:
            return (False, None)

        # 区間を連結（各接合部には前後 pad ぶん＝計 _KEPT_GAP_SEC の実無音が残る）
        condensed = np.concatenate([audio[lo:hi] for lo, hi in regions])
        return (True, condensed)

    @staticmethod
    def _speech_regions(
        probs: np.ndarray, audio_len: int, pad_ms: int, gap_sec: float
    ) -> list:
        """発話確率列から、前後 pad 付き・gap_sec 以下の無音でマージした
        サンプル区間 [[lower, upper], ...] を返す（発話なしは空リスト）。

        単発のクリックノイズ誤検出を避けるため、しきい値超えフレームの「総数」ではなく
        各連続 run の長さで判定し、_MIN_SPEECH_FRAMES 以上連続した run だけを発話とみなす。
        （離れた単発ノイズ 2 個は総数 2 でも各 run が 1 フレームなので除外される。）
        analyze（gap=_KEPT_GAP_SEC）と segment（gap=_SPLIT_GAP_SEC）の共通処理。
        """
        speech_idx = np.flatnonzero(probs >= _SPEECH_THRESHOLD)
        if len(speech_idx) == 0:
            return []
        # 発話フレームの連続区間（インデックスの切れ目で分割）
        splits = np.flatnonzero(np.diff(speech_idx) > 1)
        starts = np.concatenate(([speech_idx[0]], speech_idx[splits + 1]))
        ends = np.concatenate((speech_idx[splits], [speech_idx[-1]]))

        # 連続長が _MIN_SPEECH_FRAMES 未満の run（≒単発クリックノイズ）は捨てる
        runs = [
            (int(s), int(e))
            for s, e in zip(starts, ends)
            if int(e) - int(s) + 1 >= _MIN_SPEECH_FRAMES
        ]
        if not runs:
            return []

        pad = int(16000 * pad_ms / 1000)
        gap = int(16000 * gap_sec)
        regions: list = []
        for s, e in runs:
            lower = max(0, s * _FRAME_SAMPLES - pad)
            upper = min(audio_len, (e + 1) * _FRAME_SAMPLES + pad)
            # 近接区間はマージ（区間の間に残る無音が gap 以下なら切らない）
            if regions and lower - regions[-1][1] <= gap:
                regions[-1][1] = max(regions[-1][1], upper)
            else:
                regions.append([lower, upper])
        return regions

    def segment(self, audio: npt.NDArray[np.float32], pad_ms: int = 250) -> list:
        """長い音声を無音区間で分割したセグメント（np.ndarray）のリストを返す。

        分割点は _SPLIT_GAP_SEC を超える無音の中だけなので語の途中では切れない。
        全体が短い（_MIN_SPLIT_SEC 未満）／区間が 1 つ／VAD 不可なら空リストを返し、
        呼び出し側は従来どおり 1 本送信にフォールバックする。

        Args:
            audio: 音声データ（float32, 16kHz モノラル）
            pad_ms: 各セグメントの前後に残すパディング（ミリ秒）

        Returns:
            セグメント（np.ndarray）のリスト。分割しないときは空リスト
        """
        if len(audio) < _FRAME_SAMPLES or len(audio) / 16000 < _MIN_SPLIT_SEC:
            return []
        with self._lock:
            if not self._load_session():
                return []
            try:
                probs = self._frame_probs(audio)
            except Exception as e:
                logger.error(f"VAD 推論エラー（分割せず 1 本送信）: {e}")
                return []

        regions = self._speech_regions(probs, len(audio), pad_ms, _SPLIT_GAP_SEC)
        if len(regions) < 2:
            return []
        # 短すぎるセグメントは直前に結合して細切れ・送信オーバーヘッドを抑える。
        # 「現在のセグメントが短い」場合（末尾・中間の短区間）と「直前が短い」場合
        # （先頭の短区間）の両方を直前へ結合する。現在 seg 長を見ないと、長区間の
        # 後ろに続く 2 秒未満の短区間が独立して残ってしまう（#23）。
        min_len = int(16000 * _MIN_SEGMENT_SEC)
        segments: list = []
        for lo, hi in regions:
            seg = audio[lo:hi]
            if segments and (len(seg) < min_len or len(segments[-1]) < min_len):
                segments[-1] = np.concatenate([segments[-1], seg])
            else:
                segments.append(seg)
        return segments if len(segments) >= 2 else []
