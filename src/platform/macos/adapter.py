"""
macOS向けプラットフォーム実装。
"""

import subprocess
from typing import Dict, Optional, Sequence

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QSystemTrayIcon
from pynput.keyboard import Key

from ...utils.logger import get_logger
from ..base import PlatformAdapter
from ..common import normalize_listener_key, qt_key_to_hotkey_token

logger = get_logger(__name__)


class MacOSPlatformAdapter(PlatformAdapter):
    """macOS差分実装。"""

    name = "macos"
    font_css_stack = "'Helvetica Neue', '.AppleSystemUIFont', 'Arial'"

    _MODIFIER_VK_MAP = {
        59: "<ctrl_l>",
        62: "<ctrl_r>",
        56: "<shift_l>",
        60: "<shift_r>",
        58: "<alt_l>",
        61: "<alt_r>",
        55: "<cmd_l>",
        54: "<cmd_r>",
    }

    _MODIFIER_SCAN_MAP = {
        59: "<ctrl_l>",
        62: "<ctrl_r>",
        56: "<shift_l>",
        60: "<shift_r>",
        58: "<alt_l>",
        61: "<alt_r>",
        55: "<cmd_l>",
        54: "<cmd_r>",
    }

    _QT_FALLBACK_MODIFIERS = {
        Qt.Key.Key_Control: "<ctrl>",
        Qt.Key.Key_Shift: "<shift>",
        Qt.Key.Key_Alt: "<alt>",
        Qt.Key.Key_Meta: "<cmd>",
    }

    @property
    def paste_modifier(self) -> Key:
        return Key.cmd

    @property
    def tray_open_reasons(self) -> Sequence[QSystemTrayIcon.ActivationReason]:
        return (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        )

    def normalize_listener_key(self, key) -> Optional[str]:
        return normalize_listener_key(key)

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

    # --- ウィンドウ可視性制御（macOS 固有） ---

    def configure_app_visibility(self, hide_from_dock: bool) -> None:
        """
        Dock / Cmd+Tab からアプリを隠す（Accessory モード）。

        メニューバー常駐型アプリとして動作させたい場合に True を渡す。
        PyInstaller ビルド版は Info.plist の `LSUIElement=true` で起動時から
        この状態になるが、`python run.py` で開発実行する場合は本メソッドで
        ランタイムにポリシーを変更する必要がある。
        """
        if not hide_from_dock:
            return
        try:
            # ローカルインポート: pyobjc は macOS でしか入らないため、
            # モジュールロード時に失敗しないよう呼び出し時に import する。
            from AppKit import NSApp, NSApplicationActivationPolicyAccessory  # type: ignore
        except Exception as e:
            logger.warning(f"AppKit を import できないため Dock 非表示を適用しません: {e}")
            return
        try:
            NSApp.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
            logger.debug("macOS Activation Policy を Accessory に設定しました")
        except Exception as e:
            logger.warning(f"Activation Policy 変更に失敗: {e}")

    def bring_to_front(self, window) -> None:
        """
        メニューバーから設定を開いた時に確実に前面化する。

        Accessory モードでは `raise_()` / `activateWindow()` だけでは
        他アプリの上に出ないことがあるため、AppKit 経由で
        `activateIgnoringOtherApps_(True)` を呼ぶ。
        """
        try:
            from AppKit import NSApp  # type: ignore
        except Exception as e:
            logger.debug(f"AppKit 不在のため bring_to_front をスキップ: {e}")
            return
        try:
            NSApp.activateIgnoringOtherApps_(True)
        except Exception as e:
            logger.warning(f"NSApp.activateIgnoringOtherApps_ 失敗: {e}")

    # --- 入力系権限チェック（macOS 固有） ---

    # 権限設定画面への deep link
    _PERMISSION_URLS = {
        "accessibility": "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
        "input_monitoring": "x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent",
    }

    def check_input_permissions(self) -> Dict[str, bool]:
        """
        アクセシビリティ（テキスト挿入用）と入力監視（ホットキー検出用）の
        権限状態を返す。

        権限がないと pynput はイベントを受け取れず「ホットキーが効かない」
        だけが起き、原因がユーザーに見えないため、起動時に明示的に確認する。
        API が使えない環境（pyobjc 不在等）では安全側に True を返す。
        """
        result = {"accessibility": True, "input_monitoring": True}
        try:
            from ApplicationServices import AXIsProcessTrusted  # type: ignore
            result["accessibility"] = bool(AXIsProcessTrusted())
        except Exception as e:
            logger.debug(f"アクセシビリティ権限の確認をスキップ: {e}")
        try:
            import Quartz  # type: ignore
            if hasattr(Quartz, "CGPreflightListenEventAccess"):
                result["input_monitoring"] = bool(Quartz.CGPreflightListenEventAccess())
        except Exception as e:
            logger.debug(f"入力監視権限の確認をスキップ: {e}")
        return result

    def open_permission_settings(self, which: str) -> None:
        """システム設定の該当権限ペインを開く。"""
        url = self._PERMISSION_URLS.get(which)
        if not url:
            return
        try:
            subprocess.Popen(["open", url])
        except Exception as e:
            logger.warning(f"システム設定を開けませんでした: {e}")
