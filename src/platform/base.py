"""
プラットフォーム抽象レイヤー。

OS差分（入力挿入・キー正規化・トレイ挙動・フォント）を
共通インターフェースとして定義する。
"""

from abc import ABC, abstractmethod
from typing import Any, Dict, Optional, Sequence

from pynput.keyboard import Key


class PlatformAdapter(ABC):
    """OS別機能を提供する抽象インターフェース。"""

    name: str = "unknown"
    font_css_stack: str = "'Arial'"

    @property
    @abstractmethod
    def paste_modifier(self) -> Key:
        """貼り付けショートカットで使用する修飾キーを返す。"""
        raise NotImplementedError

    @property
    @abstractmethod
    def tray_open_reasons(self) -> Sequence[Any]:
        """設定UIを開くトレイアクティベーション理由一覧を返す。"""
        raise NotImplementedError

    def is_tray_open_reason(self, reason: Any) -> bool:
        """トレイのアクティベーション理由が設定表示対象か判定する。"""
        return reason in self.tray_open_reasons

    @abstractmethod
    def normalize_listener_key(self, key: Any) -> Optional[str]:
        """pynputキーイベントを正規化文字列へ変換する。"""
        raise NotImplementedError

    @abstractmethod
    def modifier_hotkey_from_native(
        self,
        virtual_key: int,
        scan_code: int = 0,
        qt_key: Optional[int] = None,
    ) -> str:
        """Qtネイティブキー情報から修飾キー表現を返す。"""
        raise NotImplementedError

    @abstractmethod
    def qt_key_to_hotkey_token(self, key: int, scan_code: int = 0) -> str:
        """Qtキーコードをpynput形式トークンに変換する。"""
        raise NotImplementedError

    # --- ウィンドウ可視性制御（macOS でのみ実体を持つ。既定は no-op） ---

    def configure_app_visibility(self, hide_from_dock: bool) -> None:
        """
        アプリ可視性（Dock / タスクバー登録）を切り替える。

        macOS のみ実装が必要。Windows はタスクバーに出るのが通常挙動なので何もしない。

        Args:
            hide_from_dock: True なら Dock / タスクバーから隠す
        """
        return None

    def bring_to_front(self, window: Any) -> None:
        """
        指定ウィンドウを最前面に持ち上げる OS ネイティブ処理。

        Qt の `raise_()` / `activateWindow()` だけでは macOS の Accessory モードで
        前面化できないため、AppKit の `activateIgnoringOtherApps_` 等で補強する。
        Windows では Qt 標準処理で十分なので no-op。

        Args:
            window: 対象ウィンドウ（QWidget 等）
        """
        return None

    # --- 入力系権限（macOS でのみ実体を持つ。既定は「すべて許可済み」扱い） ---

    def check_input_permissions(self) -> Dict[str, bool]:
        """
        グローバルホットキー・テキスト入力に必要な OS 権限の状態を返す。

        Returns:
            {"accessibility": bool, "input_monitoring": bool}
            （macOS 以外は常に True。権限の概念がないため）
        """
        return {"accessibility": True, "input_monitoring": True}

    def open_permission_settings(self, which: str) -> None:
        """
        OS の該当権限設定画面を開く。

        Args:
            which: "accessibility" または "input_monitoring"
        """
        return None
