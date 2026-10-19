"""
クリップボードの全形式の退避と復元（Windows・Mac 版 Paster.swift の退避上限の移植）

音声入力の貼り付けはクリップボード経由なので、貼る前に利用者のクリップボードを退避し、
貼った後に戻す。以前の Windows 版は文字列しか退避しておらず、画像やファイルをコピーしたまま
音声入力すると、貼り付け後にクリップボードが空になって元の内容が消えていた。

Mac と同じく合計 32MB・64 種類の上限を設け、超えるときは文字列・RTF・HTML・URL など軽い型を
優先して退避する。1 つも退避できなかった（全部が上限超え・読めない）ときは、呼び出し側が
クリップボードを消さずに残す（消すと元の内容が確実に失われるため）。

Win32 API を ctypes で直接呼ぶ。Windows 以外・失敗時は None を返し、呼び出し側は従来の
文字列だけの退避に戻る（例外は外に出さない）。
"""

import ctypes
import sys
import time
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

from ...utils.logger import get_logger

logger = get_logger(__name__)

# 退避の上限（Mac と同じ）
MAX_TOTAL_BYTES = 32 * 1024 * 1024
MAX_FORMATS = 64

# GlobalAlloc で作れない（GDI オブジェクト・メタファイルなど）か、復元すると壊れる形式は退避しない。
# CF_BITMAP は CF_DIB から OS が合成し直すので、CF_DIB を戻せば画像は戻る
_SKIP_FORMATS = {
    2,      # CF_BITMAP
    3,      # CF_METAFILEPICT
    9,      # CF_PALETTE
    14,     # CF_ENHMETAFILE
    0x0080,  # CF_OWNERDISPLAY
    0x0082,  # CF_DSPBITMAP
    0x0083,  # CF_DSPMETAFILEPICT
    0x008E,  # CF_DSPENHMETAFILE
}
_GDIOBJ_RANGE = range(0x0300, 0x0400)

# 軽くて大事な形式（上限を超えるときに優先して残す）。名前付き形式は登録名で見る
_LIGHT_STANDARD = {13, 1, 7, 16}  # CF_UNICODETEXT / CF_TEXT / CF_OEMTEXT / CF_LOCALE
_LIGHT_NAMES = {
    "rich text format", "html format", "uniformresourcelocatorw", "uniformresourcelocator",
    "text/html", "text/plain",
}


@dataclass
class ClipboardSnapshot:
    """退避したクリップボード。

    Attributes:
        items: 退避できた (形式番号, データ) の並び（元の順）
        had_content: 退避の前にクリップボードに何か入っていたか
        complete: すべての形式を退避できたか（上限で一部を諦めたら False）
    """

    items: List[Tuple[int, bytes]] = field(default_factory=list)
    had_content: bool = False
    complete: bool = True


def select_formats(
    formats: Sequence[Tuple[int, str, int]],
    max_total: int = MAX_TOTAL_BYTES,
    max_count: int = MAX_FORMATS,
) -> Tuple[List[int], bool]:
    """退避する形式を選ぶ（純関数・テスト対象）。

    すべて入るなら元の順のまま全部。入らないときは軽い形式（文字列・RTF・HTML・URL）を先に、
    残りを元の順に、合計バイト数と種類数の上限まで詰める。

    Args:
        formats: (形式番号, 登録名 or "", バイト数) の並び（列挙順）
        max_total: 合計バイト数の上限
        max_count: 種類数の上限

    Returns:
        (選んだ形式番号の並び〔元の順〕, 全部選べたか)
    """
    usable = [f for f in formats if f[0] not in _SKIP_FORMATS and f[0] not in _GDIOBJ_RANGE and f[2] >= 0]
    total = sum(size for _, _, size in usable)
    if total <= max_total and len(usable) <= max_count:
        # 退避しない形式（CF_BITMAP など）は、戻した CF_DIB から OS が作り直すので欠けとは数えない
        return [fmt for fmt, _, _ in usable], True

    def is_light(entry) -> bool:
        fmt, name, _ = entry
        return fmt in _LIGHT_STANDARD or name.lower() in _LIGHT_NAMES

    ordered = [e for e in usable if is_light(e)] + [e for e in usable if not is_light(e)]
    chosen = set()
    used = 0
    for fmt, _, size in ordered:
        if len(chosen) >= max_count:
            break
        if used + size > max_total:
            continue
        chosen.add(fmt)
        used += size
    return [fmt for fmt, _, _ in usable if fmt in chosen], False


def _win32():
    """user32 / kernel32 の関数を型付きで用意する。"""
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.CloseClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.EnumClipboardFormats.argtypes = [wintypes.UINT]
    user32.EnumClipboardFormats.restype = wintypes.UINT
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = wintypes.HANDLE
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE
    user32.GetClipboardFormatNameW.argtypes = [wintypes.UINT, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClipboardFormatNameW.restype = ctypes.c_int
    kernel32.GlobalSize.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalSize.restype = ctypes.c_size_t
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    return user32, kernel32


def _open(user32) -> bool:
    """クリップボードを開く（他のアプリが開いていることがあるので数回だけ待って再試行する）。"""
    for _ in range(10):
        if user32.OpenClipboard(None):
            return True
        time.sleep(0.01)
    return False


def take_snapshot() -> Optional[ClipboardSnapshot]:
    """いまのクリップボードを退避する（Windows 以外・開けなかったときは None）。"""
    if not sys.platform.startswith("win"):
        return None
    try:
        user32, kernel32 = _win32()
    except Exception:
        return None
    if not _open(user32):
        return None
    try:
        formats: List[Tuple[int, str, int]] = []
        handles = {}
        fmt = user32.EnumClipboardFormats(0)
        while fmt:
            name = ""
            if fmt >= 0xC000:
                buf = ctypes.create_unicode_buffer(256)
                if user32.GetClipboardFormatNameW(fmt, buf, 256) > 0:
                    name = buf.value
            size = -1
            if fmt not in _SKIP_FORMATS and fmt not in _GDIOBJ_RANGE:
                handle = user32.GetClipboardData(fmt)
                if handle:
                    size = int(kernel32.GlobalSize(handle))
                    handles[fmt] = handle
            formats.append((fmt, name, size))
            fmt = user32.EnumClipboardFormats(fmt)
        snapshot = ClipboardSnapshot(had_content=bool(formats))
        selected, complete = select_formats(formats)
        snapshot.complete = complete
        for fmt in selected:
            handle = handles.get(fmt)
            if not handle:
                continue
            size = int(kernel32.GlobalSize(handle))
            pointer = kernel32.GlobalLock(handle)
            if not pointer:
                snapshot.complete = False
                continue
            try:
                snapshot.items.append((fmt, ctypes.string_at(pointer, size)))
            finally:
                kernel32.GlobalUnlock(handle)
        return snapshot
    except Exception as e:
        logger.debug(f"クリップボードの退避に失敗（文字列だけの退避に戻します）: {e}")
        return None
    finally:
        user32.CloseClipboard()


def restore_snapshot(snapshot: ClipboardSnapshot) -> bool:
    """退避したクリップボードを書き戻す（成功したら True）。"""
    if not sys.platform.startswith("win") or not snapshot.items:
        return False
    try:
        user32, kernel32 = _win32()
    except Exception:
        return False
    if not _open(user32):
        return False
    try:
        user32.EmptyClipboard()
        restored = 0
        for fmt, data in snapshot.items:
            handle = kernel32.GlobalAlloc(0x0002, max(1, len(data)))  # GMEM_MOVEABLE
            if not handle:
                continue
            pointer = kernel32.GlobalLock(handle)
            if not pointer:
                kernel32.GlobalFree(handle)
                continue
            ctypes.memmove(pointer, data, len(data))
            kernel32.GlobalUnlock(handle)
            # 成功したらハンドルの持ち主はクリップボードになる（失敗したときだけ自分で解放する）
            if not user32.SetClipboardData(fmt, handle):
                kernel32.GlobalFree(handle)
                continue
            restored += 1
        return restored > 0
    except Exception as e:
        logger.debug(f"クリップボードの復元に失敗: {e}")
        return False
    finally:
        user32.CloseClipboard()
