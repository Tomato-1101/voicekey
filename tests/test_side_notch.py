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

from src.core.clipboard_history import CAN_INCLUDE_MIME, EXCLUDE_MIME
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
    marked_mime_data,
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
        slit = SideNotchSlit()
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
        panel = SideNotchHistoryPanel(history)
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
        panel = SideNotchHistoryPanel(history)
        panel._on_query_changed("会議")
        rows = panel._list_layout.count() - 1
        self.assertEqual(rows, 1)
        panel.deleteLater()

    def test_empty_state_when_no_history(self):
        panel = SideNotchHistoryPanel(_FakeHistory([]))
        panel.refresh()
        # 空状態ラベルが 1 つ入る（stretch を含めて 2 要素）
        self.assertEqual(panel._list_layout.count(), 2)
        panel.deleteLater()


class _FakeClipboard(_FakeHistory):
    """items() と is_running だけ持つクリップボード履歴のダミー。"""

    def __init__(self, items, running=True):
        super().__init__(items)
        self.is_running = running


class MergeEntriesTests(unittest.TestCase):
    _VOICE = [{"text": "声2", "date": "2026-11-20T10:00:00+09:00"},
              {"text": "声1", "date": "2026-11-20T08:00:00+09:00"}]
    _CLIP = [{"text": "コピー", "date": "2026-11-20T09:00:00+09:00"}]

    def test_all_mixes_by_date(self):
        """「すべて」は両方を日時の新しい順に混ぜ、種類の印を付ける。"""
        merged = merge_entries(self._VOICE, self._CLIP, FILTER_ALL)
        self.assertEqual([e["text"] for e in merged], ["声2", "コピー", "声1"])
        self.assertEqual([e["kind"] for e in merged], [FILTER_VOICE, FILTER_CLIPBOARD, FILTER_VOICE])

    def test_single_kind(self):
        """片方だけの絞り込みはその種類だけ（元の並びのまま）。"""
        self.assertEqual([e["text"] for e in merge_entries(self._VOICE, self._CLIP, FILTER_VOICE)], ["声2", "声1"])
        self.assertEqual([e["text"] for e in merge_entries(self._VOICE, self._CLIP, FILTER_CLIPBOARD)], ["コピー"])

    def test_does_not_mutate_input(self):
        """元の dict に kind を書き込まない（ストアの中身を汚さない）。"""
        merge_entries(self._VOICE, self._CLIP, FILTER_ALL)
        self.assertNotIn("kind", self._VOICE[0])
        self.assertNotIn("kind", self._CLIP[0])


class EmptyMessageTests(unittest.TestCase):
    def test_query_message(self):
        self.assertEqual(empty_message(FILTER_ALL, "会議", True), "「会議」に一致する履歴はありません。")

    def test_clipboard_off_note(self):
        """クリップボードを含む絞り込みで記録 OFF なら理由を添える（音声だけのときは添えない）。"""
        self.assertIn("記録しない設定", empty_message(FILTER_ALL, "", False))
        self.assertIn("記録しない設定", empty_message(FILTER_CLIPBOARD, "", False))
        self.assertNotIn("記録しない設定", empty_message(FILTER_VOICE, "", False))
        self.assertNotIn("記録しない設定", empty_message(FILTER_ALL, "", True))


class DisplayTextTests(unittest.TestCase):
    def test_clipboard_is_trimmed_and_joined(self):
        """クリップボードは先頭だけ・改行を詰めて出す。"""
        text = "1行目\n2行目" + "あ" * (CLIP_DISPLAY_CHARS + 50)
        shown = display_text({"text": text, "kind": FILTER_CLIPBOARD})
        self.assertNotIn("\n", shown)
        self.assertTrue(shown.startswith("1行目 2行目"))
        self.assertLessEqual(len(shown), CLIP_DISPLAY_CHARS)

    def test_voice_is_unchanged(self):
        self.assertEqual(display_text({"text": "a\nb", "kind": FILTER_VOICE}), "a\nb")

    def test_marked_mime_data(self):
        """パネルからのコピーは履歴・Win+V に残さない印付き。"""
        mime = marked_mime_data("こんにちは")
        self.assertEqual(mime.text(), "こんにちは")
        self.assertEqual(bytes(mime.data(EXCLUDE_MIME)), b"\x00\x00\x00\x00")
        self.assertEqual(bytes(mime.data(CAN_INCLUDE_MIME)), b"\x00\x00\x00\x00")


class PanelFilterTests(unittest.TestCase):
    def _panel(self, running=True, clips=None):
        history = _FakeHistory([{"text": "声", "date": "2026-11-20T10:00:00+09:00"}])
        clipboard = _FakeClipboard(
            clips if clips is not None else [{"text": "コピー", "date": "2026-11-20T09:00:00+09:00"},
                                             {"text": "コピー2", "date": "2026-11-20T08:00:00+09:00"}],
            running,
        )
        return SideNotchHistoryPanel(history, clipboard)

    def _rows(self, panel):
        return panel._list_layout.count() - 1

    def test_set_filter_changes_rows(self):
        panel = self._panel()
        panel.refresh()
        self.assertEqual(self._rows(panel), 3)
        panel.set_filter(FILTER_VOICE)
        self.assertEqual(self._rows(panel), 1)
        panel.set_filter(FILTER_CLIPBOARD)
        self.assertEqual(self._rows(panel), 2)
        panel.deleteLater()

    def test_unknown_filter_ignored(self):
        panel = self._panel()
        panel.set_filter("nope")
        self.assertEqual(panel._filter, FILTER_ALL)
        panel.deleteLater()

    def test_clipboard_off_empty_shows_reason(self):
        panel = self._panel(running=False, clips=[])
        panel.set_filter(FILTER_CLIPBOARD)
        label = panel._list_layout.itemAt(0).widget()
        self.assertIn("記録しない設定", label.text())
        panel.deleteLater()


class ControllerTests(unittest.TestCase):
    def _make(self, items=None, enabled=True):
        history = _FakeHistory(items or [{"text": "テスト", "date": ""}])
        return SideNotch(history=history, enabled=enabled)

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
