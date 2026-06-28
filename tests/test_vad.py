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

from src.core.vad import _KEPT_GAP_SEC, _MIN_SPLIT_SEC, SileroVad

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


class TestSegment(unittest.TestCase):
    """長文の分割（segment / _speech_regions）を検証する。"""

    @classmethod
    def setUpClass(cls):
        cls.vad = SileroVad()
        cls.speech = _load_speech()  # 約 6.8 秒

    def test_short_audio_not_split(self):
        """_MIN_SPLIT_SEC 未満は分割しない（空リスト）。"""
        # 実音声単体は約 6.8 秒 < 12 秒 なので分割対象外
        self.assertLess(len(self.speech) / _SAMPLE_RATE, _MIN_SPLIT_SEC)
        self.assertEqual(self.vad.segment(self.speech), [])

    def test_pure_silence_not_split(self):
        """無音だけ（発話区間 0）は分割しない。"""
        self.assertEqual(self.vad.segment(_silence(15.0)), [])

    def test_long_silence_splits_into_two(self):
        """発話の間に長い無音（1.5 秒 > SPLIT_GAP）があれば 2 セグメントに割れる。"""
        # 6.8s + 1.5s 無音 + 6.8s = 約 15 秒（_MIN_SPLIT_SEC 超）
        gapped = np.concatenate([self.speech, _silence(1.5), self.speech])
        segments = self.vad.segment(gapped)
        self.assertEqual(len(segments), 2, "長い無音で 2 分割されていない")
        # 各セグメントは発話を含む（無音だけの細切れではない）
        for seg in segments:
            self.assertGreater(len(seg) / _SAMPLE_RATE, 2.0)
        # 分割しても合計が極端に欠落しない
        total = sum(len(s) for s in segments)
        self.assertGreater(total, len(gapped) * 0.5)

    def test_short_pause_not_split(self):
        """短いポーズ（0.3 秒 < SPLIT_GAP）では割らず空リスト（1 本送信に回す）。"""
        gapped = np.concatenate([self.speech, _silence(0.3), self.speech])
        # 全体は 12 秒超だが、ギャップが SPLIT_GAP 以下なので 1 区間 → 分割なし
        self.assertEqual(self.vad.segment(gapped), [])

    def test_speech_regions_gap_threshold(self):
        """_speech_regions は gap_sec を境にマージ/分割を切り替える。"""
        # フレーム 0-4 と 60-64 を発話に（間は約 55 フレーム ≒ 1.76 秒の無音）
        probs = np.zeros(100, dtype=np.float32)
        probs[0:5] = 1.0
        probs[60:65] = 1.0
        audio_len = 100 * 512
        # gap 0.7 秒（≒21.9 フレーム）未満の無音 → 2 区間に分かれる
        two = SileroVad._speech_regions(probs, audio_len, pad_ms=250, gap_sec=0.7)
        self.assertEqual(len(two), 2)
        # gap 3.0 秒 → 同じ無音がマージされ 1 区間
        one = SileroVad._speech_regions(probs, audio_len, pad_ms=250, gap_sec=3.0)
        self.assertEqual(len(one), 1)

    def test_speech_regions_single_frame_is_noise(self):
        """単発フレーム（クリックノイズ相当）は発話とみなさず空。"""
        probs = np.zeros(50, dtype=np.float32)
        probs[10] = 1.0  # 1 フレームのみ
        self.assertEqual(SileroVad._speech_regions(probs, 50 * 512, 250, 0.7), [])

    def test_speech_regions_two_isolated_frames_are_noise(self):
        """離れた単発フレーム 2 個（総数 2・各 run 長 1）は発話としない（#22）。
        旧実装は総数 2 以上で通したため 2 発話と誤採用していた。"""
        probs = np.zeros(100, dtype=np.float32)
        probs[10] = 1.0
        probs[80] = 1.0  # 離れた 2 個の単発クリックノイズ
        self.assertEqual(SileroVad._speech_regions(probs, 100 * 512, 250, 0.7), [])

    def test_speech_regions_short_run_is_speech(self):
        """連続 2 フレーム（≒64ms）の run は発話とみなす（#22）。"""
        probs = np.zeros(100, dtype=np.float32)
        probs[10:12] = 1.0  # 連続 2 フレーム
        regions = SileroVad._speech_regions(probs, 100 * 512, 250, 0.7)
        self.assertEqual(len(regions), 1)

    def test_speech_regions_isolated_noise_dropped_run_kept(self):
        """単発ノイズと連続 run が混在 → run だけ採用（#22）。"""
        probs = np.zeros(100, dtype=np.float32)
        probs[5] = 1.0       # 単発ノイズ（捨てる）
        probs[40:45] = 1.0   # 連続 5 フレームの発話（採用）
        regions = SileroVad._speech_regions(probs, 100 * 512, 250, 0.7)
        self.assertEqual(len(regions), 1)


class TestSegmentMerge(unittest.TestCase):
    """短いセグメントの直前結合（#23）。_speech_regions をモックして長さ条件だけ検証する。

    旧実装は「直前セグメントが短いとき」だけ結合していたため、長区間の後ろに続く
    _MIN_SEGMENT_SEC 未満の短区間が独立したまま残っていた。現在セグメント長でも
    判定するようにして、先頭・中間・末尾いずれの短区間も直前へ結合されることを確認する。
    """

    LONG = 48000   # 3 秒（>= _MIN_SEGMENT_SEC = 2 秒）
    SHORT = 16000  # 1 秒（< _MIN_SEGMENT_SEC）
    MIN = 32000    # 2 秒分のサンプル数

    def _segment_with_region_lens(self, region_lens):
        """指定長の連続セグメント列になる regions を返すよう _speech_regions を差し替え、
        分割対象になる 20 秒のダミー音声で segment() を実行して結果を返す。"""
        vad = SileroVad()
        regions, pos = [], 0
        for length in region_lens:
            regions.append([pos, pos + length])
            pos += length
        audio = np.zeros(20 * _SAMPLE_RATE, dtype=np.float32)  # 12 秒超 → 分割対象
        with patch.object(SileroVad, "_load_session", return_value=True), \
             patch.object(SileroVad, "_frame_probs", return_value=np.zeros(10, dtype=np.float32)), \
             patch.object(SileroVad, "_speech_regions", return_value=regions):
            return vad.segment(audio)

    def _assert_no_short_segment(self, segs, expected_count):
        self.assertEqual(len(segs), expected_count)
        for seg in segs:
            self.assertGreaterEqual(len(seg), self.MIN, "結合されず短区間が残っている")

    def test_leading_short_merged_forward(self):
        """先頭の短区間は直後の長区間と結合される。"""
        segs = self._segment_with_region_lens([self.SHORT, self.LONG, self.LONG])
        self._assert_no_short_segment(segs, 2)

    def test_middle_short_merged(self):
        """中間の短区間は直前へ結合される。"""
        segs = self._segment_with_region_lens([self.LONG, self.SHORT, self.LONG])
        self._assert_no_short_segment(segs, 2)

    def test_trailing_short_merged(self):
        """末尾の短区間（報告バグ）は直前へ結合される。"""
        segs = self._segment_with_region_lens([self.LONG, self.LONG, self.SHORT])
        self._assert_no_short_segment(segs, 2)

    def test_all_long_unchanged(self):
        """すべて長区間なら結合せずそのまま。"""
        segs = self._segment_with_region_lens([self.LONG, self.LONG])
        self._assert_no_short_segment(segs, 2)


if __name__ == "__main__":
    unittest.main()
