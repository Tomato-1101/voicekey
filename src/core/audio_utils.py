"""
音声フォーマット変換ユーティリティ

NumPy 配列形式の音声データを WAV 形式に変換する機能を提供する。
Groq/OpenAI API などへの送信時に使用される。

旧実装にあった MP3 変換（ffmpeg サブプロセス）は廃止した:
- 16kHz モノラル WAV は 5 秒 ≈ 160KB と十分小さく、転送時間差は無視できる
- ffmpeg の一時ファイル書き出し＋サブプロセス起動の方がエンコードより遅い
- import 時の `ffmpeg -version` 実行（最大 5 秒ブロック）が起動遅延の一因だった
"""

import io
import struct

import numpy as np
import numpy.typing as npt


def numpy_to_wav_bytes(
    audio_data: npt.NDArray[np.float32],
    sample_rate: int = 16000,
    channels: int = 1,
    bits_per_sample: int = 16
) -> bytes:
    """
    NumPy の float32 音声配列を WAV 形式のバイト列に変換する。

    Args:
        audio_data: float32 形式の音声データ（-1.0〜1.0 の範囲）
        sample_rate: サンプリングレート（Hz）
        channels: チャンネル数（1=モノラル、2=ステレオ）
        bits_per_sample: ビット深度（16 固定を想定）

    Returns:
        WAV ファイル形式のバイト列
    """
    # 範囲外サンプルが int16 でラップアラウンドして轟音ノイズになるのを防ぐ
    clipped = np.clip(audio_data, -1.0, 1.0)
    # float32 [-1.0, 1.0] から int16 [-32767, 32767] に変換
    audio_int16 = (clipped * 32767).astype(np.int16)

    # WAVヘッダーを構築
    buffer = io.BytesIO()

    # データサイズを計算
    data_size = len(audio_int16) * channels * (bits_per_sample // 8)

    # RIFFヘッダー（ファイル識別子）
    buffer.write(b'RIFF')
    buffer.write(struct.pack('<I', 36 + data_size))  # ファイルサイズ - 8
    buffer.write(b'WAVE')

    # fmtチャンク（フォーマット情報）
    buffer.write(b'fmt ')
    buffer.write(struct.pack('<I', 16))              # fmtチャンクサイズ
    buffer.write(struct.pack('<H', 1))               # PCMフォーマット
    buffer.write(struct.pack('<H', channels))        # チャンネル数
    buffer.write(struct.pack('<I', sample_rate))     # サンプリングレート
    buffer.write(struct.pack('<I', sample_rate * channels * (bits_per_sample // 8)))  # バイトレート
    buffer.write(struct.pack('<H', channels * (bits_per_sample // 8)))  # ブロックサイズ
    buffer.write(struct.pack('<H', bits_per_sample)) # ビット深度

    # dataチャンク（音声データ本体）
    buffer.write(b'data')
    buffer.write(struct.pack('<I', data_size))
    buffer.write(audio_int16.tobytes())

    return buffer.getvalue()
