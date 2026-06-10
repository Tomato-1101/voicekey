"""audio_utils（NumPy → WAV バイト列）のテスト。"""

import struct
import unittest

import numpy as np

from src.core.audio_utils import numpy_to_wav_bytes


class TestWavBytes(unittest.TestCase):
    def test_riff_header(self):
        wav = numpy_to_wav_bytes(np.array([0.0, 0.5, -0.5], dtype=np.float32), 16000)
        self.assertEqual(wav[:4], b"RIFF")
        self.assertEqual(wav[8:12], b"WAVE")
        # サンプリングレートはオフセット 24
        self.assertEqual(struct.unpack("<I", wav[24:28])[0], 16000)
        # ビット深度はオフセット 34（16bit）
        self.assertEqual(struct.unpack("<H", wav[34:36])[0], 16)

    def test_clipping(self):
        # 範囲外サンプルは int16 のラップアラウンドを避けてクリップ
        wav = numpy_to_wav_bytes(np.array([2.0, -2.0], dtype=np.float32), 16000)
        samples = struct.unpack("<2h", wav[44:])
        self.assertEqual(samples, (32767, -32767))

    def test_sample_rate_param(self):
        wav = numpy_to_wav_bytes(np.array([0.0], dtype=np.float32), 24000)
        self.assertEqual(struct.unpack("<I", wav[24:28])[0], 24000)


if __name__ == "__main__":
    unittest.main()
