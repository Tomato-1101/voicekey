"""
UI 部品モジュール

設定ウィンドウを組み立てる部品（ナビ・設定カード・トグル・ボタン・ピッカー・
メッセージ欄・ページ切替）を提供する。画面の並びと文言は Mac 版と同じにし、
部品の形と色は Windows 11 の標準（WinUI 3 / 「設定」アプリ）に合わせる。
色と寸法はすべて theme.py から取る。

トグル・ボタン・ナビ・カードは QSS ではなく QPainter で描く。下の縁だけ濃い枠
（Windows のボタンの立体感）と、滑らかな動きは QSS では出せないため。
"""

from typing import List, Optional

from PySide6.QtCore import (
    QEasingCurve,
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
    QBoxLayout,
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
from .theme import Metrics, Motion, Palette, Theme, rgba


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


def _elevation_pen(rect: QRectF, top: QColor, bottom: QColor) -> QPen:
    """
    下の縁だけ少し濃い 1px の枠を描くペンを作る（Windows のボタン・入力欄の立体感）。

    Args:
        rect: 枠を描く四角
        top: 上と左右の縁の色
        bottom: 下の縁の色
    """
    gradient = QLinearGradient(0, rect.top(), 0, rect.bottom())
    gradient.setColorAt(0.0, top)
    # 下端の 2px ぶんだけ濃くする（WinUI の ControlElevationBorderBrush と同じ考え方）
    gradient.setColorAt(max(0.0, 1.0 - 2.0 / max(rect.height(), 1.0)), top)
    gradient.setColorAt(1.0, bottom)
    return QPen(gradient, 1.0)


def _accent_border(p: Palette, rect: QRectF) -> QPen:
    """アクセント色の面に付ける枠（上はほぼ見えず、下が少し暗い）。"""
    return _elevation_pen(rect, rgba("#FFFFFF", 0.08), rgba("#000000", 0.14 if p.dark else 0.40))


def _paint_focus_ring(painter: QPainter, rect: QRectF, radius: float) -> None:
    """
    キーボードで移ってきた部品に付ける枠（Windows の FocusVisual。本文色の 2px の線）。

    Args:
        painter: 描く先
        rect: 枠を描く四角（線の中心がここを通る）
        radius: 角丸の半径
    """
    painter.save()
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.setPen(QPen(theme.palette().text, 2.0))
    painter.drawRoundedRect(rect, radius, radius)
    painter.restore()


def _is_keyboard_focus(event) -> bool:
    """フォーカスがキーボード操作（Tab など）で移ってきたなら True。"""
    return event.reason() in (
        Qt.FocusReason.TabFocusReason,
        Qt.FocusReason.BacktabFocusReason,
        Qt.FocusReason.ShortcutFocusReason,
    )


# ---------------------------------------------------------------------------
# 文字
# ---------------------------------------------------------------------------


class Text(QLabel):
    """
    役割（本文・補助・アクセント・注意など）で色が決まる文字。外観が変わると色も追従する。

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
            size: 文字の大きさ（Mac 版と同じ数値。theme.px で Windows の大きさになる）
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
            f" background: transparent; font-size: {theme.px(self._size)}px;"
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
            size: 文字の大きさ（Mac 版と同じ数値）
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
# 窓の下地
# ---------------------------------------------------------------------------


class Backdrop(QWidget):
    """
    窓の下地。Windows 11 の「設定」アプリと同じく、窓全体を 1 色（ライト #F3F3F3 /
    ダーク #202020）で塗る。サイドバーも本文も同じ地の上に載る。
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        Theme.instance().changed.connect(self.update)

    def paintEvent(self, _event) -> None:  # noqa: N802
        """地色を塗る。"""
        painter = QPainter(self)
        painter.fillRect(self.rect(), theme.palette().window)


# ---------------------------------------------------------------------------
# トグル・ボタン・ピッカー
# ---------------------------------------------------------------------------


class Switch(QAbstractButton):
    """
    トグルスイッチ（WinUI の ToggleSwitch）。左に「オン／オフ」の文字、右に 40×20 の溝。

    OFF: 枠だけの溝と灰色のつまみ。ON: アクセント色の溝と白（ダークでは黒）のつまみ。
    つまみは滑って移り、マウスを載せると少し大きくなる。
    「オン」「オフ」は同じ幅の欄に出すので、切り替えても部品の大きさは変わらない。
    """

    _ON_TEXT = "オン"
    _OFF_TEXT = "オフ"
    _TEXT_GAP = 12.0   # 文字と溝のすき間
    _RING = 4.0        # フォーカス枠を描くための外側の余白

    def __init__(self, checked: bool = False, parent: Optional[QWidget] = None) -> None:
        """
        Args:
            checked: 最初の状態
            parent: 親ウィジェット
        """
        super().__init__(parent)
        self.setCheckable(True)
        self.setChecked(checked)
        self.setFont(theme.font(13))
        metrics = QFontMetrics(self.font())
        self._text_w = float(max(metrics.horizontalAdvance(self._ON_TEXT), metrics.horizontalAdvance(self._OFF_TEXT)))
        self.setFixedSize(self.sizeHint())
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self._t = 1.0 if checked else 0.0
        self._hover = 0.0
        self._focus_by_keyboard = False
        self._anim = _animation(self, Motion.TOGGLE_MS, self._set_t)
        self._hover_anim = _animation(self, Motion.HOVER_MS, self._set_hover)
        self.toggled.connect(self._on_toggled)
        Theme.instance().changed.connect(self.update)

    def _set_t(self, t: float) -> None:
        """つまみの位置（0.0 = OFF、1.0 = ON）を更新して描き直す。"""
        self._t = t
        self.update()

    def _set_hover(self, value: float) -> None:
        self._hover = value
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

    def enterEvent(self, event) -> None:  # noqa: N802
        _run(self._hover_anim, self._hover, 1.0)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        _run(self._hover_anim, self._hover, 0.0)
        super().leaveEvent(event)

    def set_hovered(self, hovered: bool) -> None:
        """行全体にマウスが載ったとき（行のどこを押しても切り替わるため）に、つまみを大きくする。"""
        _run(self._hover_anim, self._hover, 1.0 if hovered else 0.0)

    def sizeHint(self) -> QSize:  # noqa: N802
        width = self._text_w + self._TEXT_GAP + Metrics.SWITCH_W + self._RING
        return QSize(int(width + 0.5), int(Metrics.SWITCH_H + self._RING * 2))

    def paintEvent(self, _event) -> None:  # noqa: N802
        """「オン／オフ」の文字、溝、つまみの順に描く。"""
        p = theme.palette()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        enabled = self.isEnabled()

        painter.setFont(self.font())
        painter.setPen(p.text if enabled else p.text_disabled)
        painter.drawText(
            QRectF(0.0, 0.0, self._text_w, float(self.height())),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
            self._ON_TEXT if self.isChecked() else self._OFF_TEXT,
        )

        w, h = float(Metrics.SWITCH_W), float(Metrics.SWITCH_H)
        track = QRectF(self._text_w + self._TEXT_GAP, (self.height() - h) / 2.0, w, h)
        if not enabled:
            painter.setOpacity(0.4)
        radius = h / 2.0
        off_fill = p.track_off if not self._hover else _mix(p.track_off, p.subtle_hover, self._hover)
        on_fill = _mix(p.accent, p.accent_hover, self._hover)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(_mix(off_fill, on_fill, self._t))
        painter.drawRoundedRect(track, radius, radius)
        # OFF の溝は枠で形を出す（ON に近づくほど消す）
        border = QColor(p.track_off_border)
        border.setAlphaF(border.alphaF() * (1.0 - self._t))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(border, 1.0))
        painter.drawRoundedRect(track.adjusted(0.5, 0.5, -0.5, -0.5), radius - 0.5, radius - 0.5)

        knob = 12.0 + 2.0 * self._hover if enabled else 12.0
        cx = track.left() + _lerp(10.0, w - 10.0, self._t)
        cy = track.center().y()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(_mix(p.knob_off, p.on_accent, self._t))
        painter.drawEllipse(QPointF(cx, cy), knob / 2.0, knob / 2.0)

        if self.hasFocus() and self._focus_by_keyboard:
            painter.setOpacity(1.0)
            _paint_focus_ring(painter, track.adjusted(-3.0, -3.0, 3.0, 3.0), radius + 3.0)

    def focusInEvent(self, event) -> None:  # noqa: N802
        """キーボードで移ってきたときだけ枠を出すため、理由を覚えておく。"""
        self._focus_by_keyboard = _is_keyboard_focus(event)
        super().focusInEvent(event)


class CapsuleButton(QAbstractButton):
    """
    ボタン（WinUI の Button。角丸 4・高さ 32・下の縁が少し濃い枠）。

    名前は Mac 版の部品に合わせて残しているが、形は Windows の標準のボタン。

    kind:
        "primary": アクセント色の面に白い文字（その画面でいちばん大事な操作に 1 つだけ）
        "secondary": 白い面（既定）
        "destructive": 白い面に赤い文字（消す・やめる）
        "link": 面を持たないアクセント色の文字（HyperlinkButton）
    size:
        "small" / "regular" / "large"
    """

    # (文字の大きさ〔Mac 版の数値〕, 左右の余白, 高さ)
    _SIZES = {
        "small": (11, 8, 24),
        "regular": (13, 12, Metrics.BUTTON_H),
        "large": (13, 16, 36),
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
            icon: アイコンの名前（icons.names() のどれか）。入れると文字の代わりにアイコンだけの正方形のボタンになる
        """
        super().__init__(parent)
        self._kind = kind
        self._icon = icon
        font_size, self._pad_h, self._height = self._SIZES.get(size, self._SIZES["regular"])
        if kind == "link":
            # 面が無いので、文字の端を周りの端に揃える
            font_size = 12
            self._pad_h = 0
        self.setFont(theme.font(font_size))
        self.setText(text)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self._press = 0.0
        self._hover = 0.0
        self._focus_by_keyboard = False
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
        if self._icon:
            return QSize(self._height, self._height)
        metrics = QFontMetrics(self.font())
        if self._kind == "link":
            return QSize(metrics.horizontalAdvance(self.text()) + 2, metrics.height() + 6)
        return QSize(max(metrics.horizontalAdvance(self.text()) + self._pad_h * 2 + 2, 64), self._height)

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return self.sizeHint()

    def paintEvent(self, _event) -> None:  # noqa: N802
        """面・枠・文字を描く。押している間は面が少し沈み、文字が薄くなる（Windows と同じ）。"""
        p = theme.palette()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self.isEnabled():
            painter.setOpacity(0.45)

        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = float(Metrics.FIELD_RADIUS)

        if self._kind == "primary":
            fill = _mix(p.accent, p.accent_hover, self._hover)
            fill = _mix(fill, rgba(p.accent.name(), 0.8), self._press)
            painter.setBrush(fill)
            painter.setPen(_accent_border(p, rect))
            painter.drawRoundedRect(rect, radius, radius)
            text_color = _mix(p.on_accent, rgba(p.on_accent.name(), 0.7), self._press)
        elif self._kind == "link":
            text_color = _mix(p.accent_text, rgba(p.accent_text.name(), 0.75), max(self._hover * 0.6, self._press))
        else:
            fill = _mix(p.control, p.control_hover, self._hover)
            fill = _mix(fill, p.control_pressed, self._press)
            painter.setBrush(fill)
            painter.setPen(_elevation_pen(rect, p.control_border, p.control_border_bottom))
            painter.drawRoundedRect(rect, radius, radius)
            base = p.danger if self._kind == "destructive" else p.text
            text_color = _mix(base, p.text_secondary, self._press) if self._kind != "destructive" else base

        if self.hasFocus() and self._focus_by_keyboard:
            _paint_focus_ring(painter, rect.adjusted(0.5, 0.5, -0.5, -0.5), radius)

        if self._icon:
            side = 16.0
            icons.paint(
                painter,
                self._icon,
                QRectF((self.width() - side) / 2.0, (self.height() - side) / 2.0, side, side),
                text_color,
            )
            return
        painter.setPen(text_color)
        painter.setFont(self.font())
        align = Qt.AlignmentFlag.AlignVCenter | (
            Qt.AlignmentFlag.AlignLeft if self._kind == "link" else Qt.AlignmentFlag.AlignHCenter
        )
        painter.drawText(self.rect(), align, self.text())

    def focusInEvent(self, event) -> None:  # noqa: N802
        """キーボードで移ってきたときだけ枠を出すため、理由を覚えておく。"""
        self._focus_by_keyboard = _is_keyboard_focus(event)
        super().focusInEvent(event)


class Picker(QComboBox):
    """
    選択肢から 1 つ選ぶ部品（WinUI の ComboBox）。

    白い面に、選んでいる文字を左寄せで出し、右端に下向きの山形を置く。
    部品の幅は「いちばん長い選択肢」のままなので、選び直しても周りは動かない。
    開いたときの一覧の見た目は theme.app_qss が決める。

    ページをスクロールしている途中でホイールが乗っても値が変わらないようにしてある
    （フォーカスが無いときのホイールは親へ流す）。
    """

    _TEXT_LEFT = 11.0     # 左の縁から文字まで
    _CHEVRON_RIGHT = 16.0  # 右の縁から山形の中心まで

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(Metrics.FIELD_H)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self._pressed = False
        self._focus_by_keyboard = False
        Theme.instance().changed.connect(self.update)

    def wheelEvent(self, event) -> None:  # noqa: N802
        """フォーカスが無いときは値を変えず、ページのスクロールに任せる。"""
        if self.hasFocus():
            super().wheelEvent(event)
        else:
            event.ignore()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        self._pressed = True
        self.update()
        super().mousePressEvent(event)

    def hidePopup(self) -> None:  # noqa: N802
        """一覧を閉じたら押し込みの色を戻す。"""
        self._pressed = False
        super().hidePopup()
        self.update()

    def focusInEvent(self, event) -> None:  # noqa: N802
        self._focus_by_keyboard = _is_keyboard_focus(event)
        super().focusInEvent(event)

    def paintEvent(self, _event) -> None:  # noqa: N802
        """面・枠・選んでいる文字（左寄せ）・下向きの山形を描く。"""
        p = theme.palette()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        enabled = self.isEnabled()
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = float(Metrics.FIELD_RADIUS)

        if not enabled:
            fill = p.control_pressed
        elif self._pressed:
            fill = p.control_pressed
        elif self.underMouse():
            fill = p.control_hover
        else:
            fill = p.control
        painter.setBrush(fill)
        painter.setPen(_elevation_pen(rect, p.control_border, p.control_border_bottom))
        painter.drawRoundedRect(rect, radius, radius)
        if self.hasFocus() and self._focus_by_keyboard and enabled:
            _paint_focus_ring(painter, rect.adjusted(0.5, 0.5, -0.5, -0.5), radius)

        cx = self.width() - self._CHEVRON_RIGHT
        cy = self.height() / 2.0
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(
            QPen(
                p.text_secondary if enabled else p.text_disabled,
                1.2,
                Qt.PenStyle.SolidLine,
                Qt.PenCapStyle.RoundCap,
                Qt.PenJoinStyle.RoundJoin,
            )
        )
        chevron = QPainterPath()
        chevron.moveTo(cx - 4.0, cy - 2.0)
        chevron.lineTo(cx, cy + 2.0)
        chevron.lineTo(cx + 4.0, cy - 2.0)
        painter.drawPath(chevron)

        painter.setPen(p.text if enabled else p.text_disabled)
        painter.setFont(self.font())
        text_rect = QRectF(self._TEXT_LEFT, 0.0, cx - 10.0 - self._TEXT_LEFT, float(self.height()))
        text = QFontMetrics(self.font()).elidedText(
            self.currentText(), Qt.TextElideMode.ElideRight, int(text_rect.width())
        )
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, text)


# ---------------------------------------------------------------------------
# 設定カードと、カードを並べる棚
# ---------------------------------------------------------------------------


class SettingRow(QWidget):
    """
    設定の 1 行（Windows の「設定」アプリの SettingsCard）。左に見出しと説明、右に操作部品。

    右がトグルのときは、行のどこを押しても切り替わり、マウスを載せると行の面が明るくなる。
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
        layout.setSpacing(16)

        labels = QVBoxLayout()
        labels.setContentsMargins(0, 0, 0, 0)
        labels.setSpacing(1)
        # 見出しと説明は行の上下中央に置く（Windows の SettingsCard と同じ。折り返しの高さ見積もりで
        # 行が少し高くなっても、文字が上に寄って下が空いて見えないようにする）
        labels.addStretch(1)
        self.title_label = Text(title, size=13)
        labels.addWidget(self.title_label)
        self.caption_label = Text(caption, size=11, role="secondary", wrap=True)
        self.caption_label.setVisible(bool(caption))
        labels.addWidget(self.caption_label)
        labels.addStretch(1)
        layout.addLayout(labels, 1)

        if trailing is not None:
            layout.addWidget(trailing, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._stacked = False
        self.setMinimumHeight(Metrics.ROW_MIN_H)
        if self.is_clickable():
            self.setCursor(Qt.CursorShape.PointingHandCursor)

    def is_clickable(self) -> bool:
        """行のどこを押しても操作できる（右がトグルの）行なら True。"""
        return isinstance(self._trailing, Switch) and self._trailing.isEnabled()

    def set_caption(self, caption: str) -> None:
        """説明を差し替える。"""
        self.caption_label.setText(caption)
        self.caption_label.setVisible(bool(caption))

    # 見出しと説明の欄がこれより細くなるなら、操作部品を見出しの下へ回す
    # （Windows の SettingsCard が窓を狭めたときと同じ並び。説明が 1 語ずつ折り返されて読めなくなるのを防ぐ）
    _STACK_BELOW_LABEL_W = 240

    def resizeEvent(self, event) -> None:  # noqa: N802
        """幅に合わせて、操作部品を右に置くか見出しの下に置くかを決め直す。"""
        super().resizeEvent(event)
        if self._trailing is None or self._trailing.isHidden():
            return
        layout = self.layout()
        room = (
            self.width() - 2 * Metrics.ROW_PADDING_H - layout.spacing() - self._trailing.sizeHint().width()
        )
        stacked = room < self._STACK_BELOW_LABEL_W
        if stacked == self._stacked:
            return
        self._stacked = stacked
        if stacked:
            layout.setDirection(QBoxLayout.Direction.TopToBottom)
            layout.setSpacing(8)
            layout.setAlignment(self._trailing, Qt.AlignmentFlag.AlignLeft)
        else:
            layout.setDirection(QBoxLayout.Direction.LeftToRight)
            layout.setSpacing(16)
            layout.setAlignment(self._trailing, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

    def enterEvent(self, event) -> None:  # noqa: N802
        self._hover_changed(True)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hover_changed(False)
        super().leaveEvent(event)

    def _hover_changed(self, hovered: bool) -> None:
        """押せる行なら、行の面（棚が描く）とトグルのつまみを載せた状態にする。"""
        if not self.is_clickable():
            return
        self._trailing.set_hovered(hovered)
        parent = self.parentWidget()
        if parent is not None:
            parent.update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        """右がトグルなら、行のどこを押しても切り替える。"""
        if (
            self.is_clickable()
            and event.button() == Qt.MouseButton.LeftButton
            and self.rect().contains(event.position().toPoint())
        ):
            self._trailing.click()
        super().mouseReleaseEvent(event)


class SettingsGroup(QWidget):
    """
    設定カードを縦に並べる棚。Windows の「設定」アプリと同じく、1 行ずつ独立したカード
    （白い面・角丸 4・薄い枠）にし、カードの間を 4px あける。
    """

    SPACING = 4

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(self.SPACING)
        Theme.instance().changed.connect(self.update)

    def add_row(self, row: QWidget) -> QWidget:
        """
        行を足す。

        Args:
            row: 足す行（SettingRow でなくてもよい。どの行もカードの上に載る）

        Returns:
            足した行（続けて設定できるように返す）
        """
        self._layout.addWidget(row)
        return row

    def paintEvent(self, _event) -> None:  # noqa: N802
        """見えている行ごとにカードを描く（押せる行にマウスが載っていれば面を明るくする）。"""
        p = theme.palette()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        radius = float(Metrics.GROUP_RADIUS)
        for index in range(self._layout.count()):
            widget = self._layout.itemAt(index).widget()
            if widget is None or not widget.isVisible():
                continue
            rect = QRectF(widget.geometry()).adjusted(0.5, 0.5, -0.5, -0.5)
            hovered = isinstance(widget, SettingRow) and widget.is_clickable() and widget.underMouse()
            painter.setBrush(p.row_hover if hovered else p.row)
            painter.setPen(QPen(p.card_border, 1.0))
            painter.drawRoundedRect(rect, radius, radius)


class Card(QWidget):
    """
    数字や一覧を載せる面（ダッシュボードのカード。WinUI の Card と同じ角丸 8・薄い枠）。

    Attributes:
        body: 中身を足していく縦のレイアウト
    """

    RADIUS = 8

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
        """面と枠を描く。"""
        p = theme.palette()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(p.row)
        painter.setPen(QPen(p.card_border, 1.0))
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), self.RADIUS, self.RADIUS)


class Segmented(QWidget):
    """
    横に並んだ選択肢から 1 つを選ぶ部品（Windows Community Toolkit の Segmented と同じ形）。
    選ばれている面と、その下のアクセント色の短い棒が滑って移る。

    幅は「いちばん長い選択肢」で決めて固定する。選び直しても周りの部品が動かないようにするため。

    Signals:
        currentChanged(int): 選択が変わったとき（引数は選ばれた番号）
    """

    currentChanged = Signal(int)

    HEIGHT = 32
    _PADDING_H = 14   # 選択肢の文字の左右の余白
    _INSET = 2.0      # 溝の縁から選択の面までのすき間

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
        self._x = float(self._current)   # 選択の面の位置（選択肢 1 つぶんを 1.0 とする）
        self._font = theme.font(13)
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
            animate: 選択の面を滑らせるなら True
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
        """選択の面の位置を更新して描き直す。"""
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
        """溝 → 選択の面 → アクセントの棒 → 文字、の順に描く。"""
        p = theme.palette()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self.isEnabled():
            painter.setOpacity(0.45)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = float(Metrics.FIELD_RADIUS)
        painter.setBrush(p.track_off)
        painter.setPen(QPen(p.control_border, 1.0))
        painter.drawRoundedRect(rect, radius, radius)

        thumb = QRectF(
            self._x * self._segment_w + self._INSET,
            self._INSET,
            self._segment_w - self._INSET * 2,
            self.height() - self._INSET * 2,
        )
        inner = radius - 1.0
        painter.setBrush(p.control)
        painter.setPen(_elevation_pen(thumb, p.control_border, p.control_border_bottom))
        painter.drawRoundedRect(thumb, inner, inner)
        bar = QRectF(thumb.center().x() - 8.0, thumb.bottom() - 4.0, 16.0, 3.0)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(p.accent)
        painter.drawRoundedRect(bar, 1.5, 1.5)
        if self.hasFocus():
            _paint_focus_ring(painter, rect.adjusted(0.5, 0.5, -0.5, -0.5), radius)

        painter.setFont(self._font)
        for index, option in enumerate(self._options):
            cell = QRectF(index * self._segment_w, -1.0, self._segment_w, float(self.height()))
            painter.setPen(p.text if index == self._current else p.text_secondary)
            painter.drawText(cell, Qt.AlignmentFlag.AlignCenter, option)


# ---------------------------------------------------------------------------
# サイドバーのナビ
# ---------------------------------------------------------------------------


class NavList(QWidget):
    """
    サイドバーのページ一覧（WinUI の NavigationView の左ペイン）。

    選んでいる行は薄い面で示し、行の左端にアクセント色の短い縦棒を出す。選び直すと棒が滑って移る。
    行の間に小見出し（「音声入力」「アプリ」など）を挟める。

    Signals:
        currentChanged(int): 選ぶ行が変わったとき（引数は行の番号。小見出しは数えない）
    """

    currentChanged = Signal(int)

    _GAP = 4            # 行と行のすき間
    _TEXT_SIZE = 13     # 行の文字（Mac 版の数値。Windows では 14px）
    _ICON_X = 12        # 行の左端からアイコンまで
    _TEXT_X = 44        # 行の左端から見出しまで（アイコンの列 40px の右）
    _SECTION_TOP = 16   # 小見出しの上の余白
    _SECTION_H = 20     # 小見出しの文字の高さ
    _SECTION_BOTTOM = 4  # 小見出しの下の余白
    _BAR_H = 16.0       # 選択の縦棒の高さ
    _BAR_W = 3.0        # 選択の縦棒の幅

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._items: List[tuple] = []      # (見出し, アイコンの名前)
        self._tops: List[int] = []         # 各行の上端
        self._sections: List[tuple] = []   # (小見出し, 上端)
        self._cursor = 0                   # 次に足すものの上端
        self._current = -1
        self._hover = -1
        self._pressed = -1
        self._bar_y = 0.0
        self._anim = _animation(self, Motion.NAV_MS, self._set_bar_y)
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
            self._bar_y = float(self._tops[0])
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
            animate: 縦棒を滑らせるなら True（起動時の復元などは False）
        """
        if not 0 <= index < len(self._items) or index == self._current:
            return
        self._current = index
        target = float(self._tops[index])
        if animate and self.isVisible():
            _run(self._anim, self._bar_y, target)
        else:
            self._anim.stop()
            self._set_bar_y(target)
        self.currentChanged.emit(index)

    def _index_at(self, y: float) -> int:
        """位置 y にある行の番号を返す（行の外なら -1）。"""
        for index, top in enumerate(self._tops):
            if top <= y <= top + Metrics.NAV_ROW_H:
                return index
        return -1

    def _set_bar_y(self, y: float) -> None:
        self._bar_y = y
        self.update()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        index = self._index_at(event.position().y())
        if index != self._hover:
            self._hover = index
            self.update()

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hover = -1
        self._pressed = -1
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._pressed = self._index_at(event.position().y())
            self.update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            index = self._index_at(event.position().y())
            pressed, self._pressed = self._pressed, -1
            if index >= 0 and index == pressed:
                self.set_current_index(index)
            self.update()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        """上下キーで行を移る。"""
        if event.key() == Qt.Key.Key_Up:
            self.set_current_index(max(0, self._current - 1))
        elif event.key() == Qt.Key.Key_Down:
            self.set_current_index(min(len(self._items) - 1, self._current + 1))
        else:
            super().keyPressEvent(event)

    def paintEvent(self, _event) -> None:  # noqa: N802
        """小見出し → 行の面（選択・載せている・押している）→ 縦棒 → アイコンと見出し、の順に描く。"""
        p = theme.palette()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w = float(self.width())
        row_h = float(Metrics.NAV_ROW_H)
        radius = float(Metrics.NAV_ROW_RADIUS)

        painter.setFont(theme.font(13, QFont.Weight.DemiBold))
        painter.setPen(p.text)
        for title, top in self._sections:
            painter.drawText(
                QRectF(float(self._ICON_X), top, w - self._ICON_X * 2.0, self._SECTION_H),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                title,
            )

        painter.setPen(Qt.PenStyle.NoPen)
        for index, top in enumerate(self._tops):
            if index == self._pressed:
                fill = p.subtle_pressed
            elif index == self._current:
                # 選んでいる行に載せたときは少しだけ薄くする（WinUI の SelectedPointerOver）
                fill = p.subtle_pressed if index == self._hover else p.subtle_hover
            elif index == self._hover:
                fill = p.subtle_hover
            else:
                continue
            painter.setBrush(fill)
            painter.drawRoundedRect(QRectF(0.0, float(top), w, row_h), radius, radius)

        if self._current >= 0:
            bar = QRectF(0.0, self._bar_y + (row_h - self._BAR_H) / 2.0, self._BAR_W, self._BAR_H)
            painter.setBrush(p.accent)
            painter.drawRoundedRect(bar, self._BAR_W / 2.0, self._BAR_W / 2.0)

        if self.hasFocus() and 0 <= self._current < len(self._tops):
            top = float(self._tops[self._current])
            _paint_focus_ring(painter, QRectF(1.0, top + 1.0, w - 2.0, row_h - 2.0), radius)

        painter.setFont(theme.font(self._TEXT_SIZE))
        for index, (title, icon) in enumerate(self._items):
            top = float(self._tops[index])
            if icon:
                side = 16.0
                icons.paint(
                    painter,
                    icon,
                    QRectF(float(self._ICON_X), top + (row_h - side) / 2.0, side, side),
                    p.text,
                )
            x = float(self._TEXT_X if icon else self._ICON_X)
            painter.setPen(p.text)
            painter.drawText(
                QRectF(x, top, w - x - 8.0, row_h),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                title,
            )


# ---------------------------------------------------------------------------
# ページの器と切替
# ---------------------------------------------------------------------------


class ScrollPage(QScrollArea):
    """
    1 ページ分の器。中身は最大幅 CONTENT_MAX_W で縦に並べ、はみ出したら縦にスクロールする。

    見出しは Windows の「設定」アプリと同じく 28px の Semibold、小見出しは 14px の Semibold。

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
        pad = Metrics.CONTENT_PADDING
        # 上と左右は Windows の「設定」アプリに合わせて少し広く取る
        outer.setContentsMargins(pad + 12, pad + 12, pad + 12, pad)
        outer.setSpacing(0)
        column = QWidget()
        if max_width is not None:
            column.setMaximumWidth(max_width)
        self.body = QVBoxLayout(column)
        self.body.setContentsMargins(0, 0, 0, 0)
        self.body.setSpacing(8)
        outer.addWidget(column, 1)
        # 幅が最大幅を超えた分は右に余白として残す（中身は左に寄せる）
        outer.addStretch(0)
        self.setWidget(holder)

    def add_title(self, title: str, subtitle: str = "") -> None:
        """
        ページの見出しを足す。

        Args:
            title: ページ名
            subtitle: 1 行の説明（空なら出さない）
        """
        self.body.addWidget(Text(title, size=28, weight=QFont.Weight.DemiBold))
        if subtitle:
            self.body.addWidget(Text(subtitle, size=13, role="secondary", wrap=True))
        self.body.addSpacing(12)

    def add_section(self, title: str) -> None:
        """カードの上に置く小見出しを足す。"""
        self.body.addSpacing(14)
        label = Text(title, size=13, weight=QFont.Weight.DemiBold)
        label.setContentsMargins(2, 0, 0, 0)
        self.body.addWidget(label)

    def add_group(self) -> SettingsGroup:
        """設定カードを並べる棚を足して返す。"""
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
