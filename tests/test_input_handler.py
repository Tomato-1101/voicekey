"""テキスト入力ハンドラ（src/core/input_handler.py）のテスト。

主眼はクリップボード復元が「呼び出し元（Enter 自動送信・録音中 UI の非表示）を
塞がない」こと。復元待ち（RESTORE_DELAY=0.3s）が insert_text の同期パスに残ると、
ユーザー体感の「文字は入っているのに Enter / UI が 0.5 秒遅れる」を引き起こす。
"""

import unittest
from unittest import mock

from src.core.input_handler import InputHandler, PASTE_DELAY, RESTORE_DELAY


class TestInsertTextNonBlocking(unittest.TestCase):
    """insert_text が復元待ちでブロックしないことを検証する。"""

    def test_restore_is_offloaded_to_timer_not_inline_sleep(self):
        """復元（RESTORE_DELAY 待ち）は同期 sleep ではなく別スレッド Timer に逃がす。"""
        ih = InputHandler()
        sleeps = []
        timers = []

        class _FakeTimer:
            def __init__(self, interval, fn, args=()):
                timers.append((interval, fn, args))

            def start(self):
                pass

        with mock.patch("src.core.input_handler.pyperclip") as pc, \
             mock.patch("src.core.input_handler.time.sleep", side_effect=sleeps.append), \
             mock.patch("src.core.input_handler.threading.Timer", _FakeTimer), \
             mock.patch.object(ih, "_keyboard"):
            pc.paste.return_value = "ユーザーが前にコピーしていた内容"
            ok = ih.insert_text("こんにちは")

        self.assertTrue(ok)
        # 同期 sleep は貼り付け前の PASTE_DELAY だけ。RESTORE_DELAY(0.3s) は含まれない
        self.assertEqual(sleeps, [PASTE_DELAY])
        self.assertNotIn(RESTORE_DELAY, sleeps)
        # 復元は RESTORE_DELAY 後にバックグラウンド Timer で予約される
        self.assertEqual(len(timers), 1)
        self.assertEqual(timers[0][0], RESTORE_DELAY)
        self.assertEqual(timers[0][2], ("ユーザーが前にコピーしていた内容",))

    def test_no_restore_timer_when_clipboard_was_empty(self):
        """退避すべき内容が無ければ復元 Timer も張らない。"""
        ih = InputHandler()
        timers = []

        class _FakeTimer:
            def __init__(self, *a, **k):
                timers.append(a)

            def start(self):
                pass

        with mock.patch("src.core.input_handler.pyperclip") as pc, \
             mock.patch("src.core.input_handler.time.sleep"), \
             mock.patch("src.core.input_handler.threading.Timer", _FakeTimer), \
             mock.patch.object(ih, "_keyboard"):
            pc.paste.return_value = ""
            ih.insert_text("テスト")

        self.assertEqual(timers, [])

    def test_restore_clipboard_copies_back(self):
        """_restore_clipboard は退避内容をクリップボードに書き戻す。"""
        with mock.patch("src.core.input_handler.pyperclip") as pc:
            InputHandler._restore_clipboard("元の内容")
        pc.copy.assert_called_once_with("元の内容")


if __name__ == "__main__":
    unittest.main()
