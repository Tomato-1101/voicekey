"""
操作音モジュール（Windows・録音開始/停止の効果音・音源ファイル不要）

Mac 版 SoundFX.swift と同じく、開始=2 音上昇・停止=2 音下降の短いブリップを正弦波で
その場合成する（バンドルに音源ファイルを増やさない）。Mac は AVAudioEngine の再生専用
エンジン、Windows は既存依存の sounddevice で出力する。いずれもマイク録音とは独立した
再生で、fire-and-forget（音声入力パイプラインに待ちを足さない）。失敗（オーディオ経路の
一時不整合等）は握りつぶす＝音が鳴らないだけで実害はない。

テスト容易性: 波形合成は純粋関数 `make_buffer(kind)`（numpy のみ・音声ハードウェア不要）に
分離し、実際の再生（sounddevice）は `_default_player` に隔離して差し替え可能にしてある。
Mac と定数（周波数・長さ・振幅・フェード）を一致させること。
"""

import threading
from typing import Callable, Optional

import numpy as np

from ..utils.logger import get_logger

logger = get_logger(__name__)

# 合成・再生のパラメータ（Mac 版 SoundFX と一致させる）
SAMPLE_RATE = 44100      # 合成フォーマット（モノラル 44.1kHz float）
TONE_DURATION = 0.065    # 各トーンの長さ（秒）
AMPLITUDE = 0.1          # 控えめな音量
FADE_SEC = 0.006         # 前後 6ms を線形フェードしてクリック音を消す

# 開始=2 音上昇（E5→B5 相当）、停止=2 音下降（A5→D5 相当）
_FREQS = {
    "start": (660.0, 990.0),
    "stop": (880.0, 587.0),
}


def make_buffer(kind: str) -> Optional[np.ndarray]:
    """種類に応じた短い正弦波ブリップを合成する（float32 モノラル）。

    Args:
        kind: "start"（開始・上昇）または "stop"（停止・下降）

    Returns:
        合成した波形（float32 1 次元配列）。未知の種類なら None。
    """
    freqs = _FREQS.get(kind)
    if freqs is None:
        return None

    frames_per_tone = int(SAMPLE_RATE * TONE_DURATION)
    fade_frames = min(frames_per_tone, int(SAMPLE_RATE * FADE_SEC))
    two_pi = 2.0 * np.pi

    # 前後 fade_frames を線形フェードする包絡線（1 トーンぶん）
    env = np.ones(frames_per_tone, dtype=np.float64)
    if fade_frames > 0:
        ramp = np.arange(fade_frames, dtype=np.float64) / fade_frames
        env[:fade_frames] = np.minimum(env[:fade_frames], ramp)
        # 末尾はミラーで落とす（残り frames が fade_frames を割ると env が小さくなる）
        env[-fade_frames:] = np.minimum(env[-fade_frames:], ramp[::-1])

    t = np.arange(frames_per_tone, dtype=np.float64) / SAMPLE_RATE
    tones = []
    for freq in freqs:
        tones.append(np.sin(two_pi * freq * t) * env * AMPLITUDE)
    return np.concatenate(tones).astype(np.float32)


def _default_player(buffer: np.ndarray) -> None:
    """既定の再生（sounddevice・マイク録音とは独立した出力ストリーム）。"""
    import sounddevice as sd

    sd.play(buffer, SAMPLE_RATE)


def _play_impl(kind: str, player: Optional[Callable[[np.ndarray], None]] = None) -> None:
    """合成 → 再生の本体（失敗は握りつぶす）。テストは player を注入して検証する。"""
    try:
        buffer = make_buffer(kind)
        if buffer is None:
            return
        (player or _default_player)(buffer)
    except Exception as e:  # 音が鳴らないだけで実害はないため握りつぶす
        logger.debug(f"操作音の再生に失敗（無視）: {e}")


def play(kind: str) -> None:
    """操作音を鳴らす（fire-and-forget）。呼び出し側は即座に戻る。

    Args:
        kind: "start" または "stop"
    """
    try:
        threading.Thread(target=_play_impl, args=(kind,), daemon=True).start()
    except Exception as e:
        logger.debug(f"操作音の投入に失敗（無視）: {e}")
