"""サイドノッチ（src/ui/side_notch.py）のロジック・状態テスト。

検証する性質:
1. 相対時刻ヘルパ: たった今/N 分前/N 時間前/N 日前 と不正入力の空文字。
2. 履歴フィルタ: テキスト部分一致（大文字小文字無視）・空クエリ全件。
3. スリットの録音状態トグル（値変化のみ再描画）。
4. 履歴パネル: 検索フィルタで一覧が絞られる・空状態メッセージ。
5. コントローラ: 表示トグル・録音状態中継・トグル開閉・「ホームを開く」中継。

Qt はオフスクリーンで動かし、実画面・実デバイス・DWM には触れない。
"""

import os
from datetime import datetime, timedelta

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import unittest

from PySide6.QtWidgets import QApplication

from src.ui.side_notch import (
    SideNotch,
    SideNotchHistoryPanel,
    SideNotchSlit,
    filter_items,
    relative_time,
)

_app = QApplication.instance() or QApplication([])


class _FakeHistory:
    """items() だけ持つ履歴ストアのダミー。"""

    def __init__(self, items):
        self._items = items

    def items(self):
        return list(self._items)


class RelativeTimeTests(unittest.TestCase):
    def test_just_now(self):
        now = datetime.now().astimezone()
        recent = (now - timedelta(seconds=5)).isoformat()
        self.assertEqual(relative_time(recent, now=now), "たった今")

    def test_minutes(self):
        now = datetime.now().astimezone()
        past = (now - timedelta(minutes=3)).isoformat()
        self.assertEqual(relative_time(past, now=now), "3 分前")

    def test_hours(self):
        now = datetime.now().astimezone()
        past = (now - timedelta(hours=2)).isoformat()
        self.assertEqual(relative_time(past, now=now), "2 時間前")

    def test_days(self):
        now = datetime.now().astimezone()
        past = (now - timedelta(days=3)).isoformat()
        self.assertEqual(relative_time(past, now=now), "3 日前")

    def test_invalid_returns_empty(self):
        self.assertEqual(relative_time(""), "")
        self.assertEqual(relative_time("not-a-date"), "")


class FilterItemsTests(unittest.TestCase):
    def setUp(self):
        self.items = [
            {"text": "おはよう ございます", "date": ""},
            {"text": "Hello World", "date": ""},
            {"text": "会議の議事録", "date": ""},
        ]

    def test_empty_query_returns_all(self):
        self.assertEqual(len(filter_items(self.items, "")), 3)
        self.assertEqual(len(filter_items(self.items, "   ")), 3)

    def test_case_insensitive_match(self):
        result = filter_items(self.items, "hello")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["text"], "Hello World")

    def test_japanese_substring(self):
        result = filter_items(self.items, "議事")
        self.assertEqual(len(result), 1)

    def test_no_match(self):
        self.assertEqual(filter_items(self.items, "xyz"), [])


class SlitTests(unittest.TestCase):
    def test_recording_state_changes(self):
        slit = SideNotchSlit(is_dark=True)
        self.assertFalse(slit._recording)
        slit.set_recording(True)
        self.assertTrue(slit._recording)
        slit.set_recording(True)  # 冪等
        self.assertTrue(slit._recording)
        slit.set_recording(False)
        self.assertFalse(slit._recording)
        slit.deleteLater()


class HistoryPanelTests(unittest.TestCase):
    def test_refresh_lists_all(self):
        history = _FakeHistory([
            {"text": "ひとつめ", "date": ""},
            {"text": "ふたつめ", "date": ""},
        ])
        panel = SideNotchHistoryPanel(history, is_dark=False)
        panel.refresh()
        # 行数 = 一覧レイアウトの (widget 数 - 末尾 stretch)
        rows = panel._list_layout.count() - 1
        self.assertEqual(rows, 2)
        panel.deleteLater()

    def test_search_filters(self):
        history = _FakeHistory([
            {"text": "会議メモ", "date": ""},
            {"text": "買い物リスト", "date": ""},
        ])
        panel = SideNotchHistoryPanel(history, is_dark=False)
        panel._on_query_changed("会議")
        rows = panel._list_layout.count() - 1
        self.assertEqual(rows, 1)
        panel.deleteLater()

    def test_empty_state_when_no_history(self):
        panel = SideNotchHistoryPanel(_FakeHistory([]), is_dark=False)
        panel.refresh()
        # 空状態ラベルが 1 つ入る（stretch を含めて 2 要素）
        self.assertEqual(panel._list_layout.count(), 2)
        panel.deleteLater()


class ControllerTests(unittest.TestCase):
    def _make(self, items=None, enabled=True):
        history = _FakeHistory(items or [{"text": "テスト", "date": ""}])
        return SideNotch(history=history, is_dark=False, enabled=enabled)

    def test_enabled_shows_slit(self):
        ctrl = self._make(enabled=True)
        self.assertTrue(ctrl._slit.isVisible())
        ctrl.set_enabled(False)
        self.assertFalse(ctrl._slit.isVisible())

    def test_set_status_recording(self):
        ctrl = self._make()
        ctrl.set_status("recording")
        self.assertTrue(ctrl._slit._recording)
        ctrl.set_status("idle")
        self.assertFalse(ctrl._slit._recording)

    def test_toggle_opens_and_closes(self):
        ctrl = self._make()
        ctrl.open_panel()
        self.assertIsNotNone(ctrl._panel)
        self.assertTrue(ctrl._panel.isVisible())
        ctrl.toggle_panel()  # 開いている → 閉じる
        self.assertFalse(ctrl._panel.isVisible())

    def test_open_home_relays_signal(self):
        ctrl = self._make()
        received = {"n": 0}
        ctrl.open_home_requested.connect(lambda: received.__setitem__("n", received["n"] + 1))
        ctrl.open_panel()
        ctrl._on_open_home()
        self.assertEqual(received["n"], 1)
        self.assertFalse(ctrl._panel.isVisible())  # ホームを開く前にパネルを閉じる

    def test_disabled_does_not_open(self):
        ctrl = self._make(enabled=False)
        ctrl.open_panel()
        self.assertTrue(ctrl._panel is None or not ctrl._panel.isVisible())


if __name__ == "__main__":
    unittest.main()
