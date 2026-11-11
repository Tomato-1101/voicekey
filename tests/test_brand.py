"""
ロゴ（アプリアイコン・トレイのアイコン）のテスト

Windows 版のロゴが Mac 版と同じ絵のままであることを確かめる。
"""

import unittest
from pathlib import Path

from PySide6.QtWidgets import QApplication

from src.config.types import AppState
from src.ui import brand

_app = QApplication.instance() or QApplication([])
_ROOT = Path(__file__).resolve().parent.parent


class AppIconTests(unittest.TestCase):
    """アプリアイコンの画像。"""

    def test_images_are_the_same_files_as_mac(self) -> None:
        """Windows 側の画像は Mac 版の画像の写し（1 バイトも違わない）。片方だけ差し替えたら落ちる。"""
        for name in ("AppIconLight.png", "AppIconDark.png"):
            mac = (_ROOT / "macos" / "Resources" / name).read_bytes()
            windows = (_ROOT / "src" / "ui" / "assets" / name).read_bytes()
            self.assertEqual(mac, windows, name)

    def test_pixmap_follows_size_and_scale(self) -> None:
        """頼んだ大きさ × 画面の倍率のピクセル数で返る（高倍率の画面でぼけない）。"""
        for dark in (False, True):
            pixmap = brand.app_icon_pixmap(dark, 30, 2.0)
            self.assertFalse(pixmap.isNull())
            self.assertEqual((pixmap.width(), pixmap.height()), (60, 60))
            self.assertEqual(pixmap.devicePixelRatio(), 2.0)

    def test_light_and_dark_differ(self) -> None:
        """ライト用（ボーン地）とダーク用（カーボン地）は別の絵。"""
        light = brand.app_icon_pixmap(False, 32).toImage()
        dark = brand.app_icon_pixmap(True, 32).toImage()
        self.assertNotEqual(light.pixelColor(16, 20), dark.pixelColor(16, 20))

    def test_window_icon_has_small_and_large_sizes(self) -> None:
        """窓・タスクバー用のアイコンは小さい絵と大きい絵の両方を持つ。"""
        sizes = {size.width() for size in brand.app_icon().availableSizes()}
        self.assertTrue({16, 32, 256} <= sizes)


class StatusIconTests(unittest.TestCase):
    """トレイのアイコン。"""

    @staticmethod
    def _image(state: AppState, light_taskbar: bool = False):
        return brand.status_icon(state, light_taskbar).pixmap(64, 64).toImage()

    def test_every_state_has_its_own_picture(self) -> None:
        """待機・録音中・録音中（自動 Enter）・変換中は見分けが付く。"""
        images = [self._image(state) for state in AppState]
        for index, image in enumerate(images):
            self.assertFalse(image.isNull())
            for other in images[index + 1:]:
                self.assertNotEqual(image, other)

    def test_only_the_light_is_colored(self) -> None:
        """待機は色を使わない（枠・刻印・灯りの輪とも同じ色）。録音中は灯りの点だけが灯り色。"""
        idle = self._image(AppState.IDLE)
        recording = self._image(AppState.RECORDING)
        # 灯りの中心（18 マスの 11.7, 6.3 → 64px では 41.6, 22.4）
        center = recording.pixelColor(42, 22)
        self.assertGreater(center.red(), 200)
        self.assertLess(center.blue(), 80)
        self.assertEqual(idle.pixelColor(42, 22).alpha(), 0)  # 待機は輪なので中心は抜けている

    def test_frame_color_follows_taskbar(self) -> None:
        """枠は暗いタスクバーで白、明るいタスクバーで黒。"""
        # 枠の上辺（y = 1.875 → 64px では 6.7）
        on_dark = self._image(AppState.IDLE, False).pixelColor(32, 7)
        on_light = self._image(AppState.IDLE, True).pixelColor(32, 7)
        self.assertGreater(on_dark.red(), 200)
        self.assertLess(on_light.red(), 60)


if __name__ == "__main__":
    unittest.main()
