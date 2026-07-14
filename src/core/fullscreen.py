"""
前面ウィンドウのフルスクリーン判定モジュール（Windows 固有・待機ピルの退避に使う）

Mac 版は NSWindow.collectionBehavior で OS の Space 管理へ委譲し、フルスクリーン Space
では待機ピルが自然に隠れる（録音/変換/通知は全 Space に表示）。Windows には「現在の
Space だけに出す」相当の OS 機構が無いため、前面ウィンドウがモニタ全面を覆っているかを
自前で判定し、待機（idle）ピルだけを退避させる（録音/変換中/通知は常に表示する）。

設計（テスト容易性）:
- 実際の win32 API 呼び出し（GetForegroundWindow など）は `_win32_foreground_fullscreen`
  に隔離し、`foreground_is_fullscreen()` は判定関数を差し替え可能にする（detector 注入）。
  macOS 上ではプラットフォーム分岐で常に False を返す（テストは detector を注入して検証する）。
- 例外は握りつぶして False を返す（判定できないときは「フルスクリーンでない」＝ピルを出す
  安全側に倒す）。
"""

import sys
from typing import Callable, Optional


def foreground_is_fullscreen(detector: Optional[Callable[[], bool]] = None) -> bool:
    """前面ウィンドウがモニタ全面を覆っているか（＝フルスクリーン）を返す。

    Args:
        detector: 判定関数の差し替え（テスト用）。None なら実行中プラットフォームの
                  既定判定を使う（Windows 以外は常に False）。

    Returns:
        フルスクリーンなら True。判定できない・非対応プラットフォームなら False。
    """
    fn = detector or _platform_detector()
    if fn is None:
        return False
    try:
        return bool(fn())
    except Exception:
        # 判定に失敗したら安全側（フルスクリーンでない＝ピルを出す）へ倒す
        return False


def _platform_detector() -> Optional[Callable[[], bool]]:
    """実行中プラットフォームの既定判定関数を返す（非対応なら None）。"""
    if sys.platform.startswith("win"):
        return _win32_foreground_fullscreen
    return None


# デスクトップ/シェル系ウィンドウのクラス名（これらが前面でも「フルスクリーン」とはみなさない）。
# 空のデスクトップ（Progman/WorkerW）やタスクバー（Shell_TrayWnd）を除外する。
_EXCLUDED_WINDOW_CLASSES = frozenset(
    {"Progman", "WorkerW", "Shell_TrayWnd", "Windows.UI.Core.CoreWindow"}
)


def _win32_foreground_fullscreen() -> bool:
    """win32 で前面ウィンドウがモニタ全面を覆っているか判定する（Windows 実機専用）。

    GetForegroundWindow → GetWindowRect と、そのウィンドウが載るモニタの矩形
    （MonitorFromWindow + GetMonitorInfo）を比較し、ウィンドウ矩形がモニタ全体を
    覆っていればフルスクリーンとみなす。デスクトップ/タスクバーは除外する。
    """
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32

    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", wintypes.LONG),
            ("top", wintypes.LONG),
            ("right", wintypes.LONG),
            ("bottom", wintypes.LONG),
        ]

    class MONITORINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("rcMonitor", RECT),
            ("rcWork", RECT),
            ("dwFlags", wintypes.DWORD),
        ]

    MONITOR_DEFAULTTONEAREST = 2

    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return False

    # デスクトップ/シェル系ウィンドウは除外する
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    if buf.value in _EXCLUDED_WINDOW_CLASSES:
        return False

    rect = RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return False

    monitor = user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
    if not monitor:
        return False

    info = MONITORINFO()
    info.cbSize = ctypes.sizeof(MONITORINFO)
    if not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
        return False

    m = info.rcMonitor
    # ウィンドウがモニタ全面を覆っている（枠がモニタ端に達している）か
    return (
        rect.left <= m.left
        and rect.top <= m.top
        and rect.right >= m.right
        and rect.bottom >= m.bottom
    )
