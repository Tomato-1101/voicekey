"""録音中 HUD（src/ui/hud.py）のオフラインテスト。

検証する性質:
1. 三段階サイズ: 待機ピル < 変換中 < 録音（面積・高さの順序）。
2. 待機ピル（常時表示）の可視条件: always_visible の ON/OFF で idle が表示/非表示になる。
3. ハンズフリー配線: set_hands_free で内部フラグが立ち、録音描画が例外なく走る。
4. 設定の既定値: hud_always_visible は既定 False で、_force_always_on の矯正対象ではない。

Qt はオフスクリーンで動かし、実画面・実デバイスには触れない。イベントループは回さないため、
サイズは表示中の補間値ではなく純粋関数 _target_size() を検証する（アニメの途中値に依存しない）。
"""

import os

# PySide6 の import より前に必ずオフスクリーン指定する
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import unittest

from PySide6.QtCore import QPoint
from PySide6.QtGui import QColor, QPainter, QPixmap
from PySide6.QtWidgets import QApplication

from src.ui.hud import Hud
from src.config.constants import DEFAULT_CONFIG
from src.config.config_manager import ConfigManager

# QApplication はプロセスに 1 つ。各テストで使い回す
_app = QApplication.instance() or QApplication([])


class HudSizeTests(unittest.TestCase):
    """三段階サイズと待機ピルの可視条件。"""

    def _target(self, hud: Hud, mode: str) -> tuple:
        hud._mode = mode
        return hud._target_size()

    def test_three_stage_size_ordering(self):
        """待機ピル < 変換中 < 録音（面積が単調増加・高さも単調増加）。"""
        hud = Hud(enabled=True)
        idle_w, idle_h = self._target(hud, "idle")
        trans_w, trans_h = self._target(hud, "transcribing")
        rec_w, rec_h = self._target(hud, "recording")

        # 高さは定数で厳密に順序が決まる（14 < 26 < 36）
        self.assertLess(idle_h, trans_h)
        self.assertLess(trans_h, rec_h)
        # 面積（見た目の大きさ）も待機 < 変換中 < 録音
        self.assertLess(idle_w * idle_h, trans_w * trans_h)
        self.assertLess(trans_w * trans_h, rec_w * rec_h)
        hud.deleteLater()

    def test_idle_pill_visibility_depends_on_always_visible(self):
        """always_visible が True のときだけ idle で待機ピルを表示する。"""
        # OFF: idle では隠れる
        hud_off = Hud(enabled=True)
        hud_off.always_visible = False
        hud_off.set_state("idle")
        self.assertEqual(hud_off._mode, "hidden")
        self.assertFalse(hud_off.isVisible())
        hud_off.deleteLater()

        # ON: idle で待機ピルを表示する
        hud_on = Hud(enabled=True)
        hud_on.always_visible = True
        hud_on.set_state("idle")
        self.assertEqual(hud_on._mode, "idle")
        self.assertTrue(hud_on.isVisible())
        hud_on._hide()
        hud_on.deleteLater()

    def test_disabled_hud_never_shows(self):
        """enabled=False の HUD は常時表示 ON でも出さない。"""
        hud = Hud(enabled=False)
        hud.always_visible = True
        hud.set_state("idle")
        self.assertFalse(hud.isVisible())
        hud.deleteLater()


class HudHandsFreeTests(unittest.TestCase):
    """ハンズフリー配線と全状態の描画が例外なく走ること。"""

    def test_set_hands_free_flag(self):
        hud = Hud(enabled=True)
        hud.set_state("recording")
        self.assertFalse(hud._hands_free)
        hud.set_hands_free(True)
        self.assertTrue(hud._hands_free)
        hud._hide()
        hud.deleteLater()

    def test_render_all_states_without_error(self):
        """待機/録音/ハンズフリー録音/自動Enter/変換中/通知を描画してもクラッシュしない。"""
        cases = [
            ("idle", None, False),
            ("recording", None, False),
            ("recording", None, True),
            ("recording_auto_enter", None, False),
            ("transcribing", None, False),
            (None, "音声が検出されませんでした", False),
        ]
        for state, notice, hands_free in cases:
            hud = Hud(enabled=True)
            hud.always_visible = True
            if state:
                if hands_free:
                    hud.set_hands_free(True)
                hud.set_state(state)
                if state.startswith("recording"):
                    for i in range(hud.BAR_COUNT):
                        hud.push_level(0.2 + 0.6 * (i % 5) / 5)
            else:
                hud.show_notice(notice)
            canvas = QPixmap(hud.size())
            canvas.fill(QColor("#6E6E73"))
            painter = QPainter(canvas)
            hud.render(painter, QPoint(0, 0))  # 例外が出れば失敗
            painter.end()
            hud._notice_timer.stop()
            hud._size_anim.stop()
            hud._blink.stop()
            hud.deleteLater()


class HudConfigDefaultTests(unittest.TestCase):
    """設定の既定値と常時 ON 矯正の対象外であること。"""

    def test_default_is_false(self):
        self.assertIn("hud_always_visible", DEFAULT_CONFIG)
        self.assertFalse(DEFAULT_CONFIG["hud_always_visible"])

    def test_not_forced_by_always_on(self):
        """_force_always_on は hud_always_visible を矯正しない（hud_enabled は矯正する）。"""
        cfg = {"hud_always_visible": False, "hud_enabled": False}
        ConfigManager._force_always_on(cfg)
        self.assertFalse(cfg["hud_always_visible"])  # ユーザーの OFF が保たれる
        self.assertTrue(cfg["hud_enabled"])          # こちらは常時 ON に矯正される

        cfg2 = {"hud_always_visible": True}
        ConfigManager._force_always_on(cfg2)
        self.assertTrue(cfg2["hud_always_visible"])  # ON も保たれる


if __name__ == "__main__":
    unittest.main()
