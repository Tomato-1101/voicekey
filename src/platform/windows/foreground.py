"""
最前面アプリの取得（Windows・Mac 版 FrontAppTracker.swift の Windows 版）

貼り付け先アプリを履歴（アプリ名）と診断ログ（貼付先）に残すために、最前面ウィンドウの
プロセスの実行ファイル名と表示名（実行ファイルのバージョン情報の FileDescription）を返す。
ctypes で Win32 API を直接呼ぶ（追加の依存なし）。Windows 以外・失敗時は (None, None)。

表示名の取得（バージョン情報の読み出し）は数 ms かかるので、実行ファイルのパスごとに覚える。
"""

import os
import sys
import threading
from typing import Dict, Optional, Tuple

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

_name_cache: Dict[str, str] = {}
_cache_lock = threading.Lock()


def foreground_app() -> Tuple[Optional[str], Optional[str]]:
    """最前面アプリの (実行ファイル名, 表示名) を返す（例: ("chrome.exe", "Google Chrome")）。

    Returns:
        取れなければ (None, None)
    """
    if not sys.platform.startswith("win"):
        return None, None
    try:
        path = _foreground_exe_path()
    except Exception:
        return None, None
    if not path:
        return None, None
    exe = os.path.basename(path)
    return exe, display_name(path)


def display_name(path: str) -> str:
    """実行ファイルの表示名（FileDescription。無ければ拡張子を除いた名前）。"""
    with _cache_lock:
        cached = _name_cache.get(path)
    if cached:
        return cached
    try:
        name = _file_description(path)
    except Exception:
        name = None
    if not name:
        name = os.path.splitext(os.path.basename(path))[0]
    with _cache_lock:
        _name_cache[path] = name
    return name


def _foreground_exe_path() -> Optional[str]:
    """最前面ウィンドウを持つプロセスの実行ファイルのフルパス（Win32 API）。"""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return None
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not handle:
        return None
    try:
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return None
        return buf.value
    finally:
        kernel32.CloseHandle(handle)


def _file_description(path: str) -> Optional[str]:
    """実行ファイルのバージョン情報から FileDescription を読む（無ければ None）。"""
    import ctypes
    from ctypes import wintypes

    version = ctypes.WinDLL("version")
    version.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
    version.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
    version.VerQueryValueW.argtypes = [
        ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.UINT),
    ]

    size = version.GetFileVersionInfoSizeW(path, None)
    if not size:
        return None
    data = ctypes.create_string_buffer(size)
    if not version.GetFileVersionInfoW(path, 0, size, data):
        return None
    # 言語とコードページの組（最初の 1 つ）を読み、その言語の FileDescription を引く
    ptr = ctypes.c_void_p()
    length = wintypes.UINT()
    if not version.VerQueryValueW(data, "\\VarFileInfo\\Translation", ctypes.byref(ptr), ctypes.byref(length)):
        return None
    if length.value < 4:
        return None
    lang, codepage = ctypes.cast(ptr, ctypes.POINTER(wintypes.WORD * 2)).contents
    key = f"\\StringFileInfo\\{lang:04x}{codepage:04x}\\FileDescription"
    if not version.VerQueryValueW(data, key, ctypes.byref(ptr), ctypes.byref(length)):
        return None
    if not length.value:
        return None
    text = ctypes.wstring_at(ptr.value, length.value).rstrip("\x00").strip()
    return text or None
