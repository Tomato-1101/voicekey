"""ホームダッシュボード（settings_window の Home ページ）のロジックテスト。

検証する性質:
1. 節約時間の換算（_saved_comparison）が Mac 版と同じしきい値で段階を選ぶ。
2. 3 桁区切り（_grouped）。
3. _refresh_home が StatsStore.snapshot / daily_series と HistoryStore.items から
   累計・節約・期間・最近の履歴を正しく表示に反映する。
4. select_home がホームページ（先頭）を選ぶ。今日/今週トグルで期間表示が切り替わる。

Qt はオフスクリーンで動かし、keyring / 音声デバイス / レジストリは実体に触れずモックする。
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import tempfile
import unittest
from unittest import mock

import yaml
from PySide6.QtWidgets import QApplication

from src.config.config_manager import ConfigManager
from src.ui.settings_window import SettingsWindow

_app = QApplication.instance() or QApplication([])


class _FakeStats:
    """snapshot / daily_series だけ持つ StatsStore のダミー。"""

    def __init__(self, snap, series):
        self._snap = snap
        self._series = series  # 7 日ぶんの日次バケット（古い順・末尾が今日）

    def snapshot(self):
        return dict(self._snap)

    def daily_series(self, num_days, end_day=None):
        return self._series[-num_days:]


class _FakeHistory:
    def __init__(self, items):
        self._items = items

    def items(self):
        return list(self._items)


def _day(chars, sessions, rec):
    return {"day": "2026-07-15", "characters": chars, "sessions": sessions, "recording_seconds": rec}


class SavedComparisonTests(unittest.TestCase):
    def test_thresholds(self):
        f = SettingsWindow._saved_comparison
        self.assertEqual(f(0), "これから時間が積み上がっていきます")
        self.assertEqual(f(100), "これから時間が積み上がっていきます")
        self.assertEqual(f(180), "カップ麺 1 個ぶんの待ち時間")
        self.assertEqual(f(1800), "通勤 1 回ぶんの移動時間")
        self.assertEqual(f(7200), "映画 1 本ぶんの尺")
        self.assertEqual(f(28800), "ぐっすり睡眠 1 回ぶん")
        self.assertEqual(f(86400), "まるっと 1 日ぶんの時間")

    def test_grouped(self):
        self.assertEqual(SettingsWindow._grouped(0), "0")
        self.assertEqual(SettingsWindow._grouped(5000), "5,000")
        self.assertEqual(SettingsWindow._grouped(1234567), "1,234,567")


class HomeRefreshTests(unittest.TestCase):
    def setUp(self):
        self._paths = []
        self._windows = []
        from src.core import login_coordinator
        login_coordinator._shared = None

    def tearDown(self):
        for w in self._windows:
            w.deleteLater()
        for p in self._paths:
            try:
                os.unlink(p)
            except OSError:
                pass

    def _config(self):
        handle = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8")
        yaml.dump({}, handle, allow_unicode=True)
        handle.close()
        self._paths.append(handle.name)
        return ConfigManager(config_path=handle.name)

    def _make_window(self, stats, history):
        cm = self._config()
        with mock.patch(
            "src.ui.settings_window.secrets.is_keyring_available", return_value=False
        ), mock.patch(
            "src.ui.settings_window.AudioRecorder.list_input_devices", return_value=[]
        ), mock.patch(
            "src.ui.settings_window.autostart.is_supported", return_value=True
        ), mock.patch(
            "src.ui.settings_window.autostart.is_enabled", return_value=False
        ), mock.patch(
            "src.utils.secrets.get_auth_session", return_value=None
        ):
            win = SettingsWindow(config_manager=cm, stats=stats, history=history)
        self._windows.append(win)
        return win

    def _stats(self):
        # 7 日ぶん。末尾（今日）だけ入力あり
        series = [_day(0, 0, 0.0)] * 6 + [_day(100, 3, 30.0)]
        snap = {
            "total_characters": 5000,
            "total_sessions": 50,
            "total_recording_seconds": 600.0,
            "saved_seconds": 8000.0,  # 7200<=s<28800 ＝ 映画 1 本
            "level": 5,
            "level_progress": 0.4,
            "xp": 5000,
            "xp_to_next_level": 250,
            "current_streak": 3,
            "longest_streak": 7,
        }
        return _FakeStats(snap, series)

    def test_refresh_populates_cards(self):
        win = self._make_window(self._stats(), _FakeHistory([]))
        win._refresh_home()
        self.assertEqual(win._home_greeting.text(), "今日はここまで 100 文字を入力しました")
        self.assertEqual(win._home_total_chars.text(), "5,000")
        self.assertEqual(win._home_level_label.text(), "レベル 5")
        self.assertEqual(win._home_level_progress.value(), 40)
        self.assertEqual(win._home_saved_comparison.text(), "映画 1 本ぶんの尺")
        # 期間（既定=今日）: daily_series(1) の末尾 100 文字
        self.assertEqual(win._home_period_chars.text(), "100")

    def test_period_toggle_switches(self):
        win = self._make_window(self._stats(), _FakeHistory([]))
        win._refresh_home()
        # 今週へ切替 → 7 日合計 = 100
        win._set_home_period(7)
        self.assertEqual(win._home_period_chars.text(), "100")
        self.assertTrue(win._home_week_btn.isChecked())
        self.assertFalse(win._home_today_btn.isChecked())

    def test_greeting_when_no_input_today(self):
        series = [_day(0, 0, 0.0)] * 7
        snap = {
            "total_characters": 0, "total_sessions": 0, "total_recording_seconds": 0.0,
            "saved_seconds": 0.0, "level": 1, "level_progress": 0.0, "xp": 0,
            "xp_to_next_level": 0, "current_streak": 0, "longest_streak": 0,
        }
        win = self._make_window(_FakeStats(snap, series), _FakeHistory([]))
        win._refresh_home()
        self.assertEqual(win._home_greeting.text(), "今日はまだ入力していません")

    def test_recent_history_listed(self):
        history = _FakeHistory([
            {"text": "さいきんの入力", "date": "2026-07-15T09:00:00+09:00"},
        ])
        win = self._make_window(self._stats(), history)
        win._refresh_home()
        self.assertEqual(win._home_history_list.count(), 1)

    def test_recent_history_empty_placeholder(self):
        win = self._make_window(self._stats(), _FakeHistory([]))
        win._refresh_home()
        self.assertEqual(win._home_history_list.count(), 1)  # 案内行 1 つ

    def test_select_home_selects_first_row(self):
        win = self._make_window(self._stats(), _FakeHistory([]))
        win._nav.setCurrentRow(2)
        win.select_home()
        self.assertEqual(win._nav.currentRow(), win._home_page_index)
        self.assertEqual(win._home_page_index, 0)


if __name__ == "__main__":
    unittest.main()
