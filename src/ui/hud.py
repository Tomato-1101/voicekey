"""
録音中 HUD モジュール（画面下部中央の小型ピル）

方針は Mac 版 Hud.swift と同じく「録音中のみ・小型・情報最小限」:
- 録音中: 音声レベル連動の波形バー（ストリーミング字幕があればそれを優先表示）
- 変換中: 「変換中…」テキスト
- 通知: エラー・無音検出などを 2 秒だけ表示

最重要の制約: HUD は**絶対にキーフォーカスを奪ってはならない**。
フォーカスを奪うと貼り付け先ウィンドウが変わり、文字起こし結果が別の場所に
入力されてしまう。そのため非アクティブ表示・入力透過・タスクバー非登録の
フレームレスウィンドウとして実装する。
"""

from collections import deque
from typing import Deque

from PySide6.QtCore import Qt, QTimer, QRectF
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QGuiApplication
from PySide6.QtWidgets import QWidget


class Hud(QWidget):
    """
    録音状態を示すフローティング HUD（フレームレス・入力透過・非アクティブ）。

    すべての公開メソッドはメインスレッドから呼ばれる前提（アプリは
    Qt シグナルのキュー接続でワーカースレッドからメインへホップさせる）。

    Attributes:
        enabled: HUD を表示するか（設定で無効化可能。False なら常に非表示）
    """

    WIDTH = 360
    HEIGHT = 48
    BAR_COUNT = 20
    NOTICE_MSEC = 2000  # 通知の表示時間（ミリ秒）

    def __init__(self, enabled: bool = True) -> None:
        """HUD を初期化する（生成時は非表示）。"""
        super().__init__()
        self.enabled = enabled

        self._mode = "hidden"            # hidden/recording/recording_auto_enter/transcribing/notice
        self._caption = ""               # ストリーミングのライブ字幕
        self._notice_text = ""           # 一時通知の本文
        self._levels: Deque[float] = deque([0.0] * self.BAR_COUNT, maxlen=self.BAR_COUNT)

        self._setup_window()

        # 通知の自動消去タイマー（単発）
        self._notice_timer = QTimer(self)
        self._notice_timer.setSingleShot(True)
        self._notice_timer.timeout.connect(self._on_notice_timeout)

    def _setup_window(self) -> None:
        """フォーカスを奪わない・入力を透過するフレームレスウィンドウを構成する。"""
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint        # 枠なし
            | Qt.WindowType.WindowStaysOnTopHint     # 常に最前面
            | Qt.WindowType.Tool                     # タスクバーに出さない
            | Qt.WindowType.WindowDoesNotAcceptFocus  # フォーカスを受け取らない
            | Qt.WindowType.WindowTransparentForInput  # クリック透過
        )
        # 背景を透過してピル形状だけ描画する
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        # 表示時にアクティブ化しない（貼り付け先のフォーカスを維持する最重要設定）
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.resize(self.WIDTH, self.HEIGHT)

    # ------------------------------------------------------------------
    # 公開 API（メインスレッドから呼ぶ）
    # ------------------------------------------------------------------

    def set_state(self, state: str) -> None:
        """
        アプリ状態に応じて HUD 表示を切り替える。

        Args:
            state: "idle"/"recording"/"recording_auto_enter"/"transcribing"
        """
        if state == "idle":
            # 通知表示中はタイマーに任せて消さない
            if self._mode == "notice":
                return
            self._hide()
            return

        if state in ("recording", "recording_auto_enter"):
            self._notice_timer.stop()
            self._caption = ""  # 新しい録音のたびに字幕をリセット
            self._levels = deque([0.0] * self.BAR_COUNT, maxlen=self.BAR_COUNT)
            self._mode = state
            self._show()
        elif state == "transcribing":
            self._notice_timer.stop()
            self._mode = "transcribing"
            self._show()

    def push_level(self, level: float) -> None:
        """音声レベル（0.0-1.0）を波形バーに反映する（録音中のみ）。"""
        if self._mode not in ("recording", "recording_auto_enter"):
            return
        self._levels.append(max(0.0, min(1.0, float(level))))
        self.update()

    def set_caption(self, text: str) -> None:
        """ストリーミングのライブ字幕を更新する（録音中のみ反映）。"""
        if self._mode not in ("recording", "recording_auto_enter"):
            return
        self._caption = text or ""
        self.update()

    def show_notice(self, text: str) -> None:
        """一時通知を NOTICE_MSEC だけ表示する。"""
        if not self.enabled:
            return
        self._notice_text = text or ""
        self._mode = "notice"
        self._show()
        self._notice_timer.start(self.NOTICE_MSEC)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _on_notice_timeout(self) -> None:
        """通知タイマー満了で HUD を隠す（録音が再開していなければ）。"""
        if self._mode == "notice":
            self._hide()

    def _show(self) -> None:
        """HUD を画面下部中央へ配置して表示する（アクティブ化しない）。"""
        if not self.enabled:
            return
        self._reposition()
        if not self.isVisible():
            self.show()
        self.raise_()
        self.update()

    def _hide(self) -> None:
        """HUD を隠す。"""
        self._mode = "hidden"
        self.hide()

    def _reposition(self) -> None:
        """プライマリスクリーンの下部中央に配置する。"""
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            return
        geo = screen.availableGeometry()
        x = geo.center().x() - self.WIDTH // 2
        y = geo.bottom() - self.HEIGHT - 40  # 画面下から少し浮かせる
        self.move(x, y)

    # ------------------------------------------------------------------
    # 描画
    # ------------------------------------------------------------------

    def paintEvent(self, event) -> None:
        """ピル背景と状態別コンテンツを描画する。"""
        if self._mode == "hidden":
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # ピル背景（半透明のダーク。クリック透過なので見た目だけ）
        rect = QRectF(1, 1, self.width() - 2, self.height() - 2)
        path = QPainterPath()
        radius = rect.height() / 2
        path.addRoundedRect(rect, radius, radius)
        painter.fillPath(path, QColor(28, 28, 30, 235))
        painter.setPen(QColor(255, 255, 255, 26))
        painter.drawPath(path)

        if self._mode in ("recording", "recording_auto_enter"):
            self._paint_recording(painter)
        elif self._mode == "transcribing":
            self._paint_text(painter, "変換中…", QColor(200, 200, 205))
        elif self._mode == "notice":
            self._paint_text(painter, self._notice_text, QColor(220, 220, 225))

        painter.end()

    def _paint_recording(self, painter: QPainter) -> None:
        """録音中: 状態ドット + （字幕があれば字幕、なければ波形バー）。"""
        auto_enter = self._mode == "recording_auto_enter"
        dot_color = QColor("#BF40BF") if auto_enter else QColor("#FF3B30")

        # 左の状態ドット
        cy = self.height() / 2
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(dot_color)
        painter.drawEllipse(int(20), int(cy - 4), 8, 8)

        content_left = 40
        content_right = self.width() - 20

        if self._caption:
            # ライブ字幕（末尾を残して頭を省略 = 常に最新の語尾が見える）
            self._paint_caption(painter, content_left, content_right)
        else:
            self._paint_bars(painter, content_left, content_right)

    def _paint_caption(self, painter: QPainter, left: int, right: int) -> None:
        """ライブ字幕を右寄せ・頭省略で描画する。"""
        painter.setPen(QColor(245, 245, 247))
        font = QFont()
        font.setPointSize(12)
        font.setWeight(QFont.Weight.Medium)
        painter.setFont(font)
        metrics = painter.fontMetrics()
        avail = right - left
        # 末尾優先で表示（先頭を … で省略）
        elided = metrics.elidedText(self._caption, Qt.TextElideMode.ElideLeft, avail)
        painter.drawText(
            left, 0, avail, self.height(),
            int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
            elided,
        )

    def _paint_bars(self, painter: QPainter, left: int, right: int) -> None:
        """音声レベル連動の波形バーを描画する。"""
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(200, 200, 205, 200))
        avail = right - left
        gap = 3
        bar_w = 3
        total = self.BAR_COUNT * bar_w + (self.BAR_COUNT - 1) * gap
        # バー群を表示領域の中央に寄せる
        start_x = left + max(0, (avail - total) // 2)
        cy = self.height() / 2
        min_h, max_h = 3.0, 22.0
        for i, level in enumerate(self._levels):
            h = min_h + (max_h - min_h) * level
            x = start_x + i * (bar_w + gap)
            painter.drawRoundedRect(QRectF(x, cy - h / 2, bar_w, h), 1.5, 1.5)

    def _paint_text(self, painter: QPainter, text: str, color: QColor) -> None:
        """中央寄せの 1 行テキスト（変換中・通知）を描画する。"""
        painter.setPen(color)
        font = QFont()
        font.setPointSize(12)
        font.setWeight(QFont.Weight.Medium)
        painter.setFont(font)
        metrics = painter.fontMetrics()
        avail = self.width() - 40
        elided = metrics.elidedText(text, Qt.TextElideMode.ElideRight, avail)
        painter.drawText(
            20, 0, avail, self.height(),
            int(Qt.AlignmentFlag.AlignCenter),
            elided,
        )
