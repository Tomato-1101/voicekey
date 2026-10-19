"""
システムトレイモジュール

タスクバー通知領域にアイコンを表示し、
アプリケーション状態の表示とコンテキストメニューを提供する。
"""

from typing import Optional, Union

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QIcon,
    QPainter,
    QPen,
    QPixmap,
    QRadialGradient,
)
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

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
    # ライブ字幕・Meet 議事録ボット（細かい設定は設定画面の「字幕と議事録」に集約。ここは入口だけ）
    caption_toggle = Signal()
    caption_settings = Signal()
    meet_join = Signal()
    meet_leave = Signal()
    meet_login = Signal()
    meet_open_latest = Signal()
    meet_open_folder = Signal()
    meet_settings = Signal()
    
    # 状態別アイコンカラー（ブランド「17 LEGEND」の配色・Mac の Brand.swift と同じ値）。
    # Mac のメニューバーは「待機＝輪／録音中＝橙の点／変換中＝橙の輪」なので、Windows も
    # 灯りの橙（#FF5A1F）を唯一のアクセントにし、変換中は輪（_RING_STATES）で描く
    ICON_COLORS = {
        AppState.IDLE: QColor("#7F796F"),                  # 待機中：刻印（legend）
        AppState.RECORDING: QColor("#FF5A1F"),             # 録音中：灯り（signal）
        AppState.RECORDING_AUTO_ENTER: QColor("#D9480F"),  # 録音中（auto_enter）：深い灯り（signalDeep）
        AppState.TRANSCRIBING: QColor("#FF5A1F"),          # 文字起こし中：灯りの輪
    }
    # 塗りつぶしではなく輪で描く状態（Mac の「待機＝輪・変換中＝橙の輪」）
    _RING_STATES = (AppState.IDLE, AppState.TRANSCRIBING)
    
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
        self._set_icon_color(self.ICON_COLORS[AppState.IDLE], ring=True)

    def _setup_menu(self) -> None:
        """コンテキストメニューを設定する。"""
        self._menu = QMenu()

        # 今日の API 代（Mac のメニューバーと同じ。メニューを開くたびに記録から計算し直す）。
        # 表示だけの項目なので押せない。中身は app が cost_text_provider で渡す
        self.cost_text_provider = None
        self._cost_action = self._menu.addAction("")
        self._cost_action.setEnabled(False)
        self._cost_action.setVisible(False)
        self._menu.aboutToShow.connect(self._refresh_cost_action)

        # ライブ字幕（状態の行＋開始/停止）と Meet 議事録ボット（サブメニュー）。
        # 状態は app が caption_status_provider / meet_status_provider で渡す（メニューを開くたびに読み直す）
        self.caption_status_provider = None
        self.meet_status_provider = None
        self._caption_status_action = self._menu.addAction("ライブ字幕: 停止中")
        self._caption_status_action.setEnabled(False)
        self._caption_detail_action = self._menu.addAction("")
        self._caption_detail_action.setEnabled(False)
        self._caption_detail_action.setVisible(False)
        self._caption_toggle_action = self._menu.addAction("字幕を開始")
        self._caption_toggle_action.triggered.connect(self.caption_toggle.emit)
        caption_settings_action = self._menu.addAction("ライブ字幕の設定…")
        caption_settings_action.triggered.connect(self.caption_settings.emit)
        self._meet_menu = self._menu.addMenu("Meet 議事録ボット")
        self._meet_status_action = self._meet_menu.addAction("参加していません")
        self._meet_status_action.setEnabled(False)
        self._meet_source_action = self._meet_menu.addAction("")
        self._meet_source_action.setEnabled(False)
        self._meet_menu.addSeparator()
        self._meet_join_action = self._meet_menu.addAction("会議に参加…")
        self._meet_join_action.triggered.connect(self.meet_join.emit)
        self._meet_leave_action = self._meet_menu.addAction("会議から退出")
        self._meet_leave_action.triggered.connect(self.meet_leave.emit)
        self._meet_login_action = self._meet_menu.addAction("ボット用ブラウザで Google にログイン…")
        self._meet_login_action.triggered.connect(self.meet_login.emit)
        self._meet_menu.addSeparator()
        latest_action = self._meet_menu.addAction("最新の議事録を開く")
        latest_action.triggered.connect(self.meet_open_latest.emit)
        folder_action = self._meet_menu.addAction("議事録フォルダを開く")
        folder_action.triggered.connect(self.meet_open_folder.emit)
        meet_settings_action = self._meet_menu.addAction("議事録・会議の設定…")
        meet_settings_action.triggered.connect(self.meet_settings.emit)
        self._menu.aboutToShow.connect(self._refresh_caption_actions)
        self._menu.addSeparator()

        # 設定メニュー項目（Mac 版メニューバーと同じ文言）
        settings_action = self._menu.addAction("設定…")
        settings_action.triggered.connect(self.open_settings.emit)

        # セットアップガイドの再表示項目は撤去（Mac と同じく起動時の自動判定のみで表示する。
        # ユーザー指示: 「起動時にだけ表示するようにして」）。

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

    def _refresh_cost_action(self) -> None:
        """「今日の API 代 $x.xx」をメニューを開くたびに計算し直す（失敗しても項目を隠すだけ）。"""
        provider = self.cost_text_provider
        text = ""
        if provider is not None:
            try:
                text = provider() or ""
            except Exception:
                text = ""
        self._cost_action.setText(text)
        self._cost_action.setVisible(bool(text))

    def _refresh_caption_actions(self) -> None:
        """字幕とボットの行をメニューを開くたびに読み直す（失敗しても既定の表示のまま）。"""
        caption = None
        if self.caption_status_provider is not None:
            try:
                caption = self.caption_status_provider()
            except Exception:
                caption = None
        title, lines, active = caption or ("停止中", [], False)
        self._caption_status_action.setText(f"ライブ字幕: {title}")
        detail = " / ".join(lines)
        self._caption_detail_action.setText(detail)
        self._caption_detail_action.setVisible(bool(detail))
        self._caption_toggle_action.setText("字幕を停止" if active else "字幕を開始")

        meet = None
        if self.meet_status_provider is not None:
            try:
                meet = self.meet_status_provider()
            except Exception:
                meet = None
        meet = meet or {"title": "参加していません", "active": False, "chrome": True, "source": "", "badge": ""}
        self._meet_menu.setTitle("Meet 議事録ボット" + meet.get("badge", ""))
        if not meet.get("chrome", True):
            self._meet_status_action.setText("Google Chrome が必要です")
        else:
            self._meet_status_action.setText(meet.get("title", ""))
        source = meet.get("source", "")
        self._meet_source_action.setText(f"文字起こし: {source}" if source else "")
        self._meet_source_action.setVisible(bool(source))
        active = bool(meet.get("active"))
        self._meet_join_action.setVisible(not active)
        self._meet_join_action.setEnabled(bool(meet.get("chrome", True)))
        self._meet_leave_action.setVisible(active)
        self._meet_login_action.setEnabled(bool(meet.get("chrome", True)) and not active)

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
        """フィードバック入力ダイアログを開く（本文を自社サーバーへ送信する）。

        ログイン済みならアカウントに紐づき、未ログインでも匿名で送れる。
        遅延 import で起動コスト・循環 import を避ける。
        """
        from .feedback_dialog import FeedbackDialog

        dialog = FeedbackDialog()
        dialog.exec()

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

        self._set_icon_color(color, ring=status in self._RING_STATES)
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

    def _set_icon_color(self, color: QColor, ring: bool = False) -> None:
        """
        指定色の円形アイコンを生成・設定する。

        左上ハイライトの放射グラデーション + 細い縁取りで、
        ベタ塗りの安っぽさを解消する（Mac 版アイコンの質感に寄せる）。

        Args:
            color: アイコンのベース色
            ring: 塗りつぶさず輪で描くか（待機・変換中）
        """
        size = self.ICON_SIZE
        pixmap = QPixmap(size, size)
        pixmap.fill(QColor(0, 0, 0, 0))  # 透明背景

        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        if ring:
            # 輪（Mac の待機・変換中の表現）。タスクバーの明暗どちらでも見えるよう太めにする
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(color, 9))
            painter.drawEllipse(8, 8, size - 16, size - 16)
            painter.end()
            self.setIcon(QIcon(pixmap))
            return

        # 左上に光源があるような立体感を放射グラデーションで付ける
        gradient = QRadialGradient(size * 0.38, size * 0.32, size * 0.72)
        gradient.setColorAt(0.0, color.lighter(140))
        gradient.setColorAt(0.55, color)
        gradient.setColorAt(1.0, color.darker(118))
        painter.setBrush(QBrush(gradient))

        # 細い縁取り（明るいメニューバー上でも輪郭が溶けないように）
        painter.setPen(QPen(QColor(0, 0, 0, 70), 2))
        painter.drawEllipse(4, 4, size - 8, size - 8)

        painter.end()

        self.setIcon(QIcon(pixmap))
