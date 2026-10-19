"""
Windows向けプラットフォーム実装。
"""

from typing import Optional, Sequence

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QSystemTrayIcon
from pynput.keyboard import Key

from ..base import PlatformAdapter
from ..common import normalize_listener_key, qt_key_to_hotkey_token


class WindowsPlatformAdapter(PlatformAdapter):
    """Windows差分実装。"""

    name = "windows"
    font_css_stack = "'Segoe UI', 'Yu Gothic UI', 'Meiryo', 'Arial'"

    _MODIFIER_VK_MAP = {
        0xA2: "<ctrl_l>",
        0xA0: "<shift_l>",
        0xA4: "<alt_l>",
        0x5B: "<cmd_l>",
        0xA3: "<ctrl_r>",
        0xA1: "<shift_r>",
        0xA5: "<alt_r>",
        0x5C: "<cmd_r>",
    }

    _MODIFIER_SCAN_MAP = {
        29: "<ctrl_l>",
        42: "<shift_l>",
        56: "<alt_l>",
        285: "<ctrl_r>",
        54: "<shift_r>",
        312: "<alt_r>",
        57400: "<alt_r>",
        57372: "<ctrl_r>",
        57373: "<ctrl_r>",
    }

    _QT_FALLBACK_MODIFIERS = {
        Qt.Key.Key_Control: "<ctrl>",
        Qt.Key.Key_Shift: "<shift>",
        Qt.Key.Key_Alt: "<alt>",
        Qt.Key.Key_Meta: "<cmd>",
    }

    # 日本語（JIS）配列の専用キー。pynput では KeyCode(vk=..., char=None) で届き、
    # name も char も無いため共通の normalize_listener_key では拾えない。vk から
    # 判定側トークン（<> 無し）へ変換する。記録側 keymap._SPECIAL_KEY_MAP の
    # "<nonconvert>" 等は _parse_hotkey の <> 剥がしを通してここと一致する。
    # VK 実値は環境・キーボードドライバで揺れるため複数 VK を同一トークンに寄せる。
    _JIS_VK_MAP = {
        0x1D: "nonconvert",       # 無変換
        0x1C: "convert",          # 変換
        0xF2: "kana",             # ひらがな/カタカナ（かな）
        0xF3: "hankaku_zenkaku",  # 半角/全角
        0xF4: "hankaku_zenkaku",  # 半角/全角（別ドライバ）
        0x19: "hankaku_zenkaku",  # 漢字
        0xF0: "eisu",             # 英数
    }

    @property
    def paste_modifier(self) -> Key:
        return Key.ctrl

    @property
    def tray_open_reasons(self) -> Sequence[QSystemTrayIcon.ActivationReason]:
        return (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        )

    def normalize_listener_key(self, key) -> Optional[str]:
        # Ctrl を押しながら文字キーを押すと、pynput は char に制御文字（Ctrl+V なら '\x16'）を入れて届ける。
        # そのままだと「ctrl+v」のような組み合わせキー（再貼り付けキー等）が一致しないので、
        # 英数字の仮想キーコード（0-9 / A-Z）なら仮想キーから文字を引き直す
        vk = getattr(key, "vk", None)
        char = getattr(key, "char", None)
        if vk is not None and (0x30 <= vk <= 0x39 or 0x41 <= vk <= 0x5A):
            if not char or not char.isprintable():
                return chr(vk).lower()
        token = normalize_listener_key(key)
        if token is not None:
            return token
        # 共通正規化で拾えない＝日本語専用キー等。vk から判定用トークンを引く。
        vk = getattr(key, "vk", None)
        if vk is not None and vk in self._JIS_VK_MAP:
            return self._JIS_VK_MAP[vk]
        return None

    def modifier_hotkey_from_native(
        self,
        virtual_key: int,
        scan_code: int = 0,
        qt_key: Optional[int] = None,
    ) -> str:
        if virtual_key in self._MODIFIER_VK_MAP:
            return self._MODIFIER_VK_MAP[virtual_key]
        if scan_code in self._MODIFIER_SCAN_MAP:
            return self._MODIFIER_SCAN_MAP[scan_code]
        if qt_key is not None and qt_key in self._QT_FALLBACK_MODIFIERS:
            return self._QT_FALLBACK_MODIFIERS[qt_key]
        return ""

    def qt_key_to_hotkey_token(self, key: int, scan_code: int = 0) -> str:
        return qt_key_to_hotkey_token(key, scan_code)

    def foreground_app(self):
        """最前面アプリの (実行ファイル名, 表示名)（Win32 API。失敗時は (None, None)）。"""
        from .foreground import foreground_app

        return foreground_app()
