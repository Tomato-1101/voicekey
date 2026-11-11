"""
前面アプリの情報

録音を始めた時点で前面にあるアプリ（＝文字を貼り付ける先）の実行ファイルとアイコンを調べる。
HUD に「どのアプリへ入るか」をアイコンで見せるために使う。
"""

import ctypes
import logging
import sys
from ctypes import wintypes
from typing import Dict, Optional

from PySide6.QtCore import QFileInfo
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QFileIconProvider

logger = logging.getLogger(__name__)

# 実行ファイルのパスを読むだけの最小の権限（管理者で動くアプリ相手でも通りやすい）
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

# 同じアプリのアイコンを録音のたびに取り直さない（シェルへの問い合わせは数 ms〜数十 ms かかる）
_icon_cache: Dict[str, Optional[QPixmap]] = {}


def foreground_exe_path() -> str:
    """
    前面ウィンドウを持つプロセスの実行ファイルのパスを返す。

    Returns:
        実行ファイルのフルパス。Windows 以外・取れなかったときは空文字
    """
    if sys.platform != "win32":
        return ""
    try:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        user32.GetForegroundWindow.restype = wintypes.HWND
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)
        ]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return ""
        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return ""
        handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not handle:
            return ""
        try:
            size = wintypes.DWORD(32768)
            buffer = ctypes.create_unicode_buffer(size.value)
            if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                return ""
            return buffer.value
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        # アイコンが出ないだけなので、失敗しても録音は続ける
        logger.debug("前面アプリの実行ファイルを調べられませんでした", exc_info=True)
        return ""


def icon_for_exe(path: str, size: int = 32) -> Optional[QPixmap]:
    """
    実行ファイルのアイコンを返す。

    Args:
        path: 実行ファイルのフルパス
        size: 欲しい大きさ（ピクセル。高倍率の画面でもぼけないよう、表示より大きめを頼む）

    Returns:
        アイコン。パスが空・アイコンが取れないときは None
    """
    if not path:
        return None
    key = f"{path}|{size}"
    if key in _icon_cache:
        return _icon_cache[key]
    pixmap: Optional[QPixmap] = None
    try:
        icon = QFileIconProvider().icon(QFileInfo(path))
        candidate = icon.pixmap(size, size)
        if not candidate.isNull():
            pixmap = candidate
    except Exception:
        logger.debug("アプリのアイコンを取れませんでした", exc_info=True)
    _icon_cache[key] = pixmap
    return pixmap


def foreground_app_icon(size: int = 32) -> Optional[QPixmap]:
    """
    前面アプリのアイコンを返す（UI スレッドから呼ぶ。QPixmap を作るため）。

    Args:
        size: 欲しい大きさ（ピクセル）

    Returns:
        アイコン。取れないときは None
    """
    return icon_for_exe(foreground_exe_path(), size)
