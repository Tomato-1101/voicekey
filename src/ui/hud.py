"""
録音中 HUD モジュール（画面下部中央の小型ピル）

方針は Mac 版 Hud.swift と同じく「録音中のみ・小型・情報最小限」:
- 録音中: 音声レベル連動の波形バー（ストリーミング字幕があればそれを優先表示、
  自動 Enter 時は ⏎ バッジ）
- 変換中: 回転スピナー + 「変換中…」
- 通知: エラー・無音検出などを 2 秒だけ表示
ピルは Mac 版カプセルと同様に内容の幅に合わせて縮む。

最重要の制約: HUD は**絶対にキーフォーカスを奪ってはならない**。
フォーカスを奪うと貼り付け先ウィンドウが変わり、文字起こし結果が別の場所に
入力されてしまう。そのため非アクティブ表示・入力透過・タスクバー非登録の
フレームレスウィンドウとして実装する。
"""

from collections import deque
from typing import Deque

from PySide6.QtCore import Qt, QTimer, QRectF
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QGuiApplication,
)
from PySide6.QtWidgets import QWidget


class Hud(QWidget):
    """
    録音状態を示すフローティング HUD（フレームレス・入力透過・非アクティブ）。

    すべての公開メソッドはメインスレッドから呼ばれる前提（アプリは
    Qt シグナルのキュー接続でワーカースレッドからメインへホップさせる）。

    Attributes:
        enabled: HUD を表示するか（設定で無効化可能。False なら常に非表示）
    """

    # ウィンドウ寸法・バー本数は Mac 版 HudView と一致させる
    WIDTH = 460
    HEIGHT = 56
    PILL_HEIGHT = 40          # ピル本体の高さ（Mac 版カプセル相当。残りは余白）
    PADDING_X = 16            # ピル内の左右パディング
    CONTENT_GAP = 10          # ドット・バー・バッジ間の間隔
    BAR_COUNT = 24
    BAR_WIDTH = 3
    BAR_GAP = 2.5
    SPINNER_RADIUS = 7        # 変換中スピナーの半径
    CAPTION_MAX_WIDTH = 360   # ライブ字幕の最大幅（超えたら頭を省略）
    NOTICE_MSEC = 2000        # 通知の表示時間（ミリ秒）

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

        # 変換中スピナーの回転タイマー（transcribing 表示中のみ動かす）
        self._spin_angle = 0
        self._spin_timer = QTimer(self)
        self._spin_timer.setInterval(33)  # 約 30fps
        self._spin_timer.timeout.connect(self._on_spin_tick)

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
            self._spin_timer.stop()
            self._caption = ""  # 新しい録音のたびに字幕をリセット
            self._levels = deque([0.0] * self.BAR_COUNT, maxlen=self.BAR_COUNT)
            self._mode = state
            self._show()
        elif state == "transcribing":
            self._notice_timer.stop()
            self._mode = "transcribing"
            self._spin_angle = 0
            self._spin_timer.start()
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
        self._spin_timer.stop()
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

    def _on_spin_tick(self) -> None:
        """変換中スピナーを回転させる。"""
        self._spin_angle = (self._spin_angle + 12) % 360
        self.update()

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
        self._spin_timer.stop()
        self.hide()

    def _reposition(self) -> None:
        """プライマリスクリーンの下部中央に配置する。"""
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            return
        geo = screen.availableGeometry()
        x = geo.center().x() - self.WIDTH // 2
        y = geo.bottom() - self.HEIGHT - 24  # 画面下から少し浮かせる（Mac 版と同じ）
        self.move(x, y)

    # ------------------------------------------------------------------
    # 描画
    # ------------------------------------------------------------------

    @staticmethod
    def _hud_font() -> QFont:
        """HUD のテキスト描画に使う共通フォントを返す。"""
        font = QFont()
        font.setPointSize(12)
        font.setWeight(QFont.Weight.Medium)
        return font

    def _bars_width(self) -> float:
        """波形バー群の合計幅を返す。"""
        return self.BAR_COUNT * self.BAR_WIDTH + (self.BAR_COUNT - 1) * self.BAR_GAP

    def _content_width(self, metrics: QFontMetrics) -> float:
        """現在のモードで描く内容の幅を計算する（ピルを内容に合わせて縮めるため）。"""
        if self._mode in ("recording", "recording_auto_enter"):
            if self._caption:
                middle = min(
                    float(metrics.horizontalAdvance(self._caption)),
                    float(self.CAPTION_MAX_WIDTH),
                )
            else:
                middle = self._bars_width()
            width = 8 + self.CONTENT_GAP + middle  # 状態ドット + 間隔 + 中身
            if self._mode == "recording_auto_enter":
                width += self.CONTENT_GAP + metrics.horizontalAdvance("⏎")
            return width
        if self._mode == "transcribing":
            return (
                self.SPINNER_RADIUS * 2 + self.CONTENT_GAP
                + metrics.horizontalAdvance("変換中…")
            )
        if self._mode == "notice":
            return min(
                float(metrics.horizontalAdvance(self._notice_text)),
                float(self.WIDTH - 2 * self.PADDING_X - 2),
            )
        return 0.0

    def paintEvent(self, event) -> None:
        """ピル背景と状態別コンテンツを描画する。"""
        if self._mode == "hidden":
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setFont(self._hud_font())
        metrics = painter.fontMetrics()

        # ピル背景。Mac 版カプセルと同様に内容の幅へ縮める
        pill_w = min(self.width() - 2.0, self._content_width(metrics) + 2 * self.PADDING_X)
        pill_rect = QRectF(
            (self.width() - pill_w) / 2,
            (self.height() - self.PILL_HEIGHT) / 2,
            pill_w,
            self.PILL_HEIGHT,
        )
        radius = pill_rect.height() / 2

        # 影（QGraphicsDropShadowEffect は透過ウィンドウで効かないため自前で描く。
        # 数枚の半透明レイヤーを下方向にずらして柔らかい落ち影にする）
        for i in range(1, 4):
            shadow_rect = pill_rect.adjusted(-i, -i + 2, i, i + 2)
            shadow_path = QPainterPath()
            shadow_path.addRoundedRect(
                shadow_rect, radius + i, radius + i
            )
            painter.fillPath(shadow_path, QColor(0, 0, 0, 26 - i * 7))

        # ピル本体: 縦グラデーションの半透明ダーク（Mac 版 .ultraThinMaterial 風）
        path = QPainterPath()
        path.addRoundedRect(pill_rect, radius, radius)
        gradient = QLinearGradient(pill_rect.topLeft(), pill_rect.bottomLeft())
        gradient.setColorAt(0.0, QColor(58, 58, 60, 228))
        gradient.setColorAt(1.0, QColor(24, 24, 26, 240))
        painter.fillPath(path, gradient)
        # 白 10% のボーダー（暗い背景上でも輪郭が出る、Mac 版と同じ値）
        painter.setPen(QPen(QColor(255, 255, 255, 26), 1))
        painter.drawPath(path)

        content_left = pill_rect.left() + self.PADDING_X
        if self._mode in ("recording", "recording_auto_enter"):
            self._paint_recording(painter, metrics, content_left)
        elif self._mode == "transcribing":
            self._paint_transcribing(painter, content_left)
        elif self._mode == "notice":
            self._paint_notice(painter, metrics, pill_rect)

        painter.end()

    def _paint_recording(self, painter: QPainter, metrics: QFontMetrics, x: float) -> None:
        """録音中: 状態ドット + （字幕 or 波形バー） + 自動 Enter 時は ⏎ バッジ。"""
        auto_enter = self._mode == "recording_auto_enter"
        # トレイアイコンと同じ macOS システムカラー（systemPurple / systemRed）
        dot_color = QColor("#BF5AF2") if auto_enter else QColor("#FF453A")

        # 左の状態ドット
        cy = self.height() / 2
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(dot_color)
        painter.drawEllipse(QRectF(x, cy - 4, 8, 8))
        x += 8 + self.CONTENT_GAP

        if self._caption:
            # ライブ字幕（末尾を残して頭を省略 = 常に最新の語尾が見える）
            avail = min(
                metrics.horizontalAdvance(self._caption), self.CAPTION_MAX_WIDTH
            )
            elided = metrics.elidedText(self._caption, Qt.TextElideMode.ElideLeft, avail)
            painter.setPen(QColor(245, 245, 247))
            painter.drawText(
                QRectF(x, 0, avail, self.height()),
                int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
                elided,
            )
            x += avail
        else:
            self._paint_bars(painter, x)
            x += self._bars_width()

        # 自動 Enter のバッジ（Mac 版の return アイコン相当）
        if auto_enter:
            x += self.CONTENT_GAP
            painter.setPen(QColor("#BF5AF2"))
            painter.drawText(
                QRectF(x, 0, metrics.horizontalAdvance("⏎") + 2, self.height()),
                int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                "⏎",
            )

    def _paint_bars(self, painter: QPainter, start_x: float) -> None:
        """音声レベル連動の波形バーを描画する。"""
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(200, 200, 205, 200))
        cy = self.height() / 2
        min_h, max_h = 3.0, 22.0
        for i, level in enumerate(self._levels):
            h = min_h + (max_h - min_h) * level
            bx = start_x + i * (self.BAR_WIDTH + self.BAR_GAP)
            painter.drawRoundedRect(QRectF(bx, cy - h / 2, self.BAR_WIDTH, h), 1.5, 1.5)

    def _paint_transcribing(self, painter: QPainter, x: float) -> None:
        """変換中: 回転スピナー + 「変換中…」（Mac 版の ProgressView 相当）。"""
        cy = self.height() / 2
        r = self.SPINNER_RADIUS

        # 270 度の弧を回転させてスピナーを表現する（Qt の角度は 1/16 度単位）
        pen = QPen(QColor(200, 200, 205), 2.2)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawArc(QRectF(x, cy - r, r * 2, r * 2), -self._spin_angle * 16, 270 * 16)

        painter.setPen(QColor(200, 200, 205))
        painter.drawText(
            QRectF(x + r * 2 + self.CONTENT_GAP, 0, self.width(), self.height()),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            "変換中…",
        )

    def _paint_notice(self, painter: QPainter, metrics: QFontMetrics, pill_rect: QRectF) -> None:
        """一時通知をピル中央に描画する。"""
        avail = int(pill_rect.width() - 2 * self.PADDING_X)
        elided = metrics.elidedText(self._notice_text, Qt.TextElideMode.ElideRight, avail)
        painter.setPen(QColor(220, 220, 225))
        painter.drawText(pill_rect, int(Qt.AlignmentFlag.AlignCenter), elided)
