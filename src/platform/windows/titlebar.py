"""
タイトルバーの明暗

Windows のタイトルバー（閉じるボタンのある帯）は、何もしないとアプリがダークでも白いままになる。
DWM に「この窓はダーク」と伝えて、帯の色を外観に合わせる。
"""

import ctypes
import logging
import sys

logger = logging.getLogger(__name__)

# DwmSetWindowAttribute の属性番号。20 は Windows 10 2004 以降、19 はそれより前の版での番号
DWMWA_USE_IMMERSIVE_DARK_MODE = 20
_DWMWA_USE_IMMERSIVE_DARK_MODE_OLD = 19


def set_titlebar_dark(widget, dark: bool) -> bool:
    """
    窓のタイトルバーをダーク／ライトにする。

    Args:
        widget: 対象の窓（QWidget。`winId()` で HWND を取り出す）
        dark: ダークにするなら True

    Returns:
        反映できたら True。Windows 以外・古い Windows・失敗時は False（白い帯のままになるだけ）
    """
    if sys.platform != "win32":
        return False
    try:
        hwnd = int(widget.winId())
        value = ctypes.c_int(1 if dark else 0)
        dwmapi = ctypes.windll.dwmapi
        for attribute in (DWMWA_USE_IMMERSIVE_DARK_MODE, _DWMWA_USE_IMMERSIVE_DARK_MODE_OLD):
            result = dwmapi.DwmSetWindowAttribute(
                ctypes.c_void_p(hwnd), ctypes.c_uint(attribute), ctypes.byref(value), ctypes.sizeof(value)
            )
            if result == 0:
                return True
    except Exception:
        # 帯の色だけの話なので、失敗しても動作は続ける
        logger.debug("タイトルバーの明暗を変えられませんでした", exc_info=True)
    return False
