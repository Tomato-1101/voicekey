"""
ライブ字幕の表示（Mac 版 CaptionHUDController.swift の Windows 版・PySide6）

- **位置はピル固定**: 録音ピルのカプセル下端に字幕の下端を揃え、行が増えたら**上へ伸びる**（ピルが字幕へ育つ見え方）。
  ドラッグ移動・位置記憶は持たせない。大きさだけ 70〜200% で変えられる（幅とフォントを同時に拡大縮小）。
- **クリックはすべて下のアプリへ通す**（入力透過・フォーカスを奪わない）。
- ロール表示（確定 2 行＋ライブ行）・最低表示時間 `max(3 秒, 文字数×0.15 秒)`・8 秒何も来なければ畳む。
  行の管理は純ロジックの `line_model.CaptionLineModel` が持ち、ここは描くだけ。
- 透明度は字幕 0.62（ピルの 0.7 とは別のまま）。
- 音声入力中（録音・変換中・通知）は隠す（`set_suppressed`）。認識・翻訳は止めない。
- 表示/非表示が変わったら `visibility_changed` を出す（出ている間は待機ピルを引っ込めてもらう。
  ピルを戻すのは畳みアニメの完了後＝透けて別物に見えるのを防ぐ）。
"""

import time
from typing import List, Optional, Tuple

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QGuiApplication, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget

from . import line_model as lm
from .settings import clamp_scale

# 画面幅に対する字幕の幅・上限（Mac と同じ）
WIDTH_RATIO = 0.62
MAXIMUM_WIDTH = 880
HORIZONTAL_PADDING = 14
VERTICAL_PADDING = 8
LINE_SPACING = 4
MAXIMUM_CORNER_RADIUS = 22
# ガラスの不透明度（ピル 0.7 とは別）
GLASS_ALPHA = 0.62
FADE_MSEC = 220
TICK_MSEC = 200


def preferred_width(screen_width: float, scale: float) -> int:
    """倍率から字幕の幅を決める（固定幅。更新のたびに横へ揺れないように）。"""
    base = min(screen_width * WIDTH_RATIO, MAXIMUM_WIDTH)
    return int(min(base * scale, screen_width - 40))


def opacity_for(distance_from_bottom: int) -> float:
    """下の行ほど濃く、古い行ほど薄く。"""
    return {0: 1.0, 1: 0.82, 2: 0.64}.get(distance_from_bottom, 0.5)


class CaptionHud(QWidget):
    """字幕のフローティング表示（フレームレス・入力透過・非アクティブ）。メインスレッドからだけ触る。

    Signals:
        visibility_changed: 字幕が出た（True）/ 畳み終わった（False）
    """

    visibility_changed = Signal(bool)

    def __init__(self, scale: float = 1.0, shows_source: bool = True) -> None:
        super().__init__()
        self.model = lm.CaptionLineModel()
        self._scale = clamp_scale(scale)
        self.shows_source = shows_source
        # 文字起こしモードではライブ行も確定行と同じ見た目にする（訳を待たないので本文そのもの）
        self.live_looks_confirmed = False
        self._suppressed = False
        self._shown = False
        self._layout: List[Tuple[lm.Line, QRectF, str, str, float, float]] = []
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowDoesNotAcceptFocus
            | Qt.WindowType.WindowTransparentForInput
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._fade = QPropertyAnimation(self, b"windowOpacity", self)
        self._fade.setDuration(FADE_MSEC)
        self._fade.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._fade.finished.connect(self._on_fade_finished)
        self._fading_out = False
        self._timer = QTimer(self)
        self._timer.setInterval(TICK_MSEC)
        self._timer.timeout.connect(self._on_tick)

    # --- 表示更新（メインスレッド） ---

    def show_listening(self, text: str) -> None:
        if self.model.show_listening(text, time.monotonic()):
            self._refresh()

    def show_pending_source(self, text: str) -> None:
        if self.model.show_pending_source(text, time.monotonic()):
            self._refresh()

    def show_live_transcript(self, text: str) -> None:
        if self.model.show_live_transcript(text, time.monotonic()):
            self._refresh()

    def show_partial(self, source: str, japanese: str) -> None:
        if self.model.show_partial(source, japanese, time.monotonic()):
            self._refresh()

    def show_translation(self, source: str, japanese: str) -> None:
        self.model.show_translation(source, japanese, time.monotonic())
        self._refresh()

    def show_transcript(self, text: str) -> None:
        self.model.show_transcript(text, time.monotonic())
        self._refresh()

    def show_source_only(self, text: str) -> None:
        self.model.show_source_only(text, time.monotonic())
        self._refresh()

    def show_notice(self, text: str) -> None:
        self.model.show_notice(text, time.monotonic())
        self._refresh()

    def clear(self) -> None:
        self.model.clear()
        self._fade_out()

    def set_scale(self, scale: float) -> None:
        self._scale = clamp_scale(scale)
        if self._shown:
            self._relayout()

    @property
    def scale(self) -> float:
        return self._scale

    def set_shows_source(self, shows: bool) -> None:
        self.shows_source = bool(shows)
        if self._shown:
            self._relayout()

    def set_suppressed(self, suppressed: bool) -> None:
        """音声入力中は隠す（行は残す。戻ったときに残っていれば出し直す）。"""
        suppressed = bool(suppressed)
        if suppressed == self._suppressed:
            return
        self._suppressed = suppressed
        if suppressed:
            self._fade_out()
        elif not self.model.is_empty:
            self._refresh()

    @property
    def is_on_screen(self) -> bool:
        return self._shown

    # --- 内部 ---

    def _refresh(self) -> None:
        if self._suppressed:
            return
        self._relayout()
        self._fade_in()
        if not self._timer.isActive():
            self._timer.start()

    def _on_tick(self) -> None:
        if self.model.tick(time.monotonic()):
            if self.model.is_empty:
                self._fade_out()
                return
            if self._shown and not self._suppressed:
                self._relayout()
        if self.model.is_empty and not self._shown:
            self._timer.stop()

    def _fade_in(self) -> None:
        if self._shown and not self._fading_out:
            self.raise_()
            return
        self._fade.stop()
        self._fading_out = False
        if not self._shown:
            self._shown = True
            # 伸び始めと同時にピルを引っ込めてもらう（育ちきってからだと半透明越しにピルが透ける）
            self.visibility_changed.emit(True)
            self.setWindowOpacity(0.0)
            self.show()
        self.raise_()
        self._fade.setStartValue(self.windowOpacity())
        self._fade.setEndValue(1.0)
        self._fade.start()

    def _fade_out(self) -> None:
        if not self._shown:
            # 既に非表示でも false を流す（流さないとピル側と状態が食い違い、待機ピルが戻らなくなる）
            self.visibility_changed.emit(False)
            return
        if self._fading_out:
            return
        self._fading_out = True
        self._fade.stop()
        self._fade.setStartValue(self.windowOpacity())
        self._fade.setEndValue(0.0)
        self._fade.start()

    def _on_fade_finished(self) -> None:
        if not self._fading_out:
            return
        self._fading_out = False
        self._shown = False
        self.hide()
        # ピルを戻すのは畳みアニメの完了後（途中で戻すと透けて別物に見える）
        self.visibility_changed.emit(False)

    def _fonts(self, line: lm.Line) -> Tuple[QFont, QFont]:
        if line.kind == lm.KIND_LIVE:
            size = 26 if line.japanese else 20
        elif line.kind == lm.KIND_CONFIRMED:
            size = 26
        else:
            size = 22
        main = QFont()
        main.setPointSizeF(max(8.0, size * 0.75 * self._scale))
        main.setWeight(QFont.Weight.DemiBold)
        sub = QFont()
        sub.setPointSizeF(max(7.0, 13 * 0.75 * self._scale))
        return main, sub

    def _texts(self, line: lm.Line) -> Tuple[str, str]:
        """主役の文と、下に添える原文（出さないなら空）。"""
        main = line.japanese or line.source
        secondary = ""
        if self.shows_source and line.japanese and line.source and line.kind != lm.KIND_NOTICE:
            secondary = line.source
        return main, secondary

    def _relayout(self) -> None:
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            return
        geo = screen.availableGeometry()
        width = preferred_width(geo.width(), self._scale)
        content_w = width - HORIZONTAL_PADDING * 2
        lines = self.model.visible_lines
        layout = []
        y = float(VERTICAL_PADDING)
        flags = int(Qt.TextFlag.TextWordWrap)
        for index, line in enumerate(lines):
            main_font, sub_font = self._fonts(line)
            main_text, secondary = self._texts(line)
            main_metrics = QFontMetrics(main_font)
            if line.kind == lm.KIND_LIVE:
                # ライブ行は常に 1 行ぶんの固定の高さ（途中経過が伸びても高さが揺れない）。新しい語が見えるよう頭を省く
                main_text = main_metrics.elidedText(main_text, Qt.TextElideMode.ElideLeft, content_w)
                main_h = float(main_metrics.height())
                secondary = ""
            else:
                main_h = float(main_metrics.boundingRect(0, 0, content_w, 10000, flags, main_text).height())
            sub_h = 0.0
            if secondary:
                sub_h = float(QFontMetrics(sub_font).boundingRect(0, 0, content_w, 10000, flags, secondary).height())
            height = main_h + (sub_h + 2 if secondary else 0)
            opacity = opacity_for(len(lines) - 1 - index)
            layout.append((line, QRectF(HORIZONTAL_PADDING, y, content_w, height), main_text, secondary,
                           main_h, opacity))
            y += height + LINE_SPACING
        total_h = int(y - (LINE_SPACING if lines else 0) + VERTICAL_PADDING)
        total_h = max(total_h, 28)
        self._layout = layout
        from ..ui.hud import Hud

        bottom = Hud.capsule_bottom_y(geo)
        x = geo.center().x() - width // 2
        self.setGeometry(int(x), int(bottom - total_h), int(width), total_h)
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(0.5, 0.5, self.width() - 1.0, self.height() - 1.0)
        # 1 行のときは角丸＝高さの半分でピルと同じカプセル形
        radius = min(rect.height() / 2, MAXIMUM_CORNER_RADIUS)
        path = QPainterPath()
        path.addRoundedRect(rect, radius, radius)
        painter.fillPath(path, QColor(30, 29, 26, int(255 * GLASS_ALPHA)))
        painter.setPen(QPen(QColor(255, 255, 255, 26), 1))
        painter.drawPath(path)
        flags = int(Qt.TextFlag.TextWordWrap | Qt.AlignmentFlag.AlignHCenter)
        for line, box, main_text, secondary, main_h, opacity in self._layout:
            main_font, sub_font = self._fonts(line)
            is_live = line.kind == lm.KIND_LIVE
            if line.kind in (lm.KIND_SOURCE_ONLY, lm.KIND_NOTICE):
                color = QColor(225, 222, 214)
            elif is_live and not self.live_looks_confirmed:
                color = QColor(255, 255, 255, 190)
            else:
                color = QColor(255, 255, 255)
            color.setAlphaF(color.alphaF() * opacity)
            painter.setFont(main_font)
            painter.setPen(color)
            painter.drawText(QRectF(box.x(), box.y(), box.width(), main_h), flags, main_text)
            if secondary:
                sub_color = QColor(200, 196, 188)
                sub_color.setAlphaF(0.85 * opacity)
                painter.setFont(sub_font)
                painter.setPen(sub_color)
                painter.drawText(QRectF(box.x(), box.y() + main_h + 2, box.width(), box.height() - main_h - 2),
                                 flags, secondary)
        painter.end()
