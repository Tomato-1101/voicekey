"""Windows アクリル/ブラーヘルパ（src/platform/windows/acrylic.py）のテスト。

検証する性質:
1. accent state 選択: 未対応ビルド/None は None、Win10 1507..1803未満 は BLUR、1803+ は ACRYLIC。
2. blur_supported はビルド番号に追従する。
3. apply_blur は未対応（非 Windows・古いビルド）で "none" を返し applier を呼ばない。
4. apply_blur は対応ビルドで applier を hwnd/state 付きで呼び、"acrylic"/"blur" を返す。
5. applier が例外を投げたら "none"（疑似ガラスへフォールバック）。

実際の SetWindowCompositionAttribute 呼び出しはモック境界の外。ビルド判定は
_windows_build を差し替えて検証する（このテストは macOS/Linux 上でも回る）。
"""

import unittest

from src.platform.windows import acrylic
from src.platform.windows.acrylic import (
    ACCENT_ENABLE_ACRYLICBLURBEHIND,
    ACCENT_ENABLE_BLURBEHIND,
    _accent_state_for_build,
)


class _FakeWidget:
    """winId() だけ持つダミーウィジェット。"""

    def __init__(self, hwnd: int = 12345) -> None:
        self._hwnd = hwnd

    def winId(self) -> int:
        return self._hwnd


class AccentStateTests(unittest.TestCase):
    def test_none_or_too_old_is_unsupported(self):
        self.assertIsNone(_accent_state_for_build(None))
        self.assertIsNone(_accent_state_for_build(10239))  # Win10 1507 未満

    def test_blur_range(self):
        """Windows 10 1507..1803未満 は旧ブラー。"""
        self.assertEqual(_accent_state_for_build(10240), ACCENT_ENABLE_BLURBEHIND)
        self.assertEqual(_accent_state_for_build(17133), ACCENT_ENABLE_BLURBEHIND)

    def test_acrylic_range(self):
        """Windows 10 1803+ はアクリル。"""
        self.assertEqual(_accent_state_for_build(17134), ACCENT_ENABLE_ACRYLICBLURBEHIND)
        self.assertEqual(_accent_state_for_build(19045), ACCENT_ENABLE_ACRYLICBLURBEHIND)
        self.assertEqual(_accent_state_for_build(22631), ACCENT_ENABLE_ACRYLICBLURBEHIND)


class BlurSupportedTests(unittest.TestCase):
    def setUp(self):
        self._orig = acrylic._windows_build

    def tearDown(self):
        acrylic._windows_build = self._orig

    def test_supported_on_win11(self):
        acrylic._windows_build = lambda: 22631
        self.assertTrue(acrylic.blur_supported())

    def test_unsupported_on_non_windows(self):
        acrylic._windows_build = lambda: None
        self.assertFalse(acrylic.blur_supported())


class ApplyBlurTests(unittest.TestCase):
    def setUp(self):
        self._orig = acrylic._windows_build

    def tearDown(self):
        acrylic._windows_build = self._orig

    def test_none_when_unsupported_and_applier_not_called(self):
        """未対応（非 Windows 等）は "none" を返し applier を呼ばない。"""
        acrylic._windows_build = lambda: None
        calls = []
        result = acrylic.apply_blur(
            _FakeWidget(), dark=True, applier=lambda h, s, g: calls.append((h, s, g))
        )
        self.assertEqual(result, "none")
        self.assertEqual(calls, [])

    def test_acrylic_on_win11_calls_applier(self):
        """Win11 ではアクリルを適用し applier に hwnd/state を渡す。"""
        acrylic._windows_build = lambda: 22631
        calls = []
        result = acrylic.apply_blur(
            _FakeWidget(hwnd=777), dark=True, applier=lambda h, s, g: calls.append((h, s, g))
        )
        self.assertEqual(result, "acrylic")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], 777)
        self.assertEqual(calls[0][1], ACCENT_ENABLE_ACRYLICBLURBEHIND)

    def test_blur_on_old_win10(self):
        """Win10 1507..1803未満 は旧ブラーを適用する。"""
        acrylic._windows_build = lambda: 16299
        result = acrylic.apply_blur(_FakeWidget(), dark=False, applier=lambda h, s, g: None)
        self.assertEqual(result, "blur")

    def test_applier_exception_falls_back_to_none(self):
        """applier が例外を投げたら "none"（疑似ガラスへフォールバック）。"""
        acrylic._windows_build = lambda: 22631

        def boom(h, s, g):
            raise RuntimeError("no dwm")

        self.assertEqual(acrylic.apply_blur(_FakeWidget(), applier=boom), "none")

    def test_custom_tint_passed_through(self):
        """指定した gradient_argb がそのまま applier へ渡る。"""
        acrylic._windows_build = lambda: 22631
        seen = {}
        acrylic.apply_blur(
            _FakeWidget(), gradient_argb=0x12345678,
            applier=lambda h, s, g: seen.update(g=g),
        )
        self.assertEqual(seen["g"], 0x12345678)


if __name__ == "__main__":
    unittest.main()
