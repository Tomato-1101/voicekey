"""
API 送信前の音声前処理モジュール。

短い音声（1〜数秒）を想定し、API 文字起こしの精度向上のため
**音量の一定化**のみを適用する。

ノイズ対策は API モデル（Whisper / Groq）側で十分に行われているため、
ここではローカルでのノイズ低減は実施せず、numpy のみで完結する低レイテンシ処理に留める。
"""

import numpy as np
import numpy.typing as npt

from ..utils.logger import get_logger

logger = get_logger(__name__)

# 目標 RMS = -20 dBFS（人声に適した一般的な値）
TARGET_RMS_DBFS: float = -20.0
# ピーク上限 = -3 dBFS（音割れ防止のヘッドルーム）
PEAK_CEILING_DBFS: float = -3.0
# ゲイン上限 = +20 dB（約 10 倍）。
# 上限なしだと「ほぼ無音＋ノイズフロア」の録音でノイズだけが目標 RMS まで
# 増幅され、API 側の幻覚（架空テキスト出力）の主要因になっていた。
MAX_GAIN_DB: float = 20.0


def _dbfs_to_amp(dbfs: float) -> float:
    """dBFS（フルスケール基準デシベル）を線形振幅に変換する。"""
    return 10.0 ** (dbfs / 20.0)


def normalize_volume(audio: npt.NDArray[np.float32]) -> npt.NDArray[np.float32]:
    """
    Peak+RMS ハイブリッド方式で音量を一定化する。

    1. RMS を目標値（-20 dBFS）に合わせるゲインを算出（上限 +20 dB）
    2. 適用後にピークが -3 dBFS を超えていれば追加で抑え込む（音割れ防止）

    短い音声でも安定し、numpy のみで <1ms の処理時間。
    完全無音時は原音をそのまま返す（ゲイン発散の防止）。

    Args:
        audio: 入力音声（float32, モノラル）

    Returns:
        ゲイン調整後の音声（float32）
    """
    if audio.size == 0:
        return audio

    # float64 で計算してアンダーフローを回避
    rms = float(np.sqrt(np.mean(audio.astype(np.float64) ** 2)))
    if rms < 1e-6:
        return audio

    target_amp = _dbfs_to_amp(TARGET_RMS_DBFS)
    # ノイズフロアだけの録音を増幅し尽くさないようゲインに上限を設ける
    gain = min(target_amp / rms, _dbfs_to_amp(MAX_GAIN_DB))
    boosted = audio * gain

    # ピーク制限（クリッピング防止のヘッドルーム）
    peak = float(np.max(np.abs(boosted)))
    ceiling_amp = _dbfs_to_amp(PEAK_CEILING_DBFS)
    if peak > ceiling_amp:
        boosted = boosted * (ceiling_amp / peak)

    return boosted.astype(np.float32)


def preprocess(
    audio: npt.NDArray[np.float32],
    sample_rate: int,
    enable_normalize: bool = True,
) -> npt.NDArray[np.float32]:
    """
    音声前処理パイプラインを適用する。

    現状は音量正規化のみ。ノイズ対策は API モデル側に任せる方針。

    Args:
        audio: 入力音声（float32, モノラル）
        sample_rate: サンプリングレート（Hz、現状は使用しないが将来用）
        enable_normalize: 音量正規化を実行するか

    Returns:
        前処理後の音声（float32）
    """
    if audio.size == 0:
        return audio

    out = audio
    if enable_normalize:
        out = normalize_volume(out)
    return out
