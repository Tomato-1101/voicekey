"""クリップボードの全形式の退避（Windows）の上限と復元判定のテスト（純ロジック・Win32 には触れない）。"""

import unittest

from src.core.input_handler import RestoreDecision, decide_restore
from src.platform.windows.clipboard import MAX_FORMATS, select_formats


class TestSelectFormats(unittest.TestCase):
    def test_all_fit(self):
        formats = [(13, "", 100), (49161, "HTML Format", 2000), (8, "", 5_000_000)]
        self.assertEqual(select_formats(formats), ([13, 49161, 8], True))

    def test_skips_gdi_formats_without_counting_as_loss(self):
        formats = [(8, "", 10), (2, "", 0), (14, "", 0)]  # CF_DIB / CF_BITMAP / CF_ENHMETAFILE
        self.assertEqual(select_formats(formats), ([8], True))

    def test_over_budget_keeps_light_formats_first(self):
        big = 40 * 1024 * 1024
        formats = [(8, "", big), (13, "", 10), (49300, "Rich Text Format", 20), (49400, "PNG", 1000)]
        selected, complete = select_formats(formats)
        self.assertFalse(complete)
        self.assertEqual(selected, [13, 49300, 49400])  # 元の順のまま、入らない画像だけ外す

    def test_too_many_formats(self):
        formats = [(50000 + i, f"f{i}", 1) for i in range(MAX_FORMATS + 5)]
        selected, complete = select_formats(formats)
        self.assertEqual(len(selected), MAX_FORMATS)
        self.assertFalse(complete)

    def test_unreadable_formats_excluded(self):
        self.assertEqual(select_formats([(13, "", -1), (1, "", 5)]), ([1], True))


class TestRestoreDecisionWithSnapshot(unittest.TestCase):
    def test_snapshot_restored_first(self):
        self.assertEqual(
            decide_restore(1, 1, True, "text", snapshot_available=True),
            RestoreDecision.RESTORE_SNAPSHOT,
        )

    def test_unsaved_content_is_not_cleared(self):
        # 画像だけがあり、上限超えで 1 つも退避できなかった → 消さずに残す（Mac と同じ）
        self.assertEqual(
            decide_restore(1, 1, True, "", snapshot_available=False, had_unsaved_content=True),
            RestoreDecision.LEAVE_UNSAVED,
        )

    def test_old_behaviour_kept(self):
        self.assertEqual(decide_restore(1, 1, True, ""), RestoreDecision.CLEAR)
        self.assertEqual(decide_restore(1, 1, False, "x", snapshot_available=True),
                         RestoreDecision.LEAVE_USER_CONTENT)
        self.assertEqual(decide_restore(2, 1, True, "x"), RestoreDecision.SKIP)


if __name__ == "__main__":
    unittest.main()
