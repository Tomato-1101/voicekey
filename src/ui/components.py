"""
UI 部品モジュール

設定ウィンドウを組み立てる部品（浮いた島・ナビ・設定行・トグル・ボタン・ピッカー・
メッセージ欄・ページ切替）を提供する。見た目は Mac 版（Glass.swift / SettingsView.swift）
と同じにし、色と寸法はすべて theme.py から取る。

島・トグル・ボタン・ナビは QSS ではなく QPainter で描く。グラデーションの縁（ガラスの厚み）と
滑らかな動きは QSS では出せないため。
"""

from typing import List, Optional

from PySide6.QtCore import (
    QEasingCurve,
    QEvent,
    QPoint,
    QPointF,
    QRectF,
    QSize,
    Qt,
    QVariantAnimation,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
)
from PySide6.QtWidgets import (
    QAbstractButton,
    QComboBox,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from . import icons, theme
from .theme import BONE, CARBON, SIGNAL, Metrics, Motion, Theme, rgba


# ---------------------------------------------------------------------------
# 共通の小道具
# ---------------------------------------------------------------------------


def _lerp(a: float, b: float, t: float) -> float:
    """a と b の間を t（0.0〜1.0）で補間する。"""
    return a + (b - a) * t


def _mix(a: QColor, b: QColor, t: float) -> QColor:
    """2 つの色を t（0.0〜1.0）で混ぜる（不透明度も補間する）。"""
    return QColor(
        round(_lerp(a.red(), b.red(), t)),
        round(_lerp(a.green(), b.green(), t)),
        round(_lerp(a.blue(), b.blue(), t)),
        round(_lerp(a.alpha(), b.alpha(), t)),
    )


def _animation(owner: QWidget, duration: int, on_value) -> QVariantAnimation:
    """
    0.0〜1.0 を滑らかに動かすアニメーションを作る。

    Args:
        owner: 持ち主（破棄されたら一緒に消える）
        duration: かける時間（ミリ秒）
        on_value: 値が変わるたびに呼ぶ関数（引数は float）

    Returns:
        未開始の QVariantAnimation
    """
    anim = QVariantAnimation(owner)
    anim.setDuration(duration)
    anim.setEasingCurve(QEasingCurve.Type.OutCubic)
    anim.valueChanged.connect(lambda v: on_value(float(v)))
    return anim


def _run(anim: QVariantAnimation, start: float, end: float) -> None:
    """アニメーションを start から end へ走らせる（走っている途中なら止めてやり直す）。"""
    anim.stop()
    anim.setStartValue(start)
    anim.setEndValue(end)
    anim.start()


def _rim_pen(top: QColor, bottom: QColor, y0: float, y1: float) -> QPen:
    """上が明るく下が暗い 1px の縁（ガラスの厚み）を描くペンを作る。"""
    gradient = QLinearGradient(0, y0, 0, y1)
    gradient.setColorAt(0.0, top)
    gradient.setColorAt(1.0, bottom)
    return QPen(gradient, 1.0)


def _accent_gradient(y0: float, y1: float) -> QLinearGradient:
    """灯りの面のグラデーション（上が濃く下が少し薄い。Glass.swift の prominent と同じ）。"""
    gradient = QLinearGradient(0, y0, 0, y1)
    gradient.setColorAt(0.0, rgba(SIGNAL))
    gradient.setColorAt(1.0, rgba(SIGNAL, 0.78))
    return gradient


# ---------------------------------------------------------------------------
# 文字
# ---------------------------------------------------------------------------


class Text(QLabel):
    """
    役割（本文・補助・灯り・注意など）で色が決まる文字。外観が変わると色も追従する。

    Attributes:
        role: 色の役割（"primary" / "secondary" / "disabled" / "accent" / "danger" / "warn" / "ok"）
    """

    def __init__(
        self,
        text: str = "",
        size: int = 13,
        weight: QFont.Weight = QFont.Weight.Normal,
        role: str = "primary",
        wrap: bool = False,
        parent: Optional[QWidget] = None,
    ) -> None:
        """
        Args:
            text: 表示する文字
            size: 文字の大きさ（ピクセル）
            weight: 太さ
            role: 色の役割
            wrap: 折り返すなら True
            parent: 親ウィジェット
        """
        super().__init__(text, parent)
        self.role = role
        self._size = size
        self.setFont(theme.font(size, weight))
        self.setWordWrap(wrap)
        self._refresh()
        Theme.instance().changed.connect(self._refresh)

    def set_role(self, role: str) -> None:
        """色の役割を切り替える。"""
        self.role = role
        self._refresh()

    def _refresh(self) -> None:
        """いまの外観と役割に合わせて色を付け直す。"""
        p = theme.palette()
        color = {
            "primary": p.text,
            "secondary": p.text_secondary,
            "disabled": p.text_disabled,
            "accent": p.accent_text,
            "danger": p.danger,
            "warn": p.warn,
            "ok": p.ok,
        }.get(self.role, p.text)
        # 大きさもここで指定する。アプリ全体の QSS（QWidget の font-size）が setFont より優先されるため
        self.setStyleSheet(
            f"color: rgba({color.red()}, {color.green()}, {color.blue()}, {color.alpha()});"
            f" background: transparent; font-size: {self._size}px;"
        )


class MessageLabel(Text):
    """
    結果・エラーを出す欄。空のときも文が長いときも同じ高さを保つ。

    結果が出るたびに下の部品がずれるのを防ぐため、高さは行数で固定し、
    収まらない文は末尾を省略する（全文はツールチップで読める）。
    """

    def __init__(self, lines: int = 1, size: int = 11, parent: Optional[QWidget] = None) -> None:
        """
        Args:
            lines: 確保する行数
            size: 文字の大きさ（ピクセル）
            parent: 親ウィジェット
        """
        super().__init__("", size=size, role="secondary", wrap=lines > 1, parent=parent)
        self._lines = lines
        self._full_text = ""
        metrics = QFontMetrics(self.font())
        self.setFixedHeight(metrics.lineSpacing() * lines + 2)
        self.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)

    def show_message(self, text: str, kind: str = "secondary") -> None:
        """
        文を出す。

        Args:
            text: 出す文（空なら欄を空にする。高さは変わらない）
            kind: 色の役割（"secondary" / "ok" / "warn" / "danger" / "accent"）
        """
        self._full_text = text
        self.set_role(kind)
        self.setToolTip(text)
        self._elide()

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt の命名)
        """幅が変わったら省略位置を決め直す。"""
        super().resizeEvent(event)
        self._elide()

    def _elide(self) -> None:
        """欄に収まるように文の末尾を省略して表示する。"""
        metrics = QFontMetrics(self.font())
        # 複数行のときは「幅 × 行数」より少し短く見積もる（折り返しで生じる余りの分）
        budget = max(0, self.width()) * self._lines - (self._lines - 1) * metrics.averageCharWidth() * 4
        self.setText(metrics.elidedText(self._full_text, Qt.TextElideMode.ElideRight, max(0, budget)))


# ---------------------------------------------------------------------------
# 窓の下地と浮いた島
# ---------------------------------------------------------------------------


class Backdrop(QWidget):
    """
    窓の下地。地色と、左上から差す灯りの気配（ごく薄いグラデーション）を描き、
    その上に浮く島の影もここで描く。

    影を島自身ではなく下地が描くのは、影が島の外側へはみ出すため
    （島の中で描くと島の四角で切れてしまう）。
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._islands: List[QWidget] = []
        # 背景のぼかし（Windows 11 の Mica など）が効いているとき True。地色を透かす
        self._blurred = False
        Theme.instance().changed.connect(self.update)

    def set_blurred(self, blurred: bool) -> None:
        """背景のぼかしが効いているかを伝える（効いていれば地色を半透明にする）。"""
        self._blurred = blurred
        self.update()

    def add_island(self, island: QWidget) -> None:
        """影を落とす島を登録する。"""
        self._islands.append(island)
        island.installEventFilter(self)

    def eventFilter(self, watched, event) -> bool:  # noqa: N802
        """島が動いた・大きさが変わった・出入りしたら影を描き直す。"""
        if event.type() in (QEvent.Type.Move, QEvent.Type.Resize, QEvent.Type.Show, QEvent.Type.Hide):
            self.update()
        return False

    def paintEvent(self, _event) -> None:  # noqa: N802
        """地色 → 灯りの気配 → 島の影、の順に描く。"""
        p = theme.palette()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect())
        painter.fillRect(rect, p.window_tint if self._blurred else p.window)
        # 左上（灯り）→ 中ほど（地の色）→ 右下、の 3 点。Glass.swift の windowBackdrop と同じ
        wash = QLinearGradient(rect.topLeft(), rect.bottomRight())
        wash.setColorAt(0.0, p.wash_top)
        wash.setColorAt(0.5, rgba(CARBON if p.dark else BONE, 0.30))
        wash.setColorAt(1.0, p.wash_bottom)
        painter.fillRect(rect, wash)
        for island in self._islands:
            if not island.isVisible():
                continue
            origin = island.mapTo(self, QPoint(0, 0))
            theme.paint_shadow(
                painter,
                QRectF(origin.x(), origin.y(), island.width(), island.height()),
                alpha=p.shadow_alpha,
            )


class GlassIsland(QWidget):
    """
    浮いたガラスの島（サイドバー・本文の面）。角丸 18 の面と、上が明るい縁を描く。
    影は Backdrop.add_island で下地に描いてもらう。
    """

    def __init__(self, parent: Optional[QWidget] = None, radius: int = Metrics.ISLAND_RADIUS) -> None:
        """
        Args:
            parent: 親ウィジェット
            radius: 角丸の半径
        """
        super().__init__(parent)
        self._radius = radius
        Theme.instance().changed.connect(self.update)

    def paintEvent(self, _event) -> None:  # noqa: N802
        """面と縁を描く。"""
        p = theme.palette()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(rect, self._radius, self._radius)
        painter.fillPath(path, p.island)
        painter.setPen(_rim_pen(p.island_rim_top, p.island_rim_bottom, rect.top(), rect.bottom()))
        painter.drawPath(path)


# ---------------------------------------------------------------------------
# トグル・ボタン・ピッカー
# ---------------------------------------------------------------------------


class Switch(QAbstractButton):
    """
    トグルスイッチ。ON のとき溝が灯りの色になり、つまみが滑って移る。
    """

    def __init__(self, checked: bool = False, parent: Optional[QWidget] = None) -> None:
        """
        Args:
            checked: 最初の状態
            parent: 親ウィジェット
        """
        super().__init__(parent)
        self.setCheckable(True)
        self.setChecked(checked)
        self.setFixedSize(Metrics.SWITCH_W, Metrics.SWITCH_H)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._t = 1.0 if checked else 0.0
        self._anim = _animation(self, Motion.TOGGLE_MS, self._set_t)
        self.toggled.connect(self._on_toggled)
        Theme.instance().changed.connect(self.update)

    def _set_t(self, t: float) -> None:
        """つまみの位置（0.0 = OFF、1.0 = ON）を更新して描き直す。"""
        self._t = t
        self.update()

    def _on_toggled(self, checked: bool) -> None:
        """状態が変わったら、いまの位置から目標へ滑らせる。"""
        target = 1.0 if checked else 0.0
        if not self.isVisible():
            # 見えていないとき（画面を組み立てている最中）は動かさずに合わせる
            self._anim.stop()
            self._set_t(target)
            return
        _run(self._anim, self._t, target)

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(Metrics.SWITCH_W, Metrics.SWITCH_H)

    def paintEvent(self, _event) -> None:  # noqa: N802
        """溝とつまみを描く。"""
        p = theme.palette()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self.isEnabled():
            painter.setOpacity(0.5)
        w, h = float(self.width()), float(self.height())
        track = QPainterPath()
        track.addRoundedRect(QRectF(0, 0, w, h), h / 2, h / 2)
        painter.fillPath(track, _mix(p.track_off, p.accent, self._t))
        if self.hasFocus() and self.focusReason_is_keyboard():
            painter.setPen(QPen(rgba(SIGNAL, 0.55), 2.0))
            painter.drawPath(track)
        knob = h - 4.0
        x = 2.0 + self._t * (w - knob - 4.0)
        # つまみの下にごく薄い影（浮きを出す）
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(0, 0, 0, 40))
        painter.drawEllipse(QRectF(x, 3.0, knob, knob))
        painter.setBrush(QColor(255, 255, 255))
        painter.drawEllipse(QRectF(x, 2.0, knob, knob))

    def focusInEvent(self, event) -> None:  # noqa: N802
        """キーボードで移ってきたときだけ枠を出すため、理由を覚えておく。"""
        self._focus_by_keyboard = event.reason() in (
            Qt.FocusReason.TabFocusReason,
            Qt.FocusReason.BacktabFocusReason,
            Qt.FocusReason.ShortcutFocusReason,
        )
        super().focusInEvent(event)

    def focusReason_is_keyboard(self) -> bool:  # noqa: N802
        """いまのフォーカスがキーボード操作で来たものなら True。"""
        return getattr(self, "_focus_by_keyboard", False)


class CapsuleButton(QAbstractButton):
    """
    カプセル形のボタン（Glass.swift の LiquidButtonStyle と同じ見た目）。

    kind:
        "primary": 灯りの面に白い文字（その画面でいちばん大事な操作に 1 つだけ）
        "secondary": 薄いガラスの面（既定）
        "destructive": 薄いガラスの面に赤い文字（消す・やめる）
        "link": 面を持たない灯りの色の文字（カードの中の軽い操作。Mac の borderless ボタン）
    size:
        "small" / "regular" / "large"
    """

    # (文字の大きさ, 左右の余白, 上下の余白)
    _SIZES = {
        "small": (11, 8, 3),
        "regular": (13, 12, 5),
        "large": (13, 14, 7),
    }

    def __init__(
        self,
        text: str = "",
        kind: str = "secondary",
        size: str = "regular",
        parent: Optional[QWidget] = None,
        icon: str = "",
    ) -> None:
        """
        Args:
            text: ボタンの文字
            kind: 種類（"primary" / "secondary" / "destructive" / "link"）
            size: 大きさ（"small" / "regular" / "large"）
            parent: 親ウィジェット
            icon: アイコンの名前（icons.names() のどれか）。入れると文字の代わりにアイコンだけの丸いボタンになる
        """
        super().__init__(parent)
        self._kind = kind
        self._icon = icon
        font_size, self._pad_h, self._pad_v = self._SIZES.get(size, self._SIZES["regular"])
        weight = QFont.Weight.DemiBold if kind in ("primary", "link") else QFont.Weight.Normal
        if kind == "link":
            # Mac の該当ボタンは 12pt・semibold で固定。面が無いので、文字の端を周りの端に揃える
            font_size = 12
            self._pad_h = 0
        self.setFont(theme.font(font_size, weight))
        self.setText(text)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self._press = 0.0
        self._hover = 0.0
        self._press_anim = _animation(self, Motion.PRESS_MS, self._set_press)
        self._hover_anim = _animation(self, Motion.HOVER_MS, self._set_hover)
        self.pressed.connect(lambda: _run(self._press_anim, self._press, 1.0))
        self.released.connect(lambda: _run(self._press_anim, self._press, 0.0))
        Theme.instance().changed.connect(self.update)

    def set_kind(self, kind: str) -> None:
        """種類を切り替える。"""
        self._kind = kind
        self.update()

    def _set_press(self, value: float) -> None:
        self._press = value
        self.update()

    def _set_hover(self, value: float) -> None:
        self._hover = value
        self.update()

    def enterEvent(self, event) -> None:  # noqa: N802
        _run(self._hover_anim, self._hover, 1.0)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        _run(self._hover_anim, self._hover, 0.0)
        super().leaveEvent(event)

    def sizeHint(self) -> QSize:  # noqa: N802
        metrics = QFontMetrics(self.font())
        height = metrics.height() + self._pad_v * 2
        if self._icon:
            # アイコンだけのときは、文字のボタンと同じ高さの正円にする
            return QSize(height, height)
        return QSize(metrics.horizontalAdvance(self.text()) + self._pad_h * 2 + 2, height)

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return self.sizeHint()

    def paintEvent(self, _event) -> None:  # noqa: N802
        """面・縁・文字を描く。押している間は少し沈み、薄くなる。"""
        p = theme.palette()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        # 押下: 不透明度 0.7・大きさ 0.98（LiquidButtonStyle と同じ）。無効: 0.5
        opacity = 1.0 - 0.3 * self._press
        if not self.isEnabled():
            opacity = 0.5
        painter.setOpacity(opacity)
        scale = 1.0 - 0.02 * self._press
        center = QPointF(self.width() / 2.0, self.height() / 2.0)
        painter.translate(center)
        painter.scale(scale, scale)
        painter.translate(-center)

        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = rect.height() / 2.0
        path = QPainterPath()
        path.addRoundedRect(rect, radius, radius)

        if self._kind == "primary":
            painter.fillPath(path, _accent_gradient(rect.top(), rect.bottom()))
            # 載せたとき、白をほんの少し重ねて明るくする
            painter.fillPath(path, rgba("#FFFFFF", 0.10 * self._hover))
            painter.setPen(_rim_pen(rgba("#FFFFFF", 0.55), rgba("#FFFFFF", 0.0), rect.top(), rect.bottom()))
            painter.drawPath(path)
            text_color = p.on_accent
        elif self._kind == "link":
            # 面も縁も描かない（押している間の沈みと薄さだけで手応えを返す）
            text_color = p.accent
        else:
            painter.fillPath(path, _mix(p.control, p.control_hover, self._hover))
            if not p.dark:
                # ライトでは白い面が地に溶けるので、ごく薄い輪郭で形を保つ
                painter.setPen(QPen(p.control_border, 1.0))
                painter.drawPath(path)
            painter.setPen(_rim_pen(rgba("#FFFFFF", 0.45), rgba("#FFFFFF", 0.05), rect.top(), rect.bottom()))
            painter.drawPath(path)
            text_color = p.danger if self._kind == "destructive" else p.text

        if self.hasFocus() and getattr(self, "_focus_by_keyboard", False):
            painter.setPen(QPen(rgba(SIGNAL, 0.55), 2.0))
            painter.drawPath(path)

        if self._icon:
            side = 13.0
            icons.paint(
                painter,
                self._icon,
                QRectF((self.width() - side) / 2.0, (self.height() - side) / 2.0, side, side),
                text_color,
            )
            return
        painter.setPen(text_color)
        painter.setFont(self.font())
        painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.text())

    def focusInEvent(self, event) -> None:  # noqa: N802
        """キーボードで移ってきたときだけ枠を出すため、理由を覚えておく。"""
        self._focus_by_keyboard = event.reason() in (
            Qt.FocusReason.TabFocusReason,
            Qt.FocusReason.BacktabFocusReason,
            Qt.FocusReason.ShortcutFocusReason,
        )
        super().focusInEvent(event)


class Picker(QComboBox):
    """
    選択肢から 1 つ選ぶ部品（Mac のメニュー型ピッカーと同じ見た目）。

    枠を持たず、選んでいる文字を右寄せで出し、右端に上下の山形の小さな札を置く。
    部品の幅は「いちばん長い選択肢」のままなので、選び直しても右端も周りも動かない。
    開いたときの一覧の見た目は theme.app_qss が決める。

    ページをスクロールしている途中でホイールが乗っても値が変わらないようにしてある
    （フォーカスが無いときのホイールは親へ流す）。
    """

    _CHIP_W = 16.0
    _CHIP_H = 20.0
    _CHIP_GAP = 6.0

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(Metrics.FIELD_H)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        Theme.instance().changed.connect(self.update)

    def wheelEvent(self, event) -> None:  # noqa: N802
        """フォーカスが無いときは値を変えず、ページのスクロールに任せる。"""
        if self.hasFocus():
            super().wheelEvent(event)
        else:
            event.ignore()

    def paintEvent(self, _event) -> None:  # noqa: N802
        """選んでいる文字（右寄せ）と、上下の山形の札を描く。枠は描かない。"""
        p = theme.palette()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        enabled = self.isEnabled()

        chip = QRectF(
            self.width() - self._CHIP_W - 0.5,
            (self.height() - self._CHIP_H) / 2.0,
            self._CHIP_W,
            self._CHIP_H,
        )
        face = QColor(p.text)
        face.setAlphaF(0.14 if (enabled and self.underMouse()) else 0.09)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(face)
        painter.drawRoundedRect(chip, 5.0, 5.0)
        if self.hasFocus() and enabled:
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(rgba(SIGNAL, 0.55), 1.5))
            painter.drawRoundedRect(chip, 5.0, 5.0)

        mark = p.text if enabled else p.text_disabled
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(mark, 1.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        cx = chip.center().x()
        cy = chip.center().y()
        for direction in (-1.0, 1.0):
            tip = cy + direction * 5.2
            base = cy + direction * 2.2
            path = QPainterPath()
            path.moveTo(cx - 3.0, base)
            path.lineTo(cx, tip)
            path.lineTo(cx + 3.0, base)
            painter.drawPath(path)

        painter.setPen(p.text if enabled else p.text_disabled)
        painter.setFont(self.font())
        text_rect = QRectF(0.0, 0.0, chip.left() - self._CHIP_GAP, float(self.height()))
        text = QFontMetrics(self.font()).elidedText(
            self.currentText(), Qt.TextElideMode.ElideRight, int(text_rect.width())
        )
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, text)


# ---------------------------------------------------------------------------
# 設定行と、行をまとめる棚
# ---------------------------------------------------------------------------


class SettingRow(QWidget):
    """
    設定の 1 行。左に見出しと説明、右に操作部品（トグル・ピッカー・ボタン等）。

    右がトグルのときは、行のどこを押しても切り替わる（Glass.swift の RowSwitchToggleStyle と同じ）。
    """

    def __init__(
        self,
        title: str,
        caption: str = "",
        trailing: Optional[QWidget] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        """
        Args:
            title: 見出し
            caption: 説明（空なら出さない）
            trailing: 右に置く操作部品
            parent: 親ウィジェット
        """
        super().__init__(parent)
        self._trailing = trailing
        layout = QHBoxLayout(self)
        layout.setContentsMargins(
            Metrics.ROW_PADDING_H, Metrics.ROW_PADDING_V, Metrics.ROW_PADDING_H, Metrics.ROW_PADDING_V
        )
        layout.setSpacing(12)

        labels = QVBoxLayout()
        labels.setContentsMargins(0, 0, 0, 0)
        labels.setSpacing(3)
        self.title_label = Text(title, size=13)
        labels.addWidget(self.title_label)
        self.caption_label = Text(caption, size=11, role="secondary", wrap=True)
        self.caption_label.setVisible(bool(caption))
        labels.addWidget(self.caption_label)
        layout.addLayout(labels, 1)

        if trailing is not None:
            layout.addWidget(trailing, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.setMinimumHeight(Metrics.ROW_MIN_H)
        if isinstance(trailing, Switch):
            self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_caption(self, caption: str) -> None:
        """説明を差し替える。"""
        self.caption_label.setText(caption)
        self.caption_label.setVisible(bool(caption))

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        """右がトグルなら、行のどこを押しても切り替える。"""
        if (
            isinstance(self._trailing, Switch)
            and self._trailing.isEnabled()
            and event.button() == Qt.MouseButton.LeftButton
            and self.rect().contains(event.position().toPoint())
        ):
            self._trailing.click()
        super().mouseReleaseEvent(event)


class SettingsGroup(QWidget):
    """
    設定行をまとめる棚（薄いガラスの面）。行の間には細い線を引く。
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)
        Theme.instance().changed.connect(self.update)

    def add_row(self, row: QWidget) -> QWidget:
        """
        行を足す。

        Args:
            row: 足す行（SettingRow でなくてもよい）

        Returns:
            足した行（続けて設定できるように返す）
        """
        self._layout.addWidget(row)
        return row

    def paintEvent(self, _event) -> None:  # noqa: N802
        """棚の面と、行の間の線を描く。"""
        p = theme.palette()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect())
        path = QPainterPath()
        path.addRoundedRect(rect, Metrics.GROUP_RADIUS, Metrics.GROUP_RADIUS)
        painter.fillPath(path, p.row)
        painter.setPen(QPen(p.line, 1.0))
        first = True
        for index in range(self._layout.count()):
            widget = self._layout.itemAt(index).widget()
            if widget is None or not widget.isVisible():
                continue
            if not first:
                y = widget.y() + 0.5
                painter.drawLine(QPointF(Metrics.ROW_PADDING_H, y), QPointF(rect.width() - Metrics.ROW_PADDING_H, y))
            first = False


class Card(QWidget):
    """
    数字や一覧を載せる面（ダッシュボードのカード）。棚より角が丸く、内側に余白を持つ。

    Attributes:
        body: 中身を足していく縦のレイアウト
    """

    RADIUS = 14

    def __init__(self, parent: Optional[QWidget] = None, padding: int = 16) -> None:
        """
        Args:
            parent: 親ウィジェット
            padding: 面の縁から中身までの余白
        """
        super().__init__(parent)
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(padding, padding, padding, padding)
        self.body.setSpacing(6)
        Theme.instance().changed.connect(self.update)

    def paintEvent(self, _event) -> None:  # noqa: N802
        """面を描く。"""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect()), self.RADIUS, self.RADIUS)
        painter.fillPath(path, theme.palette().row)


class Segmented(QWidget):
    """
    横に並んだ選択肢から 1 つを選ぶ部品（Mac の segmented Picker と同じ見た目）。
    選ばれている印（白い板）が滑って移る。

    幅は「いちばん長い選択肢」で決めて固定する。選び直しても周りの部品が動かないようにするため。

    Signals:
        currentChanged(int): 選択が変わったとき（引数は選ばれた番号）
    """

    currentChanged = Signal(int)

    HEIGHT = 28
    _PADDING_H = 14   # 選択肢の文字の左右の余白
    _INSET = 2.0      # 溝の縁から白い板までのすき間

    def __init__(self, options: List[str], current: int = 0, parent: Optional[QWidget] = None) -> None:
        """
        Args:
            options: 選択肢の表示名（左から）
            current: 最初に選んでおく番号
            parent: 親ウィジェット
        """
        super().__init__(parent)
        self._options = list(options)
        self._current = max(0, min(current, len(self._options) - 1))
        self._x = float(self._current)   # 白い板の位置（選択肢 1 つぶんを 1.0 とする）
        self._font = theme.font(12, QFont.Weight.Medium)
        metrics = QFontMetrics(self._font)
        widest = max((metrics.horizontalAdvance(option) for option in self._options), default=0)
        self._segment_w = widest + self._PADDING_H * 2
        self.setFixedSize(self._segment_w * max(1, len(self._options)), self.HEIGHT)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self._anim = _animation(self, Motion.TOGGLE_MS, self._set_x)
        Theme.instance().changed.connect(self.update)

    def current_index(self) -> int:
        """選ばれている番号を返す。"""
        return self._current

    def set_current_index(self, index: int, animate: bool = True, notify: bool = True) -> None:
        """
        選択を変える。

        Args:
            index: 選ぶ番号（範囲外・同じ番号なら何もしない）
            animate: 白い板を滑らせるなら True
            notify: currentChanged を出すなら True（設定値を画面へ写すだけのときは False）
        """
        if not 0 <= index < len(self._options) or index == self._current:
            return
        self._current = index
        if animate and self.isVisible():
            _run(self._anim, self._x, float(index))
        else:
            self._anim.stop()
            self._set_x(float(index))
        if notify:
            self.currentChanged.emit(index)

    def _set_x(self, x: float) -> None:
        """白い板の位置を更新して描き直す。"""
        self._x = x
        self.update()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        """押された場所の選択肢を選ぶ。"""
        if event.button() == Qt.MouseButton.LeftButton and self.isEnabled():
            self.set_current_index(int(event.position().x() // self._segment_w))

    def keyPressEvent(self, event) -> None:  # noqa: N802
        """左右の矢印キーで選択を動かす。"""
        if event.key() == Qt.Key.Key_Left:
            self.set_current_index(self._current - 1)
        elif event.key() == Qt.Key.Key_Right:
            self.set_current_index(self._current + 1)
        else:
            super().keyPressEvent(event)

    def paintEvent(self, _event) -> None:  # noqa: N802
        """溝 → 白い板 → 文字、の順に描く。"""
        p = theme.palette()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self.isEnabled():
            painter.setOpacity(0.5)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        track = QPainterPath()
        track.addRoundedRect(rect, Metrics.FIELD_RADIUS, Metrics.FIELD_RADIUS)
        painter.fillPath(track, p.control)
        painter.setPen(QPen(rgba(SIGNAL, 0.55), 2.0) if self.hasFocus() else QPen(p.control_border, 1.0))
        painter.drawPath(track)

        thumb = QRectF(
            self._x * self._segment_w + self._INSET,
            self._INSET,
            self._segment_w - self._INSET * 2,
            self.height() - self._INSET * 2,
        )
        radius = Metrics.FIELD_RADIUS - self._INSET
        painter.setPen(Qt.PenStyle.NoPen)
        # 板の下にごく薄い影（浮きを出す）
        painter.setBrush(QColor(0, 0, 0, 26))
        painter.drawRoundedRect(thumb.translated(0.0, 1.0), radius, radius)
        painter.setBrush(rgba("#FFFFFF", 0.20) if p.dark else rgba("#FFFFFF"))
        painter.drawRoundedRect(thumb, radius, radius)

        painter.setFont(self._font)
        painter.setPen(p.text)
        for index, option in enumerate(self._options):
            cell = QRectF(index * self._segment_w, 0.0, self._segment_w, float(self.height()))
            painter.drawText(cell, Qt.AlignmentFlag.AlignCenter, option)


# ---------------------------------------------------------------------------
# サイドバーのナビ
# ---------------------------------------------------------------------------


class NavList(QWidget):
    """
    サイドバーのページ一覧。選んでいる行の後ろに灯りの面があり、選び直すと滑って移る。
    行の間に小見出し（「音声入力」「アプリ」など）を挟める。

    Signals:
        currentChanged(int): 選ぶ行が変わったとき（引数は行の番号。小見出しは数えない）
    """

    currentChanged = Signal(int)

    _GAP = 2            # 行と行のすき間
    _TEXT_SIZE = 14     # 行の文字（SettingsView.swift のナビ行と同じ）
    _PAD_H = 10         # 行の左右の余白
    _ICON_W = 22        # アイコンの幅
    _ICON_SIDE = 16     # アイコンそのものの大きさ
    _SECTION_TOP = 14   # 小見出しの上の余白
    _SECTION_H = 15     # 小見出しの文字の高さ
    _SECTION_BOTTOM = 4  # 小見出しの下の余白

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._items: List[tuple] = []      # (見出し, アイコンの名前)
        self._tops: List[int] = []         # 各行の上端
        self._sections: List[tuple] = []   # (小見出し, 上端)
        self._cursor = 0                   # 次に足すものの上端
        self._current = -1
        self._hover = -1
        self._pill_y = 0.0
        self._anim = _animation(self, Motion.NAV_MS, self._set_pill_y)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        Theme.instance().changed.connect(self.update)

    def add_section(self, title: str) -> None:
        """
        小見出しを足す（この後に足す行の上に出る）。

        Args:
            title: 小見出しの文字
        """
        self._sections.append((title, self._cursor + self._SECTION_TOP))
        self._cursor += self._SECTION_TOP + self._SECTION_H + self._SECTION_BOTTOM
        self.setFixedHeight(self._cursor)
        self.update()

    def add_item(self, title: str, icon: str = "") -> int:
        """
        行を足す。

        Args:
            title: 見出し
            icon: アイコンの名前（icons.names() のどれか。空ならアイコンなし）

        Returns:
            足した行の番号
        """
        self._items.append((title, icon))
        self._tops.append(self._cursor)
        self._cursor += Metrics.NAV_ROW_H + self._GAP
        self.setFixedHeight(self._cursor - self._GAP)
        if self._current < 0:
            self._current = 0
            self._pill_y = float(self._tops[0])
        self.update()
        return len(self._items) - 1

    def current_index(self) -> int:
        """いま選んでいる行の番号を返す。"""
        return self._current

    def set_current_index(self, index: int, animate: bool = True) -> None:
        """
        行を選ぶ。

        Args:
            index: 選ぶ行の番号
            animate: 灯りの面を滑らせるなら True（起動時の復元などは False）
        """
        if not 0 <= index < len(self._items) or index == self._current:
            return
        self._current = index
        target = float(self._tops[index])
        if animate and self.isVisible():
            _run(self._anim, self._pill_y, target)
        else:
            self._anim.stop()
            self._set_pill_y(target)
        self.currentChanged.emit(index)

    def _index_at(self, y: float) -> int:
        """位置 y にある行の番号を返す（行の外なら -1）。"""
        for index, top in enumerate(self._tops):
            if top <= y <= top + Metrics.NAV_ROW_H:
                return index
        return -1

    def _set_pill_y(self, y: float) -> None:
        self._pill_y = y
        self.update()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        index = self._index_at(event.position().y())
        if index != self._hover:
            self._hover = index
            self.update()

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hover = -1
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            index = self._index_at(event.position().y())
            if index >= 0:
                self.set_current_index(index)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        """上下キーで行を移る。"""
        if event.key() == Qt.Key.Key_Up:
            self.set_current_index(max(0, self._current - 1))
        elif event.key() == Qt.Key.Key_Down:
            self.set_current_index(min(len(self._items) - 1, self._current + 1))
        else:
            super().keyPressEvent(event)

    def paintEvent(self, _event) -> None:  # noqa: N802
        """小見出し → 載せている行の面 → 選んでいる行の灯り → 各行のアイコンと見出し、の順に描く。"""
        p = theme.palette()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w = float(self.width())
        radius = float(Metrics.NAV_ROW_RADIUS)

        painter.setFont(theme.font(11, QFont.Weight.DemiBold))
        painter.setPen(p.text_secondary)
        for title, top in self._sections:
            painter.drawText(
                QRectF(12.0, top, w - 24.0, self._SECTION_H),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                title,
            )

        if self._hover >= 0 and self._hover != self._current:
            hover_path = QPainterPath()
            hover_path.addRoundedRect(QRectF(0, self._tops[self._hover], w, Metrics.NAV_ROW_H), radius, radius)
            painter.fillPath(hover_path, rgba("#FFFFFF" if p.dark else "#000000", 0.07))

        if self._current >= 0:
            pill = QRectF(0.5, self._pill_y + 0.5, w - 1.0, Metrics.NAV_ROW_H - 1.0)
            pill_path = QPainterPath()
            pill_path.addRoundedRect(pill, radius, radius)
            # 選択の面: signal → signal 0.75、白 0.4 → 0 の縁（SettingsView.swift のナビ行と同じ）
            fill = QLinearGradient(0, pill.top(), 0, pill.bottom())
            fill.setColorAt(0.0, rgba(SIGNAL))
            fill.setColorAt(1.0, rgba(SIGNAL, 0.75))
            painter.fillPath(pill_path, fill)
            painter.setPen(_rim_pen(rgba("#FFFFFF", 0.40), rgba("#FFFFFF", 0.0), pill.top(), pill.bottom()))
            painter.drawPath(pill_path)

        for index, (title, icon) in enumerate(self._items):
            top = float(self._tops[index])
            # 灯りの面が半分以上重なっている行は白い文字にする（滑っている途中でも読めるように）
            on_pill = abs(top - self._pill_y) < Metrics.NAV_ROW_H / 2.0
            color = p.on_accent if on_pill else p.text
            x = float(self._PAD_H)
            if icon:
                side = float(self._ICON_SIDE)
                icons.paint(
                    painter,
                    icon,
                    QRectF(x + (self._ICON_W - side) / 2.0, top + (Metrics.NAV_ROW_H - side) / 2.0, side, side),
                    color,
                )
                x += self._ICON_W + 8.0
            painter.setPen(color)
            painter.setFont(theme.font(self._TEXT_SIZE))
            painter.drawText(
                QRectF(x, top, w - x - 8.0, Metrics.NAV_ROW_H),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                title,
            )


# ---------------------------------------------------------------------------
# ページの器と切替
# ---------------------------------------------------------------------------


class ScrollPage(QScrollArea):
    """
    1 ページ分の器。中身は最大幅 720 で縦に並べ、はみ出したら縦にスクロールする。

    Attributes:
        body: 中身を足していく縦のレイアウト
    """

    def __init__(self, parent: Optional[QWidget] = None, max_width: Optional[int] = Metrics.CONTENT_MAX_W) -> None:
        """
        Args:
            parent: 親ウィジェット
            max_width: 中身の最大幅。None なら窓の幅いっぱいまで広げる（ダッシュボード用）
        """
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QScrollArea.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.viewport().setAutoFillBackground(False)

        holder = QWidget()
        holder.setAutoFillBackground(False)
        outer = QHBoxLayout(holder)
        outer.setContentsMargins(
            Metrics.CONTENT_PADDING, Metrics.CONTENT_PADDING, Metrics.CONTENT_PADDING, Metrics.CONTENT_PADDING
        )
        outer.setSpacing(0)
        column = QWidget()
        if max_width is not None:
            column.setMaximumWidth(max_width)
        self.body = QVBoxLayout(column)
        self.body.setContentsMargins(0, 0, 0, 0)
        self.body.setSpacing(10)
        outer.addWidget(column, 1)
        # 幅が 720 を超えた分は右に余白として残す（中身は左に寄せる）
        outer.addStretch(0)
        self.setWidget(holder)

    def add_title(self, title: str, subtitle: str = "") -> None:
        """
        ページの見出しを足す。

        Args:
            title: ページ名
            subtitle: 1 行の説明（空なら出さない）
        """
        self.body.addWidget(Text(title, size=17, weight=QFont.Weight.Bold))
        if subtitle:
            self.body.addWidget(Text(subtitle, size=12, role="secondary", wrap=True))
        self.body.addSpacing(8)

    def add_section(self, title: str) -> None:
        """棚の上に置く小見出しを足す。"""
        self.body.addSpacing(8)
        label = Text(title, size=13, weight=QFont.Weight.DemiBold)
        label.setContentsMargins(4, 0, 0, 0)
        self.body.addWidget(label)

    def add_group(self) -> SettingsGroup:
        """設定行をまとめる棚を足して返す。"""
        group = SettingsGroup()
        self.body.addWidget(group)
        return group

    def finish(self) -> None:
        """中身を上に寄せる（最後に 1 回呼ぶ）。"""
        self.body.addStretch(1)


class FadeStack(QStackedWidget):
    """
    ページを重ねて持ち、切り替えるときに新しいページをふわっと出す。
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._effect: Optional[QGraphicsOpacityEffect] = None
        self._anim = _animation(self, Motion.PAGE_MS, self._set_opacity)
        self._anim.finished.connect(self._clear_effect)

    def _set_opacity(self, value: float) -> None:
        if self._effect is not None:
            self._effect.setOpacity(value)

    def _clear_effect(self) -> None:
        """出し終えたら効果を外す（付けたままだと中の部品の描画が毎回 1 枚絵を経由して重くなる）。"""
        widget = self.currentWidget()
        if widget is not None:
            widget.setGraphicsEffect(None)
        self._effect = None

    def show_index(self, index: int, animate: bool = True) -> None:
        """
        ページを切り替える。

        Args:
            index: 出すページの番号
            animate: ふわっと出すなら True
        """
        if index == self.currentIndex() or not 0 <= index < self.count():
            return
        self._anim.stop()
        self._clear_effect()
        self.setCurrentIndex(index)
        if not animate or not self.isVisible():
            return
        widget = self.currentWidget()
        self._effect = QGraphicsOpacityEffect(widget)
        self._effect.setOpacity(0.0)
        widget.setGraphicsEffect(self._effect)
        _run(self._anim, 0.0, 1.0)
