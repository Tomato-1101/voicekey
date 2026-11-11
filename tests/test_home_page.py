"""ダッシュボード（src/ui/pages/home.py）のテスト。

検証する性質:
1. 構築ではデータを読まず、refresh() で数字と一覧が入る（ページを開くのを軽く保つ）。
2. データが空でも最大（大きい数字・履歴 8 件・長い文）でも、カードの高さと位置が変わらない（恒久要件）。
3. マイクテストは開始でタイマーが回り、ページが隠れたら必ず止まる（実マイクは使わず偽物に差し替える）。
4. 履歴の行を押すと全文がコピーされ、「API キーを設定」「もう一度見る」が正しい行き先へ知らせる。

Qt はオフスクリーンで動かす。料金・統計・履歴は一時ディレクトリのファイルだけを使い、
キーチェーン（has_any_api_key）は差し替えて実ストアを読まない。課金 API・通信は使わない。
"""

import os

# PySide6 の import より前に必ずオフスクリーン指定する
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QApplication

from src.core import api_pricing
from src.core.api_usage import ApiUsageStore
from src.core.history import HistoryStore
from src.core.stats import StatsStore
from src.ui.pages import home
from src.ui.pages.context import PageContext

# QApplication はプロセスに 1 つ。各テストで使い回す
_app = QApplication.instance() or QApplication([])


class _FakeConfig:
    """PageContext に渡す設定の偽物（読むだけ。保存は成功したことにする）。"""

    def __init__(self, data=None):
        self.data = dict(data or {})

    def get(self, key, default=None):
        return self.data.get(key, default)

    def save(self, changes):
        self.data.update(changes)
        return True


class _FakeMonitor:
    """MicLevelMonitor の偽物（実マイクを開かない）。"""

    def __init__(self, ok=True):
        self.ok = ok
        self.running = False
        self.started_with = None
        self.stop_calls = 0

    def start(self, device="default"):
        self.started_with = device
        self.running = self.ok
        return self.ok

    def read_level(self):
        return 0.5 if self.running else 0.0

    @property
    def is_running(self):
        return self.running

    def stop(self):
        self.stop_calls += 1
        self.running = False


class _HomeTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.stats = StatsStore(self.dir / "stats.json")
        self.usage = ApiUsageStore(self.dir / "api-usage.json")
        self.history = HistoryStore(self.dir / "history.json")
        self.ctx = PageContext(
            _FakeConfig({"audio_input_device": "default"}),
            history=self.history, stats=self.stats, api_usage=self.usage,
        )
        self._key_patch = mock.patch("src.ui.pages.home.engines.has_any_api_key", return_value=False)
        self.has_key = self._key_patch.start()
        self.pages = []

    def tearDown(self):
        for page in self.pages:
            page.close()
            page.deleteLater()
        _app.processEvents()
        self._key_patch.stop()
        self._tmp.cleanup()

    def _page(self, width=720, height=600):
        page = home.HomePage(self.ctx)
        self.pages.append(page)
        page.resize(width, height)
        page.show()
        _app.processEvents()
        return page

    def _fill_max(self):
        """大きい数字・料金の内訳たくさん・長い履歴 8 件を入れる。"""
        for _ in range(3):
            self.stats.record_session(333_333_333, 3600 * 400)
        for index in range(7):
            self.stats.record_session(
                999_999_999 - index, 60, f"app{index}", "とても長い名前のアプリケーション" * 4, ""
            )
        now = datetime.now()
        for day in range(40):
            self.usage.record_stt("soniox", "stt-rt-v5", 3600 * 50, now=now - timedelta(days=day))
        self.usage.record_stt("openai", "gpt-transcribe", 1234.5)
        self.usage.record_stt("groq", "whisper-large-v3-turbo", 0.5)
        self.usage.record_stt("elevenlabs", "scribe_v2", 600)
        self.usage.record_stt("azure_mai", "mai-transcribe-2", 600)
        self.usage.record_stt("unknown_engine", "mystery-model-with-a-very-long-name-v123", 600)
        self.usage.record_llm("groq", "openai/gpt-oss-20b", "formatting", 9_876_543, 1_234_567)
        self.usage.record_llm("gemini", "gemini-x", "formatting", 1000, 1000)
        long_text = "とても長い音声入力の履歴です。" * 40
        for index in range(12):
            self.history.add(f"{index}: {long_text}")

    def _geometry(self, page):
        """カード・主な欄の (y, 高さ) を、ページの左上を原点に集める。"""
        widgets = list(page.stats_cards) + [card for card, _v, _n in page.cost_totals.values()]
        widgets += list(page.cost_cards) + list(page.tool_cards) + [page.apps_card, page.history_card, page.paid_note]
        out = []
        for widget in widgets:
            top = widget.mapTo(page.scroll.widget(), QPoint(0, 0)).y()
            out.append((top, widget.height()))
        return out


class TestFormatting(unittest.TestCase):
    """表記の関数（Mac の HomeView と同じ規則）。"""

    def test_duration(self):
        self.assertEqual(home.format_duration(5), "5秒")
        self.assertEqual(home.format_duration(1313), "21分53秒")
        self.assertEqual(home.format_duration(78180), "21時間43分")

    def test_saved_time_message(self):
        self.assertEqual(home.saved_time_message(0), "これから時間が積み上がっていきます")
        self.assertEqual(home.saved_time_message(400), "カップ麺 2 個ぶんの待ち時間")
        self.assertEqual(home.saved_time_message(3600), "通勤 2 回ぶんの移動時間")
        self.assertEqual(home.saved_time_message(24600), "映画 3 本ぶんの尺")
        self.assertEqual(home.saved_time_message(30000), "ぐっすり睡眠 1 回ぶん")
        self.assertEqual(home.saved_time_message(200000), "まるっと 2 日ぶんの時間")
        self.assertEqual(
            [home.saved_time_icon(s) for s in (0, 400, 3600, 24600, 30000, 200000)],
            ["hourglass", "cup", "tram", "film", "bed", "sun"],
        )

    def test_relative_time(self):
        now = datetime(2026, 11, 11, 12, 0, 0).astimezone()
        self.assertEqual(home.relative_time((now - timedelta(seconds=5)).isoformat(), now), "5 秒前")
        self.assertEqual(home.relative_time((now - timedelta(minutes=3)).isoformat(), now), "3 分前")
        self.assertEqual(home.relative_time((now - timedelta(hours=2)).isoformat(), now), "2 時間前")
        self.assertEqual(home.relative_time((now - timedelta(days=1)).isoformat(), now), "1 日前")
        self.assertEqual(home.relative_time("壊れた値", now), "壊れた値")

    def test_usage_amount(self):
        self.assertEqual(
            home._usage_amount({"input_tokens": 1200, "output_tokens": 30, "audio_seconds": 0, "billed_audio_seconds": 0}),
            "入力 1,200 / 出力 30 トークン",
        )
        self.assertEqual(
            home._usage_amount({"input_tokens": 0, "output_tokens": 0, "audio_seconds": 2, "billed_audio_seconds": 10}),
            "2秒（課金対象 10秒）",
        )


class TestHomePage(_HomeTestBase):
    def test_construct_does_not_read_data(self):
        """構築だけでは統計・料金・キーチェーンを読まない（重い処理は refresh で）。"""
        with mock.patch.object(self.stats, "snapshot") as snap, mock.patch.object(self.usage, "today_summary") as cost:
            home.HomePage(self.ctx).deleteLater()
        snap.assert_not_called()
        cost.assert_not_called()
        self.has_key.assert_not_called()

    def test_empty_state(self):
        """データが空なら、空の表示が同じ枠の中に出る。"""
        page = self._page()
        page.refresh()
        self.assertEqual(page.subtitle.text(), "今日はまだ入力していません")
        self.assertEqual(page.total_chars.text(), "0")
        self.assertEqual(page.history_stack.currentIndex(), 1)
        self.assertEqual(page.breakdown_stack.currentIndex(), 1)
        self.assertEqual(page.api_key_slot.currentIndex(), 1)
        self.assertEqual(page.cost_totals["today"][1].text(), "$0.00")

    def test_values(self):
        """統計と料金の数字が入る。"""
        self.stats.record_session(6767, 60)
        self.usage.record_stt("soniox", "stt-rt-v5", 3600)
        self.history.add("こんにちは")
        self.has_key.return_value = True
        page = self._page()
        page.refresh()
        self.assertEqual(page.subtitle.text(), "今日はここまで 6767 文字を入力しました")
        self.assertEqual(page.total_chars.text(), "6,767")
        self.assertEqual(page.period_chars.text(), "6,767")
        self.assertEqual(page.cost_totals["today"][1].text(), "$0.12")
        self.assertEqual(page.breakdown_stack.currentIndex(), 0)
        self.assertEqual(page.breakdown_rows[0].title.text(), "Soniox · stt-rt-v5")
        self.assertEqual(page.breakdown_rows[0].detail.text(), "文字起こし · 1時間0分 · 1 回")
        self.assertEqual(page.breakdown_rows[0].amount.text(), "$0.12")
        self.assertEqual(page.history_stack.currentIndex(), 0)
        self.assertEqual(page.history_rows[0].body.text(), "こんにちは")
        self.assertEqual(page.api_key_slot.currentIndex(), 0)

    def test_paid_mode(self):
        """「実際に払った分」では無料枠のプロバイダーが「無料枠」になり、注記が出る。"""
        self.usage.record_stt("groq", "whisper-large-v3-turbo", 60)
        page = self._page()
        page.refresh()
        self.assertEqual(page.paid_note.text(), "")
        page.cost_mode_switch.set_current_index(1)
        self.assertEqual(page._cost_mode(), api_pricing.MODE_PAID)
        self.assertEqual(page.breakdown_rows[0].amount.text(), "無料枠")
        self.assertNotEqual(page.paid_note.text(), "")

    def test_switches_are_remembered(self):
        """切替（期間・料金の数え方・内訳の期間）は設定に残り、次に開いたときも同じ表示になる。"""
        page = self._page()
        page.refresh()
        page.period_switch.set_current_index(1)
        page.cost_mode_switch.set_current_index(1)
        page.cost_period_switch.set_current_index(2)
        config = self.ctx.config_manager
        self.assertEqual(config.get("home_period_days"), 7)
        self.assertEqual(config.get("api_cost_mode"), api_pricing.MODE_PAID)
        self.assertEqual(config.get("home_api_cost_period"), "all")
        again = self._page()
        self.assertEqual(again.period_switch.current_index(), 1)
        self.assertEqual(again.cost_mode_switch.current_index(), 1)
        self.assertEqual(again.cost_period_switch.current_index(), 2)

    def test_broken_switch_setting_falls_back(self):
        """保存された切替の値が壊れていても、既定の表示で開く。"""
        self.ctx.config_manager.data.update({"home_period_days": 99, "api_cost_mode": "?", "home_api_cost_period": None})
        page = self._page()
        self.assertEqual(page.period_switch.current_index(), 0)
        self.assertEqual(page.cost_mode_switch.current_index(), 0)
        self.assertEqual(page.cost_period_switch.current_index(), 1)

    def test_layout_does_not_move(self):
        """空と最大で、各カードの y 座標と高さが 1px も変わらない（幅 720 と 520）。"""
        for width in (720, 520):
            with self.subTest(width=width):
                page = self._page(width)
                page.refresh()
                _app.processEvents()
                empty = self._geometry(page)
                self._fill_max()
                page.refresh()
                _app.processEvents()
                full = self._geometry(page)
                # 切替を動かしても同じ
                page.cost_mode_switch.set_current_index(1, animate=False)
                page.cost_period_switch.set_current_index(2, animate=False)
                page.period_switch.set_current_index(1, animate=False)
                page._copy_history(0)
                _app.processEvents()
                switched = self._geometry(page)
                self.assertEqual(empty, full)
                self.assertEqual(empty, switched)
                # 次の幅のために空へ戻す
                self.stats.reset()
                self.history.clear()
                self.usage = ApiUsageStore(self.dir / f"api-usage-{width}.json")
                self.ctx.api_usage = self.usage

    def test_app_usage(self):
        """アプリ別は文字数の多い順に 5 件まで。空のときは案内を出す。"""
        page = self._page()
        page.refresh()
        self.assertEqual(page.apps_stack.currentIndex(), 1)
        for index, chars in enumerate((10, 300, 50, 70, 20, 5)):
            self.stats.record_session(chars, 1, f"app{index}", f"アプリ{index}", "")
        page.refresh()
        self.assertEqual(page.apps_stack.currentIndex(), 0)
        self.assertEqual([row.name() for row in page.app_rows], ["アプリ1", "アプリ3", "アプリ2", "アプリ4", "アプリ0"])

    def test_history_limit_and_copy(self):
        """履歴は 8 件まで。押すと全文がコピーされ、その行に「コピーしました」が出る。"""
        for index in range(10):
            self.history.add(f"履歴 {index}")
        page = self._page()
        page.refresh()
        self.assertEqual(page.history_rows[0].body.text(), "履歴 9")
        self.assertEqual(page.history_rows[7].body.text(), "履歴 2")
        page._copy_history(1)
        self.assertEqual(QApplication.clipboard().text(), "履歴 8")
        self.assertEqual(page.history_rows[1].copied.text(), "コピーしました")
        self.assertEqual(page.history_rows[0].copied.text(), "")
        page._clear_copied(page._copy_generation)
        self.assertEqual(page.history_rows[1].copied.text(), "")

    def test_navigation(self):
        """「API キーを設定」は api_keys へ、「もう一度見る」はセットアップガイドを開く。"""
        page = self._page()
        page.refresh()
        keys, guides = [], []
        page.navigate.connect(keys.append)
        self.ctx.open_onboarding_requested.connect(lambda: guides.append(True))
        page.api_key_button.click()
        page.guide_button.click()
        self.assertEqual(keys, ["api_keys"])
        self.assertEqual(guides, [True])

    def test_mic_test_starts_and_stops_on_hide(self):
        """マイクテストは押すと始まり、ページが隠れたら必ず止まる。"""
        monitor = _FakeMonitor()
        page = self._page()
        with mock.patch.object(page, "_make_monitor", return_value=monitor):
            page.toggle_mic_test()
        self.assertTrue(page._mic_timer.isActive())
        self.assertLessEqual(page._mic_timer.interval(), 33)
        self.assertEqual(monitor.started_with, "default")
        self.assertEqual(page.mic_button.text(), "停止")
        width = page.mic_button.width()
        page._poll_mic()
        self.assertAlmostEqual(page.mic_meter._fraction, 0.5)
        page.hide()
        self.assertFalse(page._mic_timer.isActive())
        self.assertFalse(monitor.running)
        self.assertEqual(page.mic_button.text(), "テスト")
        self.assertEqual(page.mic_button.width(), width)
        self.assertEqual(page.mic_meter._fraction, 0.0)

    def test_mic_test_failure_is_reported(self):
        """マイクを開けないときは黙らずに知らせ、タイマーは回さない。"""
        page = self._page()
        with mock.patch.object(page, "_make_monitor", return_value=_FakeMonitor(ok=False)):
            page.toggle_mic_test()
        self.assertFalse(page._mic_timer.isActive())
        self.assertEqual(page.mic_caption.text(), "マイクを開けませんでした。")

    def test_own_api_usage_when_missing(self):
        """料金の記録が渡されていなければ、自分で 1 つ作って読む。"""
        self.ctx.api_usage = None
        own = ApiUsageStore(self.dir / "own.json")
        with mock.patch("src.core.api_usage.ApiUsageStore", return_value=own) as factory:
            page = self._page()
            page.refresh()
            page.refresh()
        factory.assert_called_once_with()
        self.assertIs(page._api_usage(), own)

    def test_none_stores(self):
        """統計・履歴が無くても落ちずに空の表示になる。"""
        self.ctx.stats = None
        self.ctx.history = None
        page = self._page()
        page.refresh()
        self.assertEqual(page.subtitle.text(), "今日はまだ入力していません")
        self.assertEqual(page.history_stack.currentIndex(), 1)


if __name__ == "__main__":
    unittest.main()
