"""クリップボードの全形式の退避（Windows）の上限と復元判定のテスト（純ロジック・Win32 には触れない）。"""

import unittest
from unittest import mock

from src.core.input_handler import RestoreDecision, decide_restore
from src.platform.windows import clipboard
from src.platform.windows.clipboard import MAX_FORMATS, ClipboardSnapshot, select_formats


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


class TestOwnerWindow(unittest.TestCase):
    """書き込み経路は NULL ではなくメッセージ専用ウィンドウでクリップボードを開く。

    NULL で開いて EmptyClipboard すると持ち主が NULL になり SetClipboardData が失敗する（Microsoft の仕様）。
    Win32 はこの環境では動かないので、user32 / kernel32 をモックして渡す引数だけ確かめる。
    """

    HWND = 0x1234

    def setUp(self):
        self.user32 = mock.MagicMock()
        self.kernel32 = mock.MagicMock()
        self.user32.CreateWindowExW.return_value = self.HWND
        self.user32.OpenClipboard.return_value = True
        self.user32.SetClipboardData.return_value = 1
        self.user32.EnumClipboardFormats.return_value = 0
        self.kernel32.GlobalAlloc.return_value = 1
        self.kernel32.GlobalLock.return_value = 1
        patches = [
            mock.patch.object(clipboard.sys, "platform", "win32"),
            mock.patch.object(clipboard, "_win32", return_value=(self.user32, self.kernel32)),
            # 偽のポインタへ書き込まないよう memmove も差し替える
            mock.patch.object(clipboard.ctypes, "memmove"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_copy_text_excluded_opens_with_message_window(self):
        self.assertTrue(clipboard.copy_text_excluded("x"))
        self.user32.OpenClipboard.assert_called_with(self.HWND)
        args = self.user32.CreateWindowExW.call_args.args
        self.assertEqual(args[1], "STATIC")
        self.assertEqual(args[8], -3)  # 親 = HWND_MESSAGE（画面に出ない）

    def test_restore_snapshot_opens_with_message_window(self):
        self.assertTrue(clipboard.restore_snapshot(ClipboardSnapshot(items=[(13, b"a\0\0")], had_content=True)))
        self.user32.OpenClipboard.assert_called_with(self.HWND)

    def test_read_only_snapshot_keeps_null_owner(self):
        clipboard.take_snapshot()
        self.user32.OpenClipboard.assert_called_with(None)
        self.user32.CreateWindowExW.assert_not_called()

    def test_window_is_destroyed_after_each_write(self):
        # 持ち主ウィンドウを残すと他のアプリの EmptyClipboard を待たせるので、書くたびに作って壊す
        clipboard.copy_text_excluded("1")
        clipboard.restore_snapshot(ClipboardSnapshot(items=[(13, b"a\0\0")], had_content=True))
        self.assertEqual(self.user32.CreateWindowExW.call_count, 2)
        self.assertEqual(self.user32.DestroyWindow.call_count, 2)
        self.user32.DestroyWindow.assert_called_with(self.HWND)

    def test_window_is_destroyed_when_open_fails(self):
        self.user32.OpenClipboard.return_value = False
        with mock.patch.object(clipboard.time, "sleep"):
            self.assertFalse(clipboard.copy_text_excluded("x"))
        self.user32.DestroyWindow.assert_called_once_with(self.HWND)

    def test_no_window_means_no_write(self):
        self.user32.CreateWindowExW.return_value = None
        self.assertFalse(clipboard.copy_text_excluded("x"))
        self.user32.OpenClipboard.assert_not_called()


if __name__ == "__main__":
    unittest.main()
