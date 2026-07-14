"""
UIモジュール

設定ウィンドウ、システムトレイ／メニューバーアイコン、録音中 HUD を提供する。
状態表示はトレイアイコンの色に加え、画面下部中央の小型 HUD（録音中のみ・
ライブ字幕対応）で行う。旧来の Dynamic Island 風オーバーレイは持たない。
"""

from .hud import Hud
from .settings_window import SettingsWindow
from .side_notch import SideNotch
from .system_tray import SystemTray

__all__ = [
    "SettingsWindow",        # 設定ウィンドウ
    "SystemTray",            # システムトレイアイコン
    "Hud",                   # 録音中 HUD（下部中央の小型ピル）
    "SideNotch",             # 画面左端のサイドノッチ（履歴スリット）
]
