"""
Meet ボットの判断ロジックのテスト（ボットだけが残ったときの自動退出）

時刻を引数で渡して、60 秒・15 分の境目を実時間で待たずに確かめる。
"""

import unittest

from src.meetbot import rules
from src.meetbot.rules import LonelyMeetingWatch


class LonelyMeetingWatchTests(unittest.TestCase):
    """ほかの参加者がいなくなった／だれも来なかったときの退出。"""

    def test_leaves_after_alone_for_a_minute(self):
        """ほかの人を見たあと、1 人が 59 秒では退出せず、60 秒で退出する。"""
        watch = LonelyMeetingWatch(0.0)
        self.assertIsNone(watch.observe(2, 10.0))
        self.assertIsNone(watch.observe(1, 20.0))
        self.assertIsNone(watch.observe(1, 79.0))
        self.assertEqual(watch.observe(1, 80.0), rules.REASON_ALONE)

    def test_someone_coming_back_resets(self):
        """途中で 2 人に戻ったら、1 人の連続を数え直す。"""
        watch = LonelyMeetingWatch(0.0)
        watch.observe(2, 0.0)
        watch.observe(1, 10.0)
        self.assertIsNone(watch.observe(2, 60.0))
        self.assertIsNone(watch.observe(1, 70.0))
        self.assertIsNone(watch.observe(1, 129.0))
        self.assertEqual(watch.observe(1, 130.0), rules.REASON_ALONE)

    def test_unreadable_count_resets(self):
        """人数を読めなかった回（0）が挟まったら、1 人の連続を数え直す。"""
        watch = LonelyMeetingWatch(0.0)
        watch.observe(2, 0.0)
        watch.observe(1, 10.0)
        self.assertIsNone(watch.observe(0, 60.0))
        self.assertIsNone(watch.observe(1, 71.0))
        self.assertIsNone(watch.observe(1, 130.0))
        self.assertEqual(watch.observe(1, 131.0), rules.REASON_ALONE)

    def test_nobody_came_for_fifteen_minutes(self):
        """だれも来ないまま 14 分 59 秒では退出せず、15 分で退出する。"""
        watch = LonelyMeetingWatch(100.0)
        self.assertIsNone(watch.observe(1, 100.0))
        self.assertIsNone(watch.observe(1, 100.0 + 899.0))
        self.assertEqual(watch.observe(1, 100.0 + 900.0), rules.REASON_NOBODY)

    def test_unreadable_count_never_leaves(self):
        """人数が読めないままなら、15 分たっても退出しない。"""
        watch = LonelyMeetingWatch(0.0)
        for now in (0.0, 600.0, 900.0, 3600.0):
            self.assertIsNone(watch.observe(0, now))

    def test_participants_from_payload(self):
        """人数は整数のときだけ読む（無い・壊れている・真偽値は 0）。"""
        self.assertEqual(rules.participants_from_payload({"participants": 2}), 2)
        self.assertEqual(rules.participants_from_payload({}), 0)
        self.assertEqual(rules.participants_from_payload({"participants": True}), 0)
        self.assertEqual(rules.participants_from_payload(None), 0)


if __name__ == "__main__":
    unittest.main()
