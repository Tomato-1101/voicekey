"""フルスクリーン判定（src/core/fullscreen.py）と HUD の待機ピル退避のテスト。

検証する性質:
1. detector 注入: 与えた判定関数の真偽をそのまま返す。例外は False に握りつぶす。
2. 非対応プラットフォーム（macOS 等）は既定判定が None で常に False。
3. HUD 待機ピル退避: always_visible + 待機中にフルスクリーンなら隠し、解除で出し直す。
4. 録音/変換/通知はフルスクリーンでも退避しない（監視タイマーも止まる）。

win32 API 呼び出し自体（GetForegroundWindow 等）はモック境界の外なのでここでは検証しない。
Qt はオフスクリーンで動かし、実画面・実デバイスには触れない。
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import unittest

from PySide6.QtWidgets import QApplication

from src.core import fullscreen
from src.ui.hud import Hud

_app = QApplication.instance() or QApplication([])


class ForegroundIsFullscreenTests(unittest.TestCase):
    """foreground_is_fullscreen の detector 注入・例外処理。"""

    def test_injected_detector_true(self):
        self.assertTrue(fullscreen.foreground_is_fullscreen(detector=lambda: True))

    def test_injected_detector_false(self):
        self.assertFalse(fullscreen.foreground_is_fullscreen(detector=lambda: False))

    def test_detector_exception_is_false(self):
        """判定関数が例外を投げても安全側（False）へ倒す。"""
        def boom() -> bool:
            raise RuntimeError("boom")

        self.assertFalse(fullscreen.foreground_is_fullscreen(detector=boom))

    def test_non_truthy_coerced_to_bool(self):
        """判定関数の返り値は bool に正規化される。"""
        self.assertTrue(fullscreen.foreground_is_fullscreen(detector=lambda: 1))
        self.assertFalse(fullscreen.foreground_is_fullscreen(detector=lambda: 0))

    def test_default_detector_none_on_non_windows(self):
        """macOS 等では既定判定関数が無く、常に False（非対応プラットフォーム）。"""
        # このテストは macOS/Linux 上で回る前提（CI/開発機）。Windows では実判定になる。
        if os.sys.platform.startswith("win"):
            self.skipTest("Windows では実プラットフォーム判定になるため対象外")
        self.assertIsNone(fullscreen._platform_detector())
        self.assertFalse(fullscreen.foreground_is_fullscreen())


class HudFullscreenIdleTests(unittest.TestCase):
    """HUD の待機ピルがフルスクリーン時に退避する挙動。"""

    def _make_hud(self, fullscreen_now: bool) -> Hud:
        hud = Hud(enabled=True)
        hud.always_visible = True
        hud._fullscreen_probe = lambda: fullscreen_now
        return hud

    def test_idle_hidden_when_fullscreen(self):
        """フルスクリーン中は待機中でも待機ピルを表示しない（モードは idle のまま）。"""
        hud = self._make_hud(fullscreen_now=True)
        hud.set_state("idle")
        self.assertFalse(hud.isVisible())
        self.assertEqual(hud._mode, "idle")  # 隠すがモードは待機のまま
        self.assertTrue(hud._fs_timer.isActive())  # 解除待ちの監視は動く
        hud.deleteLater()

    def test_idle_shown_when_not_fullscreen(self):
        """フルスクリーンでなければ待機ピルを表示する。"""
        hud = self._make_hud(fullscreen_now=False)
        hud.set_state("idle")
        self.assertTrue(hud.isVisible())
        self.assertEqual(hud._mode, "idle")
        hud.deleteLater()

    def test_tick_hides_and_shows_on_transition(self):
        """フルスクリーン化/解除にポーリングで追従する。"""
        state = {"fs": False}
        hud = Hud(enabled=True)
        hud.always_visible = True
        hud._fullscreen_probe = lambda: state["fs"]
        hud.set_state("idle")
        self.assertTrue(hud.isVisible())
        # フルスクリーンへ移行 → 次の tick で隠れる
        state["fs"] = True
        hud._tick_fullscreen()
        self.assertFalse(hud.isVisible())
        # 解除 → 次の tick で出し直す
        state["fs"] = False
        hud._tick_fullscreen()
        self.assertTrue(hud.isVisible())
        hud.deleteLater()

    def test_recording_ignores_fullscreen(self):
        """録音中はフルスクリーンでも表示し、監視タイマーは止まる。"""
        hud = self._make_hud(fullscreen_now=True)
        hud.set_state("recording")
        self.assertTrue(hud.isVisible())
        self.assertEqual(hud._mode, "recording")
        self.assertFalse(hud._fs_timer.isActive())
        hud.deleteLater()

    def test_transcribing_ignores_fullscreen(self):
        """変換中はフルスクリーンでも表示する。"""
        hud = self._make_hud(fullscreen_now=True)
        hud.set_state("transcribing")
        self.assertTrue(hud.isVisible())
        self.assertEqual(hud._mode, "transcribing")
        self.assertFalse(hud._fs_timer.isActive())
        hud.deleteLater()

    def test_notice_ignores_fullscreen(self):
        """通知はフルスクリーンでも表示する。"""
        hud = self._make_hud(fullscreen_now=True)
        hud.show_notice("テスト通知")
        self.assertTrue(hud.isVisible())
        self.assertEqual(hud._mode, "notice")
        self.assertFalse(hud._fs_timer.isActive())
        hud.deleteLater()

    def test_timer_stops_when_always_visible_off(self):
        """always_visible OFF の待機では監視タイマーは動かない（隠すだけ）。"""
        hud = Hud(enabled=True)
        hud.always_visible = False
        hud._fullscreen_probe = lambda: True
        hud.set_state("idle")
        self.assertEqual(hud._mode, "hidden")
        self.assertFalse(hud._fs_timer.isActive())
        hud.deleteLater()


if __name__ == "__main__":
    unittest.main()
