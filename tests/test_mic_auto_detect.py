"""マイク自動検出（src/core/mic_auto_detect.py）のロジックテスト。

実マイク・実 sounddevice には触れない（合成 RMS 列と偽 InputStream で検証する）。
macOS 上でも Windows 実機がなくても通る。
"""

import sys
import types
import unittest
from unittest import mock

import numpy as np

from src.core.mic_auto_detect import (
    SCORE_THRESHOLD,
    compute_score,
    detect_speaking_device,
    frame_rms,
)

# 16kHz・30ms フレーム = 480 サンプル
SR = 16000
FRAME = 480


def _block(amplitude: float, frames: int = 1) -> np.ndarray:
    """一定振幅のサンプル列を作る（RMS = 振幅になる）。"""
    return np.full(FRAME * frames, amplitude, dtype=np.float32)


class TestScore(unittest.TestCase):
    """スコア（p90 − p10）計算のテスト。Mac 版 MicAutoDetector.score と同じ閾値設計。"""

    def test_speech_like_above_threshold(self):
        """発話（音量変動が大きい）はしきい値以上になる。"""
        rms = [0.05 if i % 3 == 0 else 0.001 for i in range(130)]
        self.assertGreaterEqual(compute_score(rms), SCORE_THRESHOLD)

    def test_steady_noise_below_threshold(self):
        """定常ノイズ（変動なし）は音量が大きくてもしきい値未満。

        単純な最大 RMS 方式だとループバック装置や常時ノイズ源を誤選択する
        ケースの再現。p90 − p10 ならスコア 0 になる。
        """
        self.assertLess(compute_score([0.02] * 130), SCORE_THRESHOLD)

    def test_silence_below_threshold(self):
        """ほぼ無音はしきい値未満。"""
        self.assertLess(compute_score([0.0005] * 130), SCORE_THRESHOLD)

    def test_too_few_frames_returns_zero(self):
        """フレーム不足（10 未満）は判定不能で 0。"""
        self.assertEqual(compute_score([0.5, 0.0, 0.5]), 0.0)

    def test_speech_beats_noise(self):
        """同時監視時に発話マイクが定常ノイズ源より高スコアになる。"""
        speech = [0.05 if i % 3 == 0 else 0.001 for i in range(130)]
        self.assertGreater(compute_score(speech), compute_score([0.02] * 130))


class TestFrameRms(unittest.TestCase):
    """30ms フレーム分割と RMS 計算のテスト。"""

    def test_constant_amplitude(self):
        """一定振幅のフレーム RMS は振幅に等しい。"""
        rms = frame_rms(_block(0.25, frames=3), FRAME)
        self.assertEqual(len(rms), 3)
        for value in rms:
            self.assertAlmostEqual(value, 0.25, places=5)

    def test_remainder_dropped(self):
        """フレーム長に満たない端数サンプルは捨てられる。"""
        samples = np.zeros(FRAME + 100, dtype=np.float32)
        self.assertEqual(len(frame_rms(samples, FRAME)), 1)

    def test_too_short_returns_empty(self):
        """1 フレーム未満は空。"""
        self.assertEqual(frame_rms(np.zeros(10, dtype=np.float32), FRAME), [])


class _FakeInputStream:
    """sd.InputStream の偽物。start() で合成音声を一括でコールバックへ流し込む。"""

    # デバイス id → 合成サンプル列（テストごとに設定する）
    signals = {}
    # InputStream を開けないデバイス id の集合
    fail_devices = set()

    def __init__(self, device, channels, samplerate, dtype, callback):
        if device in self.fail_devices:
            raise RuntimeError("device busy")
        self.device = device
        self.callback = callback

    def start(self):
        samples = self.signals.get(self.device)
        if samples is None:
            return
        # 実機のコールバック分割を模して 1024 サンプルずつ流す（2D: frames x channels）
        for offset in range(0, len(samples), 1024):
            chunk = samples[offset:offset + 1024].reshape(-1, 1)
            self.callback(chunk, len(chunk), None, None)

    def stop(self):
        pass

    def close(self):
        pass


def _speech_signal() -> np.ndarray:
    """発話風: 大小の 30ms フレームが交互に続く信号。"""
    parts = []
    for i in range(40):
        parts.append(_block(0.2 if i % 3 == 0 else 0.001))
    return np.concatenate(parts)


def _noise_signal(amplitude: float = 0.05) -> np.ndarray:
    """定常ノイズ風: 一定振幅が続く信号。"""
    return _block(amplitude, frames=40)


class TestDetectSpeakingDevice(unittest.TestCase):
    """全デバイス同時監視（detect_speaking_device）の統合テスト（偽 sounddevice）。"""

    def setUp(self):
        _FakeInputStream.signals = {}
        _FakeInputStream.fail_devices = set()
        # 偽 sounddevice モジュール（detect_speaking_device 内の import が拾う）
        self._fake_sd = types.SimpleNamespace(
            InputStream=_FakeInputStream,
            query_devices=lambda device=None: {"default_samplerate": float(SR)},
        )
        self._devices = [
            {"id": 0, "name": "Speech Mic", "label": "Speech Mic (WASAPI)", "max_input_channels": 1},
            {"id": 1, "name": "Noise Mic", "label": "Noise Mic (WASAPI)", "max_input_channels": 2},
        ]

    def _detect(self):
        """偽 sounddevice とデバイス一覧で検出を実行する。"""
        with mock.patch.dict(sys.modules, {"sounddevice": self._fake_sd}), \
             mock.patch(
                 "src.core.audio_recorder.AudioRecorder.list_input_devices",
                 return_value=self._devices,
             ):
            # 合成データは start() で一括投入済みのため待ち時間は不要
            return detect_speaking_device(duration=0.01)

    def test_selects_speaking_device(self):
        """声が入っているデバイスが選ばれる（定常ノイズ源より優先）。"""
        _FakeInputStream.signals = {0: _speech_signal(), 1: _noise_signal()}
        result = self._detect()
        self.assertIsNotNone(result)
        self.assertEqual(result["id"], 0)
        self.assertEqual(result["name"], "Speech Mic")

    def test_no_speech_returns_none(self):
        """どのデバイスも定常ノイズ・無音なら None（音声を検出できませんでした）。"""
        _FakeInputStream.signals = {0: _block(0.0005, 40), 1: _noise_signal()}
        self.assertIsNone(self._detect())

    def test_unopenable_device_skipped(self):
        """開けないデバイスはスキップされ、残りで検出が成立する。"""
        _FakeInputStream.fail_devices = {0}
        _FakeInputStream.signals = {1: _speech_signal()}
        result = self._detect()
        self.assertIsNotNone(result)
        self.assertEqual(result["id"], 1)

    def test_no_devices_returns_none(self):
        """入力デバイスが 1 つも無ければ None。"""
        self._devices = []
        self.assertIsNone(self._detect())


if __name__ == "__main__":
    unittest.main()
