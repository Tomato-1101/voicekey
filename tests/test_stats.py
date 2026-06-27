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


class TestStatsDailySeries(unittest.TestCase):
    """日次・月次バケット（チャート用）のテスト。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "stats.json"

    def tearDown(self):
        self._tmp.cleanup()

    def test_daily_series_length_and_zero_fill(self):
        """指定日数ぶん古い順で返り、記録の無い日は 0 埋めされる。"""
        store = StatsStore(self.path)
        series = store.daily_series(7, end_day="2026-06-24")
        self.assertEqual(len(series), 7)
        self.assertEqual(series[0]["day"], "2026-06-18")  # 7 日前
        self.assertEqual(series[-1]["day"], "2026-06-24")  # 末尾=指定日
        self.assertTrue(all(b["characters"] == 0 for b in series))

    def test_daily_series_counts_today(self):
        """当日の記録が当日バケットに入る。"""
        store = StatsStore(self.path)
        store.record_session(characters=30, recording_seconds=2.0)
        store.record_session(characters=20, recording_seconds=1.0)
        from src.core.stats import _day_string
        from datetime import datetime
        today = _day_string(datetime.now().astimezone())
        series = store.daily_series(1, end_day=today)
        self.assertEqual(series[-1]["characters"], 50)
        self.assertEqual(series[-1]["sessions"], 2)

    def test_daily_series_persists(self):
        """日次バケットは保存され、開き直しても残る。"""
        store = StatsStore(self.path)
        store.record_session(characters=15, recording_seconds=1.0)
        from src.core.stats import _day_string
        from datetime import datetime
        today = _day_string(datetime.now().astimezone())
        reopened = StatsStore(self.path)
        series = reopened.daily_series(1, end_day=today)
        self.assertEqual(series[-1]["characters"], 15)

    def test_monthly_series_aggregates_and_wraps_year(self):
        """月次は 12 ヶ月ぶん古い順で返り、年をまたいで連続する。"""
        store = StatsStore(self.path)
        series = store.monthly_series(12, end_month="2026-03")
        self.assertEqual(len(series), 12)
        self.assertEqual(series[0]["month"], "2025-04")  # 11 ヶ月前（年またぎ）
        self.assertEqual(series[-1]["month"], "2026-03")


class TestStatsAccountLinking(unittest.TestCase):
    """アカウント連携（#10）の派生・取り込み・連携OFF時の無副作用テスト。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "stats.json"

    def tearDown(self):
        self._tmp.cleanup()

    def test_account_sync_disabled_by_default_no_side_effect(self):
        """既定で連携 OFF。record_session が backend_client を一切呼ばない（keyring 非接触）。"""
        from unittest import mock
        from src.core import backend_client
        store = StatsStore(self.path)
        with mock.patch.object(backend_client, "is_logged_in") as m:
            store.record_session(characters=10, recording_seconds=1.0)
            m.assert_not_called()  # 連携 OFF なら is_logged_in すら呼ばない

    def test_snapshot_uses_account_totals_when_higher(self):
        """ログイン中（account_* 取り込み済み）は端末横断の累計を表示する。"""
        store = StatsStore(self.path)
        store.record_session(characters=100, recording_seconds=5.0)  # ローカル 100
        # 端末横断の取り込みを模擬（別端末分を含む合算）
        store._data["account_daily"] = {}
        store._data["account_total_characters"] = 500
        store._data["account_total_sessions"] = 9
        store._data["account_total_recording_seconds"] = 40.0
        snap = store.snapshot()
        self.assertEqual(snap["total_characters"], 500)  # 端末横断が勝つ
        self.assertEqual(snap["total_sessions"], 9)
        self.assertEqual(snap["xp"], 500)
        self.assertEqual(snap["level"], 2)  # XP も端末横断で算出

    def test_local_is_floor_for_totals(self):
        """端末横断がローカルより小さくても、ローカルを下限に保つ（実績が下がらない）。"""
        store = StatsStore(self.path)
        store.record_session(characters=1000, recording_seconds=5.0)
        store._data["account_daily"] = {}
        store._data["account_total_characters"] = 200  # ローカルより小さい
        snap = store.snapshot()
        self.assertEqual(snap["total_characters"], 1000)  # ローカルが下限

    def test_effective_daily_merges_account_into_series(self):
        """daily_series はログイン中、端末横断の合算（account_daily）を field-wise max で反映する。"""
        store = StatsStore(self.path)
        # ローカルには 2026-06-26 が 10 文字
        store._data["daily"] = {"2026-06-26": {"characters": 10, "recording_seconds": 1.0, "sessions": 1}}
        # サーバー合算は同日 30 文字（別端末分を含む）＋ 別日
        store._data["account_daily"] = {
            "2026-06-26": {"characters": 30, "recording_seconds": 4.0, "sessions": 3},
            "2026-06-25": {"characters": 5, "recording_seconds": 0.5, "sessions": 1},
        }
        series = store.daily_series(2, end_day="2026-06-26")
        by_day = {b["day"]: b for b in series}
        self.assertEqual(by_day["2026-06-26"]["characters"], 30)  # max(10, 30)
        self.assertEqual(by_day["2026-06-25"]["characters"], 5)   # account のみの日も出る

    def test_clear_account_reverts_to_local(self):
        """clear_account 後はローカル値に戻る（ログアウト相当）。"""
        store = StatsStore(self.path)
        store.record_session(characters=100, recording_seconds=5.0)
        store._data["account_total_characters"] = 999
        store._data["account_daily"] = {}
        self.assertEqual(store.snapshot()["total_characters"], 999)
        store.clear_account()
        self.assertEqual(store.snapshot()["total_characters"], 100)

    def test_account_daily_persists(self):
        """取り込んだ端末横断データは保存され、開き直しても残る（再取得前の表示が消えない）。"""
        store = StatsStore(self.path)
        store._data["account_daily"] = {"2026-06-26": {"characters": 30, "recording_seconds": 4.0, "sessions": 3}}
        store._data["account_total_characters"] = 30
        store._data["account_total_sessions"] = 3
        store._data["account_total_recording_seconds"] = 4.0
        with store._lock:
            store._save()
        reopened = StatsStore(self.path)
        self.assertEqual(reopened._data["account_daily"]["2026-06-26"]["characters"], 30)
        self.assertEqual(reopened.snapshot()["total_characters"], 30)


if __name__ == "__main__":
    unittest.main()
