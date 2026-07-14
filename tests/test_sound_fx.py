"""操作音（src/core/sound_fx.py）の合成・再生ロジックのテスト。

検証する性質:
1. 合成波形の形（長さ = 2 トーン分・float32 モノラル）・振幅上限・両端フェード。
2. start は上昇（後半トーンの周波数が高い）、stop は下降（後半トーンの周波数が低い）。
3. 未知の種類は None を返す。
4. 再生本体 `_play_impl` は注入した player に合成バッファを渡す（実 sounddevice はモック境界の外）。
5. 公開 `play()` は例外を投げない（fire-and-forget）。

実際の音声出力（sounddevice）はモック境界の外なのでハードウェアには触れない。
"""

import time
import unittest

import numpy as np

from src.core import sound_fx
from src.core.sound_fx import AMPLITUDE, SAMPLE_RATE, TONE_DURATION, make_buffer


def _zero_crossings(x: np.ndarray) -> int:
    """符号反転回数（おおよその周波数の代理指標）。"""
    s = np.sign(x)
    s = s[s != 0]
    return int(np.count_nonzero(np.diff(s) != 0))


class MakeBufferTests(unittest.TestCase):
    def test_shape_and_dtype(self):
        """長さ = 2 トーン分・float32 の 1 次元（モノラル）。"""
        buf = make_buffer("start")
        frames_per_tone = int(SAMPLE_RATE * TONE_DURATION)
        self.assertEqual(buf.dtype, np.float32)
        self.assertEqual(buf.ndim, 1)
        self.assertEqual(len(buf), frames_per_tone * 2)

    def test_amplitude_bounded(self):
        """振幅は AMPLITUDE を超えない（フェードと正弦で必ず以下）。"""
        for kind in ("start", "stop"):
            buf = make_buffer(kind)
            self.assertLessEqual(float(np.max(np.abs(buf))), AMPLITUDE + 1e-6)

    def test_endpoints_faded(self):
        """クリック音防止のため先頭・末尾サンプルはほぼ 0（フェード）。"""
        buf = make_buffer("start")
        self.assertAlmostEqual(float(buf[0]), 0.0, places=5)
        self.assertAlmostEqual(float(buf[-1]), 0.0, places=5)

    def test_start_rises_stop_falls(self):
        """start は後半トーンが高音（上昇）、stop は後半トーンが低音（下降）。"""
        frames_per_tone = int(SAMPLE_RATE * TONE_DURATION)
        start = make_buffer("start")
        stop = make_buffer("stop")
        start_first = _zero_crossings(start[:frames_per_tone])
        start_second = _zero_crossings(start[frames_per_tone:])
        stop_first = _zero_crossings(stop[:frames_per_tone])
        stop_second = _zero_crossings(stop[frames_per_tone:])
        self.assertGreater(start_second, start_first)  # 660→990（上昇）
        self.assertLess(stop_second, stop_first)        # 880→587（下降）

    def test_unknown_kind_returns_none(self):
        self.assertIsNone(make_buffer("bogus"))


class PlaybackTests(unittest.TestCase):
    def test_play_impl_passes_buffer_to_player(self):
        """_play_impl は合成バッファを注入 player へ渡す。"""
        received = {}

        def fake_player(buf):
            received["buf"] = buf

        sound_fx._play_impl("stop", player=fake_player)
        self.assertIn("buf", received)
        self.assertEqual(len(received["buf"]), int(SAMPLE_RATE * TONE_DURATION) * 2)

    def test_play_impl_unknown_kind_does_not_call_player(self):
        """未知の種類なら player を呼ばない（合成が None）。"""
        called = {"n": 0}

        def fake_player(buf):
            called["n"] += 1

        sound_fx._play_impl("bogus", player=fake_player)
        self.assertEqual(called["n"], 0)

    def test_play_impl_swallows_player_error(self):
        """player が例外を投げても握りつぶす（音が鳴らないだけ）。"""
        def boom(buf):
            raise RuntimeError("no audio device")

        # 例外が伝播しないこと
        sound_fx._play_impl("start", player=boom)

    def test_public_play_does_not_raise(self):
        """公開 play() は fire-and-forget で即座に戻り例外を投げない。

        実オーディオデバイスに触れないよう既定 player を差し替えてから呼ぶ
        （テストで実機の音を鳴らさない）。
        """
        original = sound_fx._default_player
        sound_fx._default_player = lambda buf: None
        try:
            sound_fx.play("start")
            sound_fx.play("bogus")
        finally:
            # スポーンした daemon スレッドが差し替え済み player を使い終えるのを待ってから戻す
            time.sleep(0.05)
            sound_fx._default_player = original


if __name__ == "__main__":
    unittest.main()
