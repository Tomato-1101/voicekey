"""Windows 標準の見た目（src/ui/theme.py と src/ui/components.py）のテスト。

1. 文字の大きさの読み替え（Mac の数値 → Windows の段）。
2. ライト／ダークの地色が Windows 11 の「設定」アプリと同じ。
3. アクセント色の切り替え（オレンジ／Windows の色）と changed の通知。
4. 保存値の読み方（accent_from）。
5. 状態で大きさが変わらない部品（トグルのオン／オフ、セグメントの選択）。
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import unittest
from unittest import mock

from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import QApplication

_app = QApplication.instance() or QApplication([])

from src.ui import components, theme  # noqa: E402
from src.ui.pages.general import accent_from  # noqa: E402


class FontSizeTest(unittest.TestCase):
    def test_mac_sizes_map_to_windows_ramp(self):
        # 本文 13 → 14、説明 11 → 12、見出し 17 → 20（Windows の Body / Caption / Subtitle）
        self.assertEqual(theme.px(13), 14)
        self.assertEqual(theme.px(11), 12)
        self.assertEqual(theme.px(17), 20)
        self.assertEqual(theme.px(28), 28)

    def test_bold_is_clamped_to_semibold(self):
        self.assertEqual(theme.font(13, QFont.Weight.Bold).weight(), QFont.Weight.DemiBold)
        self.assertEqual(theme.font(13).pixelSize(), 14)


class PaletteTest(unittest.TestCase):
    def test_window_colors_match_windows_settings(self):
        self.assertEqual(theme.build_palette(False, theme.ACCENT_BRAND).window.name(), "#f3f3f3")
        self.assertEqual(theme.build_palette(True, theme.ACCENT_BRAND).window.name(), "#202020")

    def test_brand_accent_is_orange(self):
        self.assertEqual(theme.build_palette(False, theme.ACCENT_BRAND).accent.name(), theme.SIGNAL.lower())

    def test_system_accent_uses_windows_shades(self):
        shades = theme._derive_shades(QColor("#0078D4"))
        light = theme.build_palette(False, theme.ACCENT_SYSTEM, shades)
        dark = theme.build_palette(True, theme.ACCENT_SYSTEM, shades)
        # Windows と同じく、ライトは濃い段（Dark1）、ダークは明るい段（Light2）を面に使う
        self.assertEqual(light.accent.name(), shades.dark1.name())
        self.assertEqual(dark.accent.name(), shades.light2.name())


class AccentModeTest(unittest.TestCase):
    def setUp(self):
        self.theme = theme.Theme.instance()
        self.addCleanup(self.theme.set_accent_mode, theme.ACCENT_BRAND)

    def test_switching_accent_emits_changed(self):
        calls = []
        self.theme.changed.connect(lambda: calls.append(1))
        with mock.patch.object(theme, "system_accent_shades", return_value=theme._derive_shades(QColor("#0078D4"))):
            self.theme.set_accent_mode(theme.ACCENT_SYSTEM)
        self.assertTrue(calls)
        self.assertEqual(self.theme.accent_mode, theme.ACCENT_SYSTEM)
        self.assertNotEqual(self.theme.palette.accent.name(), theme.SIGNAL.lower())

    def test_unknown_value_falls_back_to_brand(self):
        self.theme.set_accent_mode("purple")
        self.assertEqual(self.theme.accent_mode, theme.ACCENT_BRAND)

    def test_accent_from_reads_saved_value(self):
        self.assertEqual(accent_from(lambda k, d=None: {"accent_color": "system"}.get(k, d)), theme.ACCENT_SYSTEM)
        self.assertEqual(accent_from(lambda k, d=None: d), theme.ACCENT_BRAND)
        self.assertEqual(accent_from(lambda k, d=None: "x"), theme.ACCENT_BRAND)


class StableSizeTest(unittest.TestCase):
    def test_switch_size_does_not_change_when_toggled(self):
        switch = components.Switch(False)
        before = switch.size()
        switch.setChecked(True)
        self.assertEqual(switch.size(), before)

    def test_segmented_size_does_not_change_when_selected(self):
        seg = components.Segmented(["短", "とても長い選択肢"])
        before = seg.size()
        seg.set_current_index(1)
        self.assertEqual(seg.size(), before)

    def test_picker_and_buttons_are_32_tall(self):
        self.assertEqual(components.Picker().height(), 32)
        self.assertEqual(components.CapsuleButton("保存").sizeHint().height(), 32)


if __name__ == "__main__":
    unittest.main()
