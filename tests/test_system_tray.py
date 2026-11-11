"""
システムトレイのメニューのテスト

並び・文言が Mac 版のメニューバーと同じであること、状態と今日の API 代の行が
正しく切り替わることを確かめる（実際の通知領域には依存しない）。
"""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from src.config.types import AppState
from src.ui.system_tray import SystemTray


class _FakeUsage:
    """今日の集計だけ返す偽の料金記録。"""

    def __init__(self, usd: float, has_unpriced: bool = False) -> None:
        self._summary = {"usd": usd, "has_unpriced": has_unpriced, "requests": 1}
        # 呼ばれたときの数え方の記録
        self.modes = []

    def today_summary(self, *args, **_kwargs) -> dict:
        self.modes.append(args[0] if args else None)
        return self._summary


class TestSystemTrayMenu(unittest.TestCase):
    """メニューの並び・状態行・料金の行。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.tray = SystemTray(platform_adapter=object())
        self.addCleanup(self.tray.hide)

    def _visible_titles(self) -> list:
        """見えている項目の文言（区切り線は "-"）。"""
        return ["-" if a.isSeparator() else a.text() for a in self.tray._menu.actions() if a.isVisible()]

    def test_default_order_matches_mac(self) -> None:
        """更新も料金も無いときの並び。"""
        self.assertEqual(
            self._visible_titles(),
            ["待機中", "-", "ホーム", "設定…", "フィードバックを送る…", "-",
             "強制リセット（フリーズ復帰）", "voicekey を終了"],
        )

    def test_meet_submenu(self) -> None:
        """議事録ボットを渡すと「設定…」の次にサブメニューが出て、中身は開くたびに Mac と同じ並びで作る。"""
        from unittest import mock
        from PySide6.QtCore import QObject, Signal

        class _Bot(QObject):
            state_changed = Signal(str, str)
            state = "idle"

            def is_active(self):
                return False

            def stop(self):
                pass

            def open_login(self):
                return None

        bot = _Bot()
        self.tray.set_meetbot(bot)
        titles = self._visible_titles()
        self.assertEqual(titles[titles.index("設定…") + 1], "Meet 議事録ボット")
        with mock.patch("src.meetbot.chrome.find_chrome", return_value="/fake/chrome"), \
                mock.patch("src.meetbot.transcript.list_transcripts", return_value=[]):
            self.tray._rebuild_meet_menu()
        sub = ["-" if a.isSeparator() else a.text() for a in self.tray._meet_menu.actions()]
        self.assertEqual(sub, [
            "参加していません", "文字起こし: Meet の字幕", "-", "会議に参加…", "-",
            "ボット用ブラウザで Google にログイン…", "-",
            "最新の議事録を開く", "議事録フォルダを開く", "議事録・会議の設定…",
        ])
        bot.state_changed.emit("recording", "")
        self.assertEqual(self.tray._meet_menu.title(), "Meet 議事録ボット（参加中）")

    def test_translate_toggle(self) -> None:
        """読み書き先を渡すと「翻訳して入力（常時）」が出て、開くたびに値を読み直し、押すと保存する。"""
        store = {"on": False}
        self.tray.set_translate_toggle(lambda: store["on"], lambda v: store.update(on=v))
        self.assertIn("翻訳して入力（常時）", self._visible_titles())
        store["on"] = True
        self.tray._menu.aboutToShow.emit()
        self.assertTrue(self.tray._translate_action.isChecked())
        self.tray._translate_action.trigger()
        self.assertFalse(store["on"])

    def test_state_row_follows_status_and_is_disabled(self) -> None:
        """状態行は状態に追従し、押せない。"""
        self.tray.set_status(AppState.RECORDING_AUTO_ENTER)
        self.assertEqual(self.tray._state_action.text(), "録音中（自動送信）")
        self.tray.set_status("transcribing")
        self.assertEqual(self.tray._state_action.text(), "変換中…")
        self.assertFalse(self.tray._state_action.isEnabled())

    def test_cost_row_shows_dollars_only(self) -> None:
        """料金の行はドルだけで出し、開くたびに数え直す。"""
        self.tray.set_api_usage(_FakeUsage(0.1234))
        self.assertEqual(self._visible_titles()[1], "今日の API 代 $0.12")
        self.tray._api_usage = _FakeUsage(0.004, has_unpriced=True)
        self.tray._menu.aboutToShow.emit()
        self.assertEqual(self.tray._cost_action.text(), "今日の API 代 $0.0040＋単価未確認分")
        self.assertFalse(self.tray._cost_action.isEnabled())

    def test_cost_row_follows_selected_mode(self) -> None:
        """料金の行は、ダッシュボードで選んだ数え方で数える（開くたびに読み直す）。"""
        usage = _FakeUsage(0.5)
        mode = {"value": "list"}
        self.tray.set_api_usage(usage, mode=lambda: mode["value"])
        mode["value"] = "paid"
        self.tray._menu.aboutToShow.emit()
        mode["value"] = "壊れた値"
        self.tray._menu.aboutToShow.emit()
        self.assertEqual(usage.modes, ["list", "paid", "list"])

    def test_update_row_appears_on_top(self) -> None:
        """更新があるときだけ、いちばん上に更新の行と区切り線が出る。"""
        self.tray.show_update_available("v2.4.0")
        self.assertEqual(self._visible_titles()[:3], ["v2.4.0 に更新して再起動", "-", "待機中"])

    def test_home_and_settings_emit_separate_signals(self) -> None:
        """ホームと設定…は別の要求を出す。"""
        got = []
        self.tray.open_home.connect(lambda: got.append("home"))
        self.tray.open_settings.connect(lambda: got.append("settings"))
        actions = {a.text(): a for a in self.tray._menu.actions()}
        actions["ホーム"].trigger()
        actions["設定…"].trigger()
        self.assertEqual(got, ["home", "settings"])


if __name__ == "__main__":
    unittest.main()
