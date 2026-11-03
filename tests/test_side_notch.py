"""サイドノッチ（src/ui/side_notch.py）のロジック・状態テスト。

検証する性質:
1. 相対時刻ヘルパ: たった今/N 分前/N 時間前/N 日前 と不正入力の空文字。
2. 履歴フィルタ: テキスト部分一致（大文字小文字無視）・空クエリ全件。
3. スリットの録音状態トグル（値変化のみ再描画）。
4. 履歴パネル: 検索フィルタで一覧が絞られる・空状態メッセージ。
5. コントローラ: 表示トグル・録音状態中継・トグル開閉・「ホームを開く」中継。
6. クリップボード履歴: すべて／音声入力／クリップボードの絞り込み・日時順の混在・空状態の文言・
   クリップボード行の表示の切り詰め。

Qt はオフスクリーンで動かし、実画面・実デバイス・DWM には触れない。
"""

import os
from datetime import datetime, timedelta

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import unittest

from PySide6.QtWidgets import QApplication

from src.ui.side_notch import (
    CLIP_DISPLAY_CHARS,
    FILTER_ALL,
    FILTER_CLIPBOARD,
    FILTER_VOICE,
    SideNotch,
    SideNotchHistoryPanel,
    SideNotchSlit,
    display_text,
    empty_message,
    filter_items,
    merge_entries,
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


class HistoryRowTests(unittest.TestCase):
    def test_text_is_plain_not_html(self):
        # コピーした本文に HTML のタグがあっても装飾せずそのまま見せる
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QLabel

        from src.ui.side_notch import _HistoryRow

        row = _HistoryRow({"text": "<b>太字</b>", "date": ""}, is_dark=False)
        labels = row.findChildren(QLabel)
        text_label = next(label for label in labels if label.text() == "<b>太字</b>")
        self.assertEqual(text_label.textFormat(), Qt.TextFormat.PlainText)
        row.deleteLater()


class _FakeClipboardHistory(_FakeHistory):
    """items() と is_running だけ持つクリップボード履歴ストアのダミー。"""

    def __init__(self, items, running=True):
        super().__init__(items)
        self.is_running = running


_VOICE = [
    {"text": "音声 新", "date": "2026-11-03T10:00:00+09:00"},
    {"text": "音声 古", "date": "2026-11-03T08:00:00+09:00"},
]
_CLIPS = [
    {"text": "コピー 中", "date": "2026-11-03T09:00:00+09:00", "app_name": "メモ帳"},
]


class ClipboardMergeTests(unittest.TestCase):
    def test_all_merges_by_date_desc(self):
        merged = merge_entries(_VOICE, _CLIPS, FILTER_ALL)
        self.assertEqual([e["text"] for e in merged], ["音声 新", "コピー 中", "音声 古"])
        self.assertEqual([e["kind"] for e in merged], ["voice", "clipboard", "voice"])

    def test_single_kind_filters(self):
        self.assertEqual([e["text"] for e in merge_entries(_VOICE, _CLIPS, FILTER_VOICE)], ["音声 新", "音声 古"])
        self.assertEqual([e["text"] for e in merge_entries(_VOICE, _CLIPS, FILTER_CLIPBOARD)], ["コピー 中"])

    def test_unparseable_date_goes_last(self):
        merged = merge_entries([{"text": "日時なし", "date": ""}], _CLIPS, FILTER_ALL)
        self.assertEqual([e["text"] for e in merged], ["コピー 中", "日時なし"])

    def test_search_applies_to_both_kinds(self):
        merged = merge_entries(_VOICE, _CLIPS, FILTER_ALL)
        self.assertEqual([e["text"] for e in filter_items(merged, "メモ帳")], ["コピー 中"])
        self.assertEqual(len(filter_items(merged, "音声")), 2)

    def test_empty_messages(self):
        self.assertIn("音声入力やコピーをすると", empty_message(FILTER_ALL, "", True))
        self.assertIn("音声入力すると", empty_message(FILTER_VOICE, "", False))
        self.assertNotIn("記録しない設定", empty_message(FILTER_VOICE, "", False))
        self.assertIn("コピーすると", empty_message(FILTER_CLIPBOARD, "", True))
        self.assertIn("記録しない設定", empty_message(FILTER_CLIPBOARD, "", False))
        self.assertIn("記録しない設定", empty_message(FILTER_ALL, "", False))
        self.assertEqual(empty_message(FILTER_ALL, "abc", False), "「abc」に一致する履歴はありません。")

    def test_clipboard_display_truncated_and_joined(self):
        long_text = "あ" * (CLIP_DISPLAY_CHARS + 100)
        self.assertEqual(len(display_text({"text": long_text, "kind": "clipboard"})), CLIP_DISPLAY_CHARS)
        self.assertEqual(display_text({"text": "一行目\n二行目", "kind": "clipboard"}), "一行目 二行目")
        # 音声入力の行はそのまま
        self.assertEqual(display_text({"text": "一行目\n二行目", "kind": "voice"}), "一行目\n二行目")

    def test_panel_filter_switch(self):
        panel = SideNotchHistoryPanel(_FakeHistory(_VOICE), is_dark=False, clipboard=_FakeClipboardHistory(_CLIPS))
        panel.refresh()
        self.assertEqual(panel._list_layout.count() - 1, 3)
        panel.set_filter(FILTER_CLIPBOARD)
        self.assertEqual(panel._list_layout.count() - 1, 1)
        self.assertTrue(panel._filter_buttons[FILTER_CLIPBOARD].isChecked())
        panel.set_filter(FILTER_VOICE)
        self.assertEqual(panel._list_layout.count() - 1, 2)
        panel.deleteLater()

    def test_controller_opens_with_all(self):
        ctrl = SideNotch(
            history=_FakeHistory(_VOICE), is_dark=False, enabled=True, clipboard=_FakeClipboardHistory(_CLIPS)
        )
        ctrl.open_panel()
        ctrl._panel.set_filter(FILTER_VOICE)
        ctrl.toggle_panel()
        ctrl._last_closed_at = 0.0
        ctrl.open_panel()
        # 開き直すと「すべて」に戻る（Mac はパネルを作り直すので同じ動き）
        self.assertEqual(ctrl._panel._filter, FILTER_ALL)
        self.assertEqual(ctrl._panel._list_layout.count() - 1, 3)
        ctrl.set_enabled(False)


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
