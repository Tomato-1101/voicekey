"""stats（使用実績ストア）のロジックテスト。

レベル/しきい値の計算式、節約時間の式、連続利用日数の更新、
永続化と壊れたファイルからの復帰を、一時ファイル上で検証する
（既定パスには触れない）。計算式は Mac 版 StatsStore.swift と一致させる。
"""

import tempfile
import unittest
from pathlib import Path

from src.core.stats import (
    ASSUMED_TYPING_CHARS_PER_SECOND,
    StatsStore,
    level_for_xp,
    threshold,
)


class TestStatsMath(unittest.TestCase):
    """レベル・しきい値の純関数テスト（Mac 版と同じ値になること）。"""

    def test_threshold_values(self):
        """threshold(L) = 250*(L-1)*L。レベル1は0、2は500、3は1500。"""
        self.assertEqual(threshold(1), 0)
        self.assertEqual(threshold(2), 500)
        self.assertEqual(threshold(3), 1500)
        self.assertEqual(threshold(4), 3000)

    def test_level_for_xp_boundaries(self):
        """しきい値ちょうどで次レベルに上がり、1手前では上がらない。"""
        self.assertEqual(level_for_xp(0), 1)
        self.assertEqual(level_for_xp(499), 1)
        self.assertEqual(level_for_xp(500), 2)
        self.assertEqual(level_for_xp(1499), 2)
        self.assertEqual(level_for_xp(1500), 3)

    def test_level_at_least_one(self):
        """負やゼロでもレベルは最低1。"""
        self.assertEqual(level_for_xp(-100), 1)
        self.assertEqual(level_for_xp(0), 1)

    def test_level_and_threshold_consistent(self):
        """threshold(level_for_xp(xp)) <= xp < threshold(level+1) が常に成り立つ。"""
        for xp in [0, 1, 250, 499, 500, 777, 1500, 5000, 123456]:
            lvl = level_for_xp(xp)
            self.assertLessEqual(threshold(lvl), xp)
            self.assertLess(xp, threshold(lvl + 1))


class TestStatsStore(unittest.TestCase):
    """StatsStore の記録・派生値・永続化テスト。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "stats.json"

    def tearDown(self):
        self._tmp.cleanup()

    def test_record_accumulates(self):
        """文字数・回数・録音秒が累積する。"""
        store = StatsStore(self.path)
        store.record_session(characters=100, recording_seconds=5.0)
        store.record_session(characters=50, recording_seconds=3.0)
        snap = store.snapshot()
        self.assertEqual(snap["total_sessions"], 2)
        self.assertEqual(snap["total_characters"], 150)
        self.assertAlmostEqual(snap["total_recording_seconds"], 8.0)

    def test_empty_text_ignored(self):
        """0文字（空入力）は記録しない。"""
        store = StatsStore(self.path)
        store.record_session(characters=0, recording_seconds=2.0)
        self.assertEqual(store.snapshot()["total_sessions"], 0)

    def test_saved_seconds_formula(self):
        """節約秒 = 文字数/タイピング速度 − 録音秒（マイナスは0に丸め）。"""
        store = StatsStore(self.path)
        # 100文字・録音5秒 → 100/4 - 5 = 20秒の節約
        store.record_session(characters=100, recording_seconds=5.0)
        self.assertAlmostEqual(store.snapshot()["saved_seconds"], 20.0)

    def test_saved_seconds_never_negative(self):
        """短い入力で手入力より遅くても、節約はマイナスにならない。"""
        store = StatsStore(self.path)
        # 4文字・録音10秒 → 4/4 - 10 = -9 だが 0 に丸める
        store.record_session(characters=4, recording_seconds=10.0)
        self.assertEqual(store.snapshot()["saved_seconds"], 0.0)

    def test_typing_speed_constant(self):
        """節約時間の前提タイピング速度は 4.0 字/秒（Mac 版と同値）。"""
        self.assertEqual(ASSUMED_TYPING_CHARS_PER_SECOND, 4.0)

    def test_streak_starts_at_one(self):
        """初回利用で連続日数は1。"""
        store = StatsStore(self.path)
        store.record_session(characters=10, recording_seconds=1.0)
        snap = store.snapshot()
        self.assertEqual(snap["current_streak"], 1)
        self.assertEqual(snap["longest_streak"], 1)

    def test_snapshot_derived_level(self):
        """snapshot は累計文字数から派生したレベル/進捗を含む。"""
        store = StatsStore(self.path)
        store.record_session(characters=500, recording_seconds=1.0)
        snap = store.snapshot()
        self.assertEqual(snap["xp"], 500)
        self.assertEqual(snap["level"], 2)
        self.assertEqual(snap["xp_to_next_level"], threshold(3) - 500)
        self.assertGreaterEqual(snap["level_progress"], 0.0)
        self.assertLessEqual(snap["level_progress"], 1.0)

    def test_persist_and_reload(self):
        """保存後に同じパスで開き直すと累計が残っている。"""
        store = StatsStore(self.path)
        store.record_session(characters=200, recording_seconds=4.0)
        reopened = StatsStore(self.path)
        self.assertEqual(reopened.snapshot()["total_characters"], 200)

    def test_reset(self):
        """reset で全カウントが0に戻る。"""
        store = StatsStore(self.path)
        store.record_session(characters=200, recording_seconds=4.0)
        store.reset()
        snap = store.snapshot()
        self.assertEqual(snap["total_sessions"], 0)
        self.assertEqual(snap["total_characters"], 0)
        self.assertEqual(snap["saved_seconds"], 0.0)

    def test_corrupt_file_recovers(self):
        """壊れた JSON でも 0 から開始してクラッシュしない。"""
        self.path.write_text("{ this is not valid json", encoding="utf-8")
        store = StatsStore(self.path)
        self.assertEqual(store.snapshot()["total_characters"], 0)


if __name__ == "__main__":
    unittest.main()
