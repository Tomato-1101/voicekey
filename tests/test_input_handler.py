"""テキスト入力ハンドラ（src/core/input_handler.py）のテスト。

検証する性質は 2 つ:
1. クリップボード復元が「呼び出し元（Enter 自動送信・録音中 UI の非表示）を塞がない」こと。
   復元待ち（RESTORE_DELAY=0.3s）が insert_text の同期パスに残ると、ユーザー体感の
   「文字は入っているのに Enter / UI が 0.5 秒遅れる」を引き起こす。
2. 復元がユーザーのクリップボードを壊さないこと（#13/#24）。
   - 復元待ちの間にユーザーが新しくコピーしたら、その内容を上書きしない。
   - 連続貼り付けでも、自分の挿入テキストではなくユーザーの真のオリジナルを復元する。
"""

import unittest
from unittest import mock

from src.core.input_handler import InputHandler, PASTE_DELAY, RESTORE_DELAY


class _FakeClipboard:
    """copy/paste で値が往復する、最小のクリップボード模擬。"""

    def __init__(self, initial: str = "") -> None:
        self.value = initial

    def paste(self) -> str:
        return self.value

    def copy(self, text: str) -> None:
        self.value = text


class _FakeTimer:
    """Timer を即時実行せず (interval, fn, args) を記録するだけのスタブ。"""

    created: list = []

    def __init__(self, interval, fn, args=()):
        self.interval = interval
        self.fn = fn
        self.args = args
        _FakeTimer.created.append(self)

    def start(self):
        pass

    def fire(self):
        """予約された復元コールバックを手動で実行する。"""
        self.fn(*self.args)


class _ClipboardTestBase(unittest.TestCase):
    def setUp(self):
        _FakeTimer.created = []

    def _insert(self, ih: InputHandler, clip: _FakeClipboard, text: str):
        """clip を背後のクリップボードとして insert_text を 1 回実行する。"""
        with mock.patch("src.core.input_handler.pyperclip") as pc, \
             mock.patch("src.core.input_handler.time.sleep"), \
             mock.patch("src.core.input_handler.threading.Timer", _FakeTimer), \
             mock.patch.object(ih, "_keyboard"):
            pc.paste.side_effect = clip.paste
            pc.copy.side_effect = clip.copy
            ok = ih.insert_text(text)
        return ok

    def _fire(self, timer: _FakeTimer, clip: _FakeClipboard):
        """復元 Timer のコールバックを、clip を背後にして実行する。"""
        with mock.patch("src.core.input_handler.pyperclip") as pc:
            pc.paste.side_effect = clip.paste
            pc.copy.side_effect = clip.copy
            timer.fire()


class TestInsertTextNonBlocking(_ClipboardTestBase):
    """insert_text が復元待ちでブロックしないことを検証する。"""

    def test_restore_is_offloaded_to_timer_not_inline_sleep(self):
        """復元（RESTORE_DELAY 待ち）は同期 sleep ではなく別スレッド Timer に逃がす。"""
        ih = InputHandler()
        clip = _FakeClipboard("ユーザーが前にコピーしていた内容")
        sleeps = []

        with mock.patch("src.core.input_handler.pyperclip") as pc, \
             mock.patch("src.core.input_handler.time.sleep", side_effect=sleeps.append), \
             mock.patch("src.core.input_handler.threading.Timer", _FakeTimer), \
             mock.patch.object(ih, "_keyboard"):
            pc.paste.side_effect = clip.paste
            pc.copy.side_effect = clip.copy
            ok = ih.insert_text("こんにちは")

        self.assertTrue(ok)
        # 同期 sleep は貼り付け前の PASTE_DELAY だけ。RESTORE_DELAY(0.3s) は含まれない
        self.assertEqual(sleeps, [PASTE_DELAY])
        self.assertNotIn(RESTORE_DELAY, sleeps)
        # 復元は RESTORE_DELAY 後にバックグラウンド Timer で予約される
        self.assertEqual(len(_FakeTimer.created), 1)
        self.assertEqual(_FakeTimer.created[0].interval, RESTORE_DELAY)


class TestClipboardRestoreGuard(_ClipboardTestBase):
    """#13/#24: 復元がユーザーのクリップボードを壊さないことを検証する。"""

    def test_restores_user_original(self):
        """通常: 貼り付け後に退避したユーザーのクリップボードへ戻す。"""
        ih = InputHandler()
        clip = _FakeClipboard("USER")
        self.assertTrue(self._insert(ih, clip, "HELLO"))
        self.assertEqual(clip.value, "HELLO")  # 貼り付けで自分のテキストになっている

        self._fire(_FakeTimer.created[0], clip)
        self.assertEqual(clip.value, "USER")  # 復元でユーザーの内容に戻る

    def test_skips_restore_when_user_copies_during_window(self):
        """#13: 復元待ちの間にユーザーが新しくコピーしたら上書きしない。"""
        ih = InputHandler()
        clip = _FakeClipboard("USER")
        self._insert(ih, clip, "HELLO")
        # ユーザーが復元前に別の内容をコピー
        clip.value = "ユーザーが今コピーした大事な内容"

        self._fire(_FakeTimer.created[0], clip)
        # 自分の挿入テキストではないので復元せず、ユーザーの新しい内容を保持
        self.assertEqual(clip.value, "ユーザーが今コピーした大事な内容")

    def test_consecutive_pastes_restore_true_original(self):
        """#24: 連続貼り付けでも、自分の挿入テキストでなく真のオリジナルを復元する。"""
        ih = InputHandler()
        clip = _FakeClipboard("USER")
        self._insert(ih, clip, "FIRST")   # gen1: clip -> FIRST
        self._insert(ih, clip, "SECOND")  # gen2: clip -> SECOND（USER を引き継ぐ）
        self.assertEqual(len(_FakeTimer.created), 2)

        # 古い世代の復元は無効化される（clip は SECOND のまま）
        self._fire(_FakeTimer.created[0], clip)
        self.assertEqual(clip.value, "SECOND")

        # 最新世代の復元は FIRST ではなく USER を書き戻す
        self._fire(_FakeTimer.created[1], clip)
        self.assertEqual(clip.value, "USER")

    def test_empty_clipboard_keeps_injection(self):
        """退避対象が無ければ、復元時に空で上書きせず自分の挿入テキストを残す。"""
        ih = InputHandler()
        clip = _FakeClipboard("")
        self._insert(ih, clip, "X")
        self.assertEqual(clip.value, "X")

        self._fire(_FakeTimer.created[0], clip)
        self.assertEqual(clip.value, "X")  # 空文字で潰さない


if __name__ == "__main__":
    unittest.main()
