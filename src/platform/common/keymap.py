"""
共通キー変換ユーティリティ。
"""

from typing import Any, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence


_SPECIAL_KEY_MAP = {
    # ファンクションキー
    Qt.Key.Key_F1: "<f1>",
    Qt.Key.Key_F2: "<f2>",
    Qt.Key.Key_F3: "<f3>",
    Qt.Key.Key_F4: "<f4>",
    Qt.Key.Key_F5: "<f5>",
    Qt.Key.Key_F6: "<f6>",
    Qt.Key.Key_F7: "<f7>",
    Qt.Key.Key_F8: "<f8>",
    Qt.Key.Key_F9: "<f9>",
    Qt.Key.Key_F10: "<f10>",
    Qt.Key.Key_F11: "<f11>",
    Qt.Key.Key_F12: "<f12>",
    Qt.Key.Key_F13: "<f13>",
    Qt.Key.Key_F14: "<f14>",
    Qt.Key.Key_F15: "<f15>",
    Qt.Key.Key_F16: "<f16>",
    Qt.Key.Key_F17: "<f17>",
    Qt.Key.Key_F18: "<f18>",
    Qt.Key.Key_F19: "<f19>",
    Qt.Key.Key_F20: "<f20>",
    Qt.Key.Key_F21: "<f21>",
    Qt.Key.Key_F22: "<f22>",
    Qt.Key.Key_F23: "<f23>",
    Qt.Key.Key_F24: "<f24>",
    # 特殊キー
    Qt.Key.Key_Space: "<space>",
    Qt.Key.Key_Tab: "<tab>",
    Qt.Key.Key_Return: "<enter>",
    Qt.Key.Key_Enter: "<enter>",
    Qt.Key.Key_Backspace: "<backspace>",
    Qt.Key.Key_Delete: "<delete>",
    Qt.Key.Key_Escape: "<esc>",
    Qt.Key.Key_CapsLock: "<caps_lock>",
    Qt.Key.Key_NumLock: "<num_lock>",
    Qt.Key.Key_ScrollLock: "<scroll_lock>",
    Qt.Key.Key_Pause: "<pause>",
    Qt.Key.Key_Print: "<print_screen>",
    Qt.Key.Key_SysReq: "<print_screen>",
    # ナビゲーション
    Qt.Key.Key_Home: "<home>",
    Qt.Key.Key_End: "<end>",
    Qt.Key.Key_PageUp: "<page_up>",
    Qt.Key.Key_PageDown: "<page_down>",
    Qt.Key.Key_Up: "<up>",
    Qt.Key.Key_Down: "<down>",
    Qt.Key.Key_Left: "<left>",
    Qt.Key.Key_Right: "<right>",
    Qt.Key.Key_Insert: "<insert>",
    # テンキー
    Qt.Key.Key_division: "<num_divide>",
    Qt.Key.Key_multiply: "<num_multiply>",
    Qt.Key.Key_Minus: "<num_subtract>",
    Qt.Key.Key_Plus: "<num_add>",
    # メディアキー
    Qt.Key.Key_MediaPlay: "<media_play_pause>",
    Qt.Key.Key_MediaStop: "<media_stop>",
    Qt.Key.Key_MediaPrevious: "<media_previous>",
    Qt.Key.Key_MediaNext: "<media_next>",
    Qt.Key.Key_VolumeUp: "<media_volume_up>",
    Qt.Key.Key_VolumeDown: "<media_volume_down>",
    Qt.Key.Key_VolumeMute: "<media_volume_mute>",
}


# 日本語（JIS）配列の専用キー（記録側トークン）。Qt のバージョンによっては
# 未定義の列挙があるため getattr で存在するものだけ登録する。判定側の
# windows/adapter._JIS_VK_MAP と同じ語彙（<> を剥がすと一致）にする。
for _qt_name, _token in (
    ("Key_Muhenkan", "<nonconvert>"),
    ("Key_Henkan", "<convert>"),
    ("Key_Hiragana_Katakana", "<kana>"),
    ("Key_Hiragana", "<kana>"),
    ("Key_Katakana", "<kana>"),
    ("Key_Zenkaku_Hankaku", "<hankaku_zenkaku>"),
    ("Key_Eisu_toggle", "<eisu>"),
):
    _qt_key = getattr(Qt.Key, _qt_name, None)
    if _qt_key is not None:
        _SPECIAL_KEY_MAP[_qt_key] = _token


def normalize_listener_key(key: Any) -> Optional[str]:
    """
    pynputキーイベントを正規化する。
    """
    try:
        if hasattr(key, "name"):
            name = key.name.lower()
            if name == "alt_gr":
                return "alt_r"
            return name
        if hasattr(key, "char") and key.char:
            return key.char.lower()
    except Exception:
        return None
    return None


def qt_key_to_hotkey_token(key: int, scan_code: int = 0) -> str:
    """
    Qtキーコードをpynput形式トークンに変換する。
    """
    if key in _SPECIAL_KEY_MAP:
        return _SPECIAL_KEY_MAP[key]

    text = QKeySequence(key).toString().lower()
    if not text:
        return ""
    if len(text) == 1:
        return text
    return f"<{text}>"
