"""
システムトレイモジュール

タスクバー通知領域にアイコンを表示し、
アプリケーション状態の表示とコンテキストメニューを提供する。
"""

from typing import Optional, Union

from PySide6.QtCore import Signal, Qt, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from ..config.constants import APP_VERSION
from ..config.types import AppState
from ..platform import PlatformAdapter, get_platform_adapter


class SystemTray(QSystemTrayIcon):
    """
    動的ステータス表示付きシステムトレイアイコン。
    
    アプリケーション状態に応じてアイコンの色が変化し、
    設定画面や終了へのアクセスをコンテキストメニューで提供する。
    
    Signals:
        open_settings: 設定を開く要求
        force_reset: 録音状態の強制リセット要求（フリーズ復帰用）
        quit_app: アプリケーション終了要求
        install_update: 新バージョンのインストール要求（自動アップデート）
    """

    # メニューアクション用シグナル
    open_settings = Signal()
    force_reset = Signal()
    quit_app = Signal()
    install_update = Signal()
    
    # 状態別アイコンカラー
    ICON_COLORS = {
        AppState.IDLE: QColor("dodgerblue"),              # 待機中：青
        AppState.RECORDING: QColor("red"),                 # 録音中：赤
        AppState.RECORDING_AUTO_ENTER: QColor("#BF40BF"),  # 録音中（auto_enter）：紫
        AppState.TRANSCRIBING: QColor("orange"),           # 文字起こし中：オレンジ
    }
    
    # アイコンサイズ（ピクセル）
    ICON_SIZE = 64
    
    def __init__(
        self,
        platform_adapter: Optional[PlatformAdapter] = None,
        parent=None
    ) -> None:
        """システムトレイアイコンを初期化する。"""
        super().__init__(parent)
        self._platform = platform_adapter or get_platform_adapter()

        self._setup_icon()
        self._setup_menu()
        # トレイアイコンの左クリックで直接 Settings を開かない仕様に変更。
        # 左クリックでは setContextMenu で登録したメニューを表示するのみで、
        # ユーザーがメニューから「Settings」を選んだ時に初めてウィンドウを開く。

        self.show()

    def _setup_icon(self) -> None:
        """初期アイコンを設定する。"""
        self._set_icon_color(self.ICON_COLORS[AppState.IDLE])

    def _setup_menu(self) -> None:
        """コンテキストメニューを設定する。"""
        self._menu = QMenu()

        # 設定メニュー項目（Mac 版メニューバーと同じ文言）
        settings_action = self._menu.addAction("設定…")
        settings_action.triggered.connect(self.open_settings.emit)

        # 新バージョン検知時のみ表示するインストール項目（モーダルは出さない方針）
        self._update_action = self._menu.addAction("")
        self._update_action.triggered.connect(self.install_update.emit)
        self._update_action.setVisible(False)

        # フィードバック導線（Mac 版メニューバーと同じ。mailto は既定メーラーで開く）
        feedback_action = self._menu.addAction("フィードバックを送る…")
        feedback_action.triggered.connect(self._send_feedback)

        self._menu.addSeparator()

        # 録音/マイクが詰まったときに再起動なしで内部状態を作り直す脱出口
        reset_action = self._menu.addAction("強制リセット（フリーズ復帰）")
        reset_action.triggered.connect(self.force_reset.emit)

        self._menu.addSeparator()

        # 終了メニュー項目
        quit_action = self._menu.addAction("終了")
        quit_action.triggered.connect(self.quit_app.emit)

        self.setContextMenu(self._menu)

    def show_update_available(self, version: str) -> None:
        """
        新バージョンの通知を表示し、メニューにインストール項目を出す。

        Args:
            version: 検知した新バージョン文字列
        """
        self._update_action.setText(f"アップデート {version} をインストール…")
        self._update_action.setVisible(True)
        self.showMessage(
            "voicekey アップデート",
            f"新しいバージョン {version} があります。\nトレイメニューからインストールできます。",
            QSystemTrayIcon.MessageIcon.Information,
            10000,
        )

    def show_update_failed(self, message: str) -> None:
        """アップデート失敗をトレイ通知で知らせる。"""
        self.showMessage(
            "voicekey アップデート失敗",
            f"{message}\n次回のチェックで再試行されます。",
            QSystemTrayIcon.MessageIcon.Warning,
            10000,
        )

    def _send_feedback(self) -> None:
        """フィードバック用のメール作成画面を開く（件数が増えたらフォーム URL に差し替え）。"""
        QDesktopServices.openUrl(
            QUrl(f"mailto:zhaounhaku@gmail.com?subject=voicekey フィードバック (Windows v{APP_VERSION})")
        )

    def set_status(self, status: Union[str, AppState]) -> None:
        """
        アプリケーション状態に応じてアイコンを更新する。
        
        Args:
            status: 現在のアプリケーション状態
        """
        # 文字列の場合はAppStateに変換
        if isinstance(status, str):
            status = AppState(status)
        
        color = self.ICON_COLORS.get(status, self.ICON_COLORS[AppState.IDLE])
        tooltip = self._get_tooltip(status)
        
        self._set_icon_color(color)
        self.setToolTip(tooltip)

    def _get_tooltip(self, status: AppState) -> str:
        """
        状態に応じたツールチップテキストを取得する。
        
        Args:
            status: 現在のアプリケーション状態
            
        Returns:
            ツールチップ文字列
        """
        tooltips = {
            AppState.IDLE: "voicekey - 待機中",
            AppState.RECORDING: "voicekey - 録音中",
            AppState.RECORDING_AUTO_ENTER: "voicekey - 録音中（自動 Enter）",
            AppState.TRANSCRIBING: "voicekey - 変換中",
        }
        return tooltips.get(status, "voicekey")

    def _set_icon_color(self, color: QColor) -> None:
        """
        指定色の円形アイコンを生成・設定する。
        
        Args:
            color: アイコンの色
        """
        size = self.ICON_SIZE
        pixmap = QPixmap(size, size)
        pixmap.fill(QColor(0, 0, 0, 0))  # 透明背景
        
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        
        painter.setBrush(color)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(4, 4, size - 8, size - 8)
        
        painter.end()
        
        self.setIcon(QIcon(pixmap))
