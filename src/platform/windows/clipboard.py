"""
クリップボードへの「履歴・監視に残さないで」の印付き書き込み（Windows）

voicekey が貼り付けのために一時的に書く本文を、voicekey 自身のクリップボード履歴にも
Win+V の履歴にも他のクリップボード管理アプリにも残させないための書き込み口。
Win32 API を ctypes で直接呼ぶ。Windows 以外・失敗時は False を返し、呼び出し側は
従来の pyperclip.copy に戻る（例外は外に出さない）。
"""

import ctypes
import sys
import time

from ...utils.logger import get_logger

logger = get_logger(__name__)


def _win32():
    """user32 / kernel32 の関数を型付きで用意する。"""
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.CloseClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE
    user32.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]
    user32.RegisterClipboardFormatW.restype = wintypes.UINT
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
    ]
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.DestroyWindow.argtypes = [wintypes.HWND]
    user32.DestroyWindow.restype = wintypes.BOOL
    return user32, kernel32


# メッセージ専用ウィンドウの親に渡す特別な値（HWND_MESSAGE）
_HWND_MESSAGE = -3


def _create_owner_window(user32):
    """書き込み用の持ち主ウィンドウ（画面に出ないメッセージ専用ウィンドウ）を作る（作れなければ None）。

    NULL で開いて EmptyClipboard すると持ち主が NULL になり、SetClipboardData が失敗する
    （Microsoft の OpenClipboard の仕様。pyperclip が隠しウィンドウを作るのも同じ理由）。
    書き込みのたびに作って閉じたら壊す。使い回すと、メッセージを処理しないワーカースレッドの
    ウィンドウが持ち主のまま残り、他のアプリの EmptyClipboard が送る WM_DESTROYCLIPBOARD を
    受け取れず相手を待たせるため（pyperclip も同じく毎回作って壊す）。
    """
    return user32.CreateWindowExW(
        0, "STATIC", None, 0, 0, 0, 0, 0, _HWND_MESSAGE, None, None, None
    ) or None


def _open(user32, owner=None) -> bool:
    """クリップボードを開く（他のアプリが開いていることがあるので数回だけ待って再試行する）。

    Args:
        owner: 書き込む（EmptyClipboard → SetClipboardData）ときの持ち主ウィンドウ。
            読むだけなら None のままでよい
    """
    for _ in range(10):
        if user32.OpenClipboard(owner):
            return True
        time.sleep(0.01)
    return False


def _open_for_write(user32):
    """書き込み用にクリップボードを開き、持ち主ウィンドウを返す（開けなければ None）。

    呼び出し側は CloseClipboard の後に _close_for_write で閉じること。
    """
    owner = _create_owner_window(user32)
    if not owner:
        return None
    if not _open(user32, owner):
        user32.DestroyWindow(owner)
        return None
    return owner


def _close_for_write(user32, owner) -> None:
    """書き込み用に開いたクリップボードを閉じ、持ち主ウィンドウを壊す（書いた中身はそのまま残る）。"""
    user32.CloseClipboard()
    user32.DestroyWindow(owner)



def copy_text_excluded(text: str) -> bool:
    """テキストを「クリップボードの履歴・監視に残さないで」の印付きで書き込む（成功したら True）。

    voicekey の貼り付けの一時書き込み用。Windows の取り決め（Clipboard Formats の文書）どおり
    ExcludeClipboardContentFromMonitorProcessing と CanIncludeInClipboardHistory=0 を一緒に置き、
    voicekey 自身のクリップボード履歴にも Win+V の履歴にも他のクリップボード管理アプリにも残させない。
    Windows 以外・失敗時は False（呼び出し側は従来の pyperclip.copy に戻る）。
    """
    if not sys.platform.startswith("win"):
        return False
    try:
        user32, kernel32 = _win32()
    except Exception:
        return False
    owner = _open_for_write(user32)
    if not owner:
        return False

    def put(fmt: int, data: bytes) -> bool:
        handle = kernel32.GlobalAlloc(0x0002, max(1, len(data)))  # GMEM_MOVEABLE
        if not handle:
            return False
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            kernel32.GlobalFree(handle)
            return False
        ctypes.memmove(pointer, data, len(data))
        kernel32.GlobalUnlock(handle)
        # 成功したらハンドルの持ち主はクリップボードになる（失敗したときだけ自分で解放する）
        if not user32.SetClipboardData(fmt, handle):
            kernel32.GlobalFree(handle)
            return False
        return True

    try:
        user32.EmptyClipboard()
        if not put(13, text.encode("utf-16-le") + b"\x00\x00"):  # CF_UNICODETEXT
            return False
        # 印は付けられなくても本文は入っているので貼り付けは続ける（履歴に残るだけ）
        for name in ("ExcludeClipboardContentFromMonitorProcessing", "CanIncludeInClipboardHistory"):
            fmt = user32.RegisterClipboardFormatW(name)
            if fmt:
                put(fmt, b"\x00\x00\x00\x00")
        return True
    except Exception as e:
        logger.debug(f"印付きのクリップボード書き込みに失敗: {e}")
        return False
    finally:
        _close_for_write(user32, owner)
