"""SileroVad.analyze（1 パス VAD + 無音圧縮）のロジックテスト。

venv 同梱の実 Silero ONNX モデルで推論する（ネットワーク不要・数百 ms）。
実音声は benchmark/audio/short_ja.wav（say 合成の日本語 約 6.8 秒）を使い、
「発話を失わず、長い無音だけが縮む」ことを検証する。
"""

import unittest
import wave
from pathlib import Path
from unittest.mock import patch

import numpy as np

from src.core.vad import _KEPT_GAP_SEC, SileroVad

_SAMPLE_RATE = 16000
_SPEECH_WAV = Path(__file__).parent.parent / "benchmark" / "audio" / "short_ja.wav"


def _load_speech() -> np.ndarray:
    """ベンチ用の実音声（16kHz モノラル 16bit）を float32 [-1,1] で読み込む。"""
    with wave.open(str(_SPEECH_WAV)) as w:
        raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def _silence(sec: float) -> np.ndarray:
    return np.zeros(int(_SAMPLE_RATE * sec), dtype=np.float32)


class TestAnalyze(unittest.TestCase):
    """analyze の発話判定と無音圧縮を検証する。"""

    @classmethod
    def setUpClass(cls):
        cls.vad = SileroVad()
        cls.speech = _load_speech()

    def test_too_short_audio_is_no_speech(self):
        """1 フレーム未満の音声は発話なし扱い。"""
        self.assertEqual(self.vad.analyze(_silence(0.01)), (False, None))

    def test_pure_silence_is_no_speech(self):
        """完全無音は (False, None)。"""
        has_speech, condensed = self.vad.analyze(_silence(2.0))
        self.assertFalse(has_speech)
        self.assertIsNone(condensed)

    def test_vad_unavailable_falls_back_to_original(self):
        """モデルが使えない場合は安全側（発話あり・原音のまま）に倒す。"""
        vad = SileroVad()
        with patch.object(vad, "_load_session", return_value=False):
            self.assertEqual(vad.analyze(self.speech), (True, None))

    def test_speech_is_detected_and_kept(self):
        """実音声は発話ありと判定され、発話部分はほぼ削られない。"""
        has_speech, condensed = self.vad.analyze(self.speech)
        self.assertTrue(has_speech)
        self.assertIsNotNone(condensed)
        # 文間の自然な短いポーズしか無いので、大きくは縮まない（>9 割残る）
        self.assertGreater(len(condensed), int(len(self.speech) * 0.9))

    def test_long_internal_silence_is_compressed(self):
        """発話の間の長い無音（3 秒）が圧縮され、発話自体は失われない。"""
        gapped = np.concatenate([self.speech, _silence(3.0), self.speech])
        has_speech, condensed = self.vad.analyze(gapped)
        self.assertTrue(has_speech)
        self.assertIsNotNone(condensed)

        # 3 秒の無音は「前後パディング 250ms ×2 + 保持ギャップ」程度まで縮む
        cut_sec = (len(gapped) - len(condensed)) / _SAMPLE_RATE
        self.assertGreater(cut_sec, 1.5, "長い無音が圧縮されていない")

        # 発話 2 回ぶんの中身は残っている（無圧縮の単体発話 ×2 とほぼ同量以上）
        single = self.vad.analyze(self.speech)[1]
        self.assertGreaterEqual(len(condensed), len(single) * 2)

    def test_short_pause_is_not_cut(self):
        """短い自然なポーズは切らない（句読点推定の手がかりを保持）。"""
        # トリム済み音声（端は発話 +250ms パディング）を部品にして
        # 0.3 秒のポーズを挟む。発話間ギャップ ≒ 0.25+0.3+0.25=0.8s →
        # パディング 0.5s を除いた残り 0.3s は保持ギャップ（0.5s）以下なので切られない
        core = self.vad.analyze(self.speech)[1]
        gapped = np.concatenate([core, _silence(0.3), core])
        has_speech, condensed = self.vad.analyze(gapped)
        self.assertTrue(has_speech)
        # 削減はフレーム境界の誤差程度（±4 フレーム）に収まる
        self.assertGreaterEqual(len(condensed), len(gapped) - 4 * 512)

    def test_joint_keeps_pause_for_punctuation(self):
        """圧縮後も接合部に _KEPT_GAP_SEC 程度の無音が残る設計値の確認。"""
        # 設計値そのものの検証（定数が誤って 0 にされた場合の回帰防止）
        self.assertGreaterEqual(_KEPT_GAP_SEC, 0.3)


if __name__ == "__main__":
    unittest.main()
