"""
サイドノッチ（画面左端の履歴スリット → クリックで履歴パネル）モジュール

Mac 版 SideNotch.swift と対の Windows 実装:
- 常駐スリット: 画面左端・垂直中央に、フォーカスを奪わない細い黒バー（うっすら外枠）。
  ホバーで少し太くなり、録音中はアクセント色のグローで点灯する。side_notch_enabled で表示切替。
- クリックで履歴パネル（ガラス島）が左端から出る。上部に検索フィールド、行クリックでコピー、
  下部に「ホームを開く」。外側クリック（パネルの非アクティブ化）or スリット再クリックで閉じる。
  全消去はホーム側だけに置く（ここには置かない）。
- 見た目は設定画面と同じ島（theme のパレットを自前で塗る）。外観の切り替えに追従する。
  背景のぼかし（DWM アクリル）は使わない。半透明の窓にアクリルを重ねると、Windows では
  窓の中身が描かれず空のパネルになった（2.4.0 の実機で「中身が表示されない」）。

最重要の制約: スリットは絶対にフォーカスを奪ってはならない（貼り付け先＝音声入力の入力先を
維持する）。履歴パネルは検索フィールドへ入力するためフォーカスを取るが、これはユーザーが
自分でスリットをクリックして履歴を見る操作なので許容する（口述中ではない）。
"""

import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

from PySide6.QtCore import QObject, QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetricsF,
    QGuiApplication,
    QLinearGradient,
    QPainter,
    QPen,
    QPixmap,
    QPainterPath,
    QTextLayout,
    QTextOption,
)
from PySide6.QtWidgets import (
    QAbstractButton,
    QApplication,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from . import icons, theme
from .theme import SIGNAL, Theme, rgba


def relative_time(iso_str: str, now: Optional[datetime] = None) -> str:
    """ISO 8601 文字列から日本語の相対時刻（「たった今」「N 分前」等）を作る。

    Mac 版 RelativeDateTimeFormatter（ja・short）の近似。解析に失敗したら空文字を返す。
    """
    if not iso_str:
        return ""
    try:
        dt = datetime.fromisoformat(iso_str)
    except (ValueError, TypeError):
        return ""
    ref = now or datetime.now().astimezone()
    # naive な日時は参照のタイムゾーンに合わせる（比較で例外を出さない）
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ref.tzinfo or timezone.utc)
    delta = ref - dt
    secs = int(delta.total_seconds())
    if secs < 0:
        secs = 0
    if secs < 60:
        return "たった今"
    minutes = secs // 60
    if minutes < 60:
        return f"{minutes} 分前"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} 時間前"
    days = hours // 24
    if days < 7:
        return f"{days} 日前"
    weeks = days // 7
    if weeks < 5:
        return f"{weeks} 週間前"
    months = days // 30
    if months < 12:
        return f"{max(1, months)} ヶ月前"
    return f"{days // 365} 年前"


def filter_items(items: List[Dict[str, str]], query: str) -> List[Dict[str, str]]:
    """履歴をクエリで絞り込む（テキスト部分一致・大文字小文字無視）。空クエリなら全件。

    Windows の履歴はテキストのみ（アプリ名メタデータを持たない）ため、Mac の
    「テキスト or アプリ名」からテキスト一致だけに簡素化している。
    """
    q = (query or "").strip().lower()
    if not q:
        return list(items)
    return [e for e in items if q in str(e.get("text", "")).lower()]


def _two_lines(text: str, f: QFont, width: float) -> List[str]:
    """文を幅 width で折り返し、最大 2 行にする（2 行目に収まらない分は … で省く）。"""
    text = " ".join(text.split())  # 改行は 1 行に詰める（Mac の lineLimit(2) と同じ見え方）
    if not text:
        return [""]
    layout = QTextLayout(text, f)
    option = QTextOption()
    option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
    layout.setTextOption(option)
    layout.beginLayout()
    lines: List[str] = []
    while len(lines) < 2:
        line = layout.createLine()
        if not line.isValid():
            break
        line.setLineWidth(width)
        start, length = line.textStart(), line.textLength()
        if len(lines) == 1:
            rest = text[start:]
            lines.append(QFontMetricsF(f).elidedText(rest, Qt.TextElideMode.ElideRight, width))
        else:
            lines.append(text[start:start + length].rstrip())
    layout.endLayout()
    return lines or [""]


class _HistoryRow(QAbstractButton):
    """履歴 1 行（本文 2 行まで＋相対時刻＋コピーの印）。クリックでコピー。

    Mac 版の行と同じ並び: 本文（12px）の下に、左に相対時刻、右にコピーの印。
    コピーした直後の 1.5 秒は印の代わりに「コピーしました」を出す。
    """

    PAD_H = 14
    PAD_V = 8
    GAP = 3
    COPIED_SEC = 1.5

    def __init__(self, entry: Dict[str, str], last: bool = False, parent=None) -> None:
        super().__init__(parent)
        self._text = str(entry.get("text", ""))
        self._time = relative_time(str(entry.get("date", "")))
        self._last = last
        self._copied = False
        self._hover = False
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setMouseTracking(True)
        self.setToolTip(self._text if len(self._text) > 60 else "")
        sp = self.sizePolicy()
        sp.setHeightForWidth(True)
        self.setSizePolicy(sp)

    @staticmethod
    def _body_font() -> QFont:
        return theme.font(12)

    @staticmethod
    def _small_font() -> QFont:
        return theme.font(10)

    def _lines(self, width: float) -> List[str]:
        return _two_lines(self._text, self._body_font(), max(10.0, width - self.PAD_H * 2))

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        body = QFontMetricsF(self._body_font()).lineSpacing() * len(self._lines(width))
        small = QFontMetricsF(self._small_font()).lineSpacing()
        return int(self.PAD_V * 2 + body + self.GAP + small + 0.999)

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(320, self.heightForWidth(320))

    def show_copied(self) -> None:
        """コピーしたことを 1.5 秒だけ行に出す。"""
        self._copied = True
        self.update()
        QTimer.singleShot(int(self.COPIED_SEC * 1000), self._clear_copied)

    def _clear_copied(self) -> None:
        self._copied = False
        self.update()

    def enterEvent(self, event) -> None:  # noqa: N802
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = theme.palette()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self._hover:
            painter.fillRect(self.rect(), p.row_hover)

        body_font = self._body_font()
        small_font = self._small_font()
        body_fm = QFontMetricsF(body_font)
        small_fm = QFontMetricsF(small_font)
        x = float(self.PAD_H)
        y = float(self.PAD_V)
        painter.setFont(body_font)
        painter.setPen(p.text)
        for line in self._lines(self.width()):
            painter.drawText(QPointF(x, y + body_fm.ascent()), line)
            y += body_fm.lineSpacing()
        y += self.GAP

        right = self.width() - self.PAD_H
        painter.setFont(small_font)
        painter.setPen(p.text_secondary)
        painter.drawText(QPointF(x, y + small_fm.ascent()), self._time)
        icon_size = 11.0
        icon_y = y + (small_fm.lineSpacing() - icon_size) / 2
        if self._copied:
            label = "コピーしました"
            label_w = small_fm.horizontalAdvance(label)
            painter.setPen(p.ok)
            painter.drawText(QPointF(right - label_w, y + small_fm.ascent()), label)
            icons.paint(painter, "check", QRectF(right - label_w - icon_size - 4, icon_y, icon_size, icon_size), p.ok)
        else:
            icons.paint(painter, "copy", QRectF(right - icon_size, icon_y, icon_size, icon_size), p.text_secondary)

        # 行どうしの区切り線（最後の行には引かない）
        if not self._last:
            painter.setPen(p.line)
            yb = self.height() - 0.5
            painter.drawLine(QPointF(self.PAD_H, yb), QPointF(self.width() - self.PAD_H, yb))
        painter.end()


class _IconButton(QAbstractButton):
    """アイコンだけの小さいボタン（閉じる・検索の消去）。フォーカスは取らない。"""

    def __init__(self, icon: str, size: int = 20, icon_size: float = 12.0, parent=None) -> None:
        super().__init__(parent)
        self._icon = icon
        self._icon_size = icon_size
        self._hover = False
        self.setFixedSize(size, size)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    def enterEvent(self, event) -> None:  # noqa: N802
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = theme.palette()
        painter = QPainter(self)
        s = self._icon_size
        rect = QRectF((self.width() - s) / 2, (self.height() - s) / 2, s, s)
        icons.paint(painter, self._icon, rect, p.text if self._hover else p.text_secondary)
        painter.end()


class _FooterButton(QAbstractButton):
    """フッタの「ホームを開く」（家のアイコン＋文字）。"""

    LABEL = "ホームを開く"

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._hover = False
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setToolTip("ホーム画面を開く")
        fm = QFontMetricsF(theme.font(12))
        self.setFixedSize(int(12 + 5 + fm.horizontalAdvance(self.LABEL) + 2), int(fm.height() + 4))

    def enterEvent(self, event) -> None:  # noqa: N802
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = theme.palette()
        color = p.accent_text if self._hover else p.text
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        f = theme.font(12)
        fm = QFontMetricsF(f)
        icons.paint(painter, "house", QRectF(0, (self.height() - 12) / 2, 12, 12), color)
        painter.setFont(f)
        painter.setPen(color)
        painter.drawText(QPointF(17, (self.height() - fm.height()) / 2 + fm.ascent()), self.LABEL)
        painter.end()


class _SearchField(QWidget):
    """検索欄（虫眼鏡＋入力＋消去ボタン）。角丸の薄い面に乗せる。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedHeight(30)
        row = QHBoxLayout(self)
        row.setContentsMargins(10, 0, 6, 0)
        row.setSpacing(6)
        row.addSpacing(13)  # 虫眼鏡は paintEvent で描く
        self.edit = QLineEdit(self)
        self.edit.setPlaceholderText("履歴を検索")
        self.edit.setFrame(False)
        self.edit.setFont(theme.font(12))
        row.addWidget(self.edit, 1)
        self.clear_button = _IconButton("close", size=18, icon_size=9.0, parent=self)
        self.clear_button.setToolTip("検索をクリア")
        self.clear_button.clicked.connect(self.edit.clear)
        # 文字が無いときも場所は取ったまま（入力の有無で欄の中身が動かないようにする）
        sp = self.clear_button.sizePolicy()
        sp.setRetainSizeWhenHidden(True)
        self.clear_button.setSizePolicy(sp)
        self.clear_button.setVisible(False)
        self.edit.textChanged.connect(lambda t: self.clear_button.setVisible(bool(t)))
        row.addWidget(self.clear_button)
        self.apply_theme()

    def apply_theme(self) -> None:
        p = theme.palette()
        self.edit.setStyleSheet(
            "QLineEdit { border: none; background: transparent; padding: 0;"
            f" color: {p.text.name()}; selection-background-color: {rgba(SIGNAL, 0.35).name(QColor.NameFormat.HexArgb)}; }}"
        )
        pal = self.edit.palette()
        pal.setColor(pal.ColorRole.PlaceholderText, p.text_secondary)
        self.edit.setPalette(pal)
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = theme.palette()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect()), 8, 8)
        fill = QColor(p.text)
        fill.setAlphaF(0.06)
        painter.fillPath(path, fill)
        icons.paint(painter, "search", QRectF(10, (self.height() - 11) / 2, 11, 11), p.text_secondary)
        painter.end()


class _Divider(QWidget):
    """島の中の横線（Mac の Divider().opacity(0.5) 相当）。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedHeight(1)

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.fillRect(self.rect(), theme.palette().line)
        painter.end()


class _PanelIsland(QWidget):
    """履歴パネルの島（面と縁）。中身はこの上に並べる。"""

    RADIUS = 18

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = theme.palette()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(rect, self.RADIUS, self.RADIUS)
        # 背景のぼかしが無いので、窓の地（不透明に近い）を先に敷いてから島の面を重ねる。
        # 設定画面で島が窓の地の上に乗っているのと同じ色になる。
        base = QColor(p.window)
        base.setAlphaF(0.97)
        painter.fillPath(path, base)
        painter.fillPath(path, p.island)
        gradient = QLinearGradient(0, rect.top(), 0, rect.bottom())
        gradient.setColorAt(0.0, p.island_rim_top)
        gradient.setColorAt(1.0, p.island_rim_bottom)
        painter.setPen(QPen(gradient, 1.0))
        painter.drawPath(path)
        # ライトでは白い縁が地に溶けるので、ごく薄い輪郭を足して形を見せる
        outline = QColor(p.line)
        painter.setPen(QPen(outline, 1.0))
        painter.drawPath(path)
        painter.end()


class SideNotchSlit(QWidget):
    """画面左端に常駐する細いスリット（フォーカスを奪わない・クリックで開閉トグル）。

    色は Mac 版と同じ: 黒の面（通常 0.7 / ホバー 0.85 / 録音中 0.92）に白い細い縁
    （0.28 / ホバー 0.4）。録音中は灯り（#FF5A1F）の縁とにじみで点灯する。
    外観（ライト／ダーク）では色を変えない（Mac も同じ）。
    """

    clicked = Signal()

    SLIT_HEIGHT = 96          # バー本体の高さ
    NORMAL_WIDTH = 2          # 通常時の幅
    HOVER_WIDTH = 10          # ホバー時の幅
    PANEL_WIDTH = 14          # ウィンドウ幅（ホバー拡大＋録音グローの余白を含む）
    V_MARGIN = 16             # 上下の余白（グローがはみ出す分）

    def __init__(self) -> None:
        super().__init__()
        self._hovering = False
        self._recording = False
        self._setup_window()
        self.resize(self.PANEL_WIDTH, self.SLIT_HEIGHT + self.V_MARGIN)

    def _setup_window(self) -> None:
        """フォーカスを奪わないフレームレス最前面ウィンドウ（ただしクリックは受ける）。"""
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setMouseTracking(True)
        self.setToolTip("履歴")

    def set_recording(self, recording: bool) -> None:
        """録音状態を反映する（点灯グロー。値が変わったときだけ再描画）。"""
        recording = bool(recording)
        if recording == self._recording:
            return
        self._recording = recording
        self.update()

    def enterEvent(self, event) -> None:  # noqa: N802
        self._hovering = True
        self.update()

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hovering = False
        self.update()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        w = self.HOVER_WIDTH if self._hovering else self.NORMAL_WIDTH
        h = self.SLIT_HEIGHT
        y = (self.height() - h) / 2
        bar_rect = QRectF(0, y, w, h)  # 左端に密着
        radius = min(w, 4)

        # 録音中は灯りのにじみ（透過ウィンドウでは影エフェクトが効かないため自前で重ねる）
        if self._recording:
            for i in range(1, 5):
                glow = bar_rect.adjusted(-i, -i, i * 2, i)
                path = QPainterPath()
                path.addRoundedRect(glow, radius + i, radius + i)
                painter.fillPath(path, rgba(SIGNAL, max(0.0, 0.24 - i * 0.05)))

        alpha = 0.92 if self._recording else (0.85 if self._hovering else 0.7)
        path = QPainterPath()
        path.addRoundedRect(bar_rect, radius, radius)
        painter.fillPath(path, QColor(0, 0, 0, int(alpha * 255)))
        if self._recording:
            border = rgba(SIGNAL, 0.9)
        else:
            border = QColor(255, 255, 255, int((0.4 if self._hovering else 0.28) * 255))
        pen = painter.pen()
        pen.setColor(border)
        pen.setWidthF(0.75)
        painter.setPen(pen)
        painter.drawPath(path)
        painter.end()


class SideNotchHistoryPanel(QWidget):
    """クリックで開く履歴パネル（見出し＋検索＋一覧＋ホームを開く）。

    窓は島より一回り大きく、まわりの透明な余白に島の影を描く（Mac の .padding(12) と同じ考え）。
    """

    open_home_requested = Signal()
    deactivated = Signal()

    PANEL_WIDTH = 320
    PANEL_HEIGHT = 440
    MARGIN = 16  # 影を描く余白

    def __init__(self, history) -> None:
        super().__init__()
        self._history = history
        self._query = ""
        self._setup_window()
        self.resize(self.PANEL_WIDTH + self.MARGIN * 2, self.PANEL_HEIGHT + self.MARGIN * 2)
        self._build_ui()
        Theme.instance().changed.connect(self._apply_theme)

    def _setup_window(self) -> None:
        """フレームレス最前面ウィンドウ。検索欄へ入力するためフォーカスは受ける。"""
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

    def island_rect(self) -> QRectF:
        """島の外形（窓の座標）。"""
        return QRectF(self.MARGIN, self.MARGIN, self.PANEL_WIDTH, self.PANEL_HEIGHT)

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(self.MARGIN, self.MARGIN, self.MARGIN, self.MARGIN)
        self._island = _PanelIsland(self)
        outer.addWidget(self._island)

        root = QVBoxLayout(self._island)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # 見出し（時計のアイコン＋「履歴」＋閉じる）
        header = QWidget()
        hl = QHBoxLayout(header)
        hl.setContentsMargins(14, 12, 10, 12)
        hl.setSpacing(6)
        self._header_icon = QLabel()
        self._header_icon.setFixedSize(13, 13)
        hl.addWidget(self._header_icon)
        self._title = QLabel("履歴")
        self._title.setFont(theme.font(14, QFont.Weight.DemiBold))
        hl.addWidget(self._title)
        hl.addStretch()
        close_btn = _IconButton("close", size=22, icon_size=11.0)
        close_btn.setToolTip("閉じる")
        close_btn.clicked.connect(self.hide)
        hl.addWidget(close_btn)
        root.addWidget(header)

        # 検索欄
        self._search_field = _SearchField()
        self._search = self._search_field.edit
        self._search.textChanged.connect(self._on_query_changed)
        search_wrap = QWidget()
        swl = QVBoxLayout(search_wrap)
        swl.setContentsMargins(14, 0, 14, 10)
        swl.addWidget(self._search_field)
        root.addWidget(search_wrap)

        root.addWidget(_Divider())

        # 一覧（スクロール）
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        self._scroll.viewport().setAutoFillBackground(False)
        self._list_container = QWidget()
        self._list_container.setAutoFillBackground(False)
        self._list_container.setStyleSheet("background: transparent;")
        self._list_layout = QVBoxLayout(self._list_container)
        self._list_layout.setContentsMargins(0, 0, 0, 0)
        self._list_layout.setSpacing(0)
        self._list_layout.addStretch()
        self._scroll.setWidget(self._list_container)
        root.addWidget(self._scroll, 1)

        root.addWidget(_Divider())

        # フッタ（「ホームを開く」）
        footer = QWidget()
        fl = QHBoxLayout(footer)
        fl.setContentsMargins(14, 12, 14, 12)
        home_btn = _FooterButton()
        home_btn.clicked.connect(self.open_home_requested.emit)
        fl.addWidget(home_btn)
        fl.addStretch()
        root.addWidget(footer)

        self._apply_theme()

    def _apply_theme(self) -> None:
        """外観が変わったら文字色と一覧を塗り直す。"""
        p = theme.palette()
        self._title.setStyleSheet(f"color: {p.text.name()}; background: transparent;")
        pix = _icon_pixmap("history", 13, p.text_secondary, self.devicePixelRatioF())
        self._header_icon.setPixmap(pix)
        self._search_field.apply_theme()
        self._rebuild_list()
        self.update()

    def _on_query_changed(self, text: str) -> None:
        self._query = text
        self._rebuild_list()

    def refresh(self) -> None:
        """履歴ストアの現在値で一覧を作り直す（開くたびに呼ぶ）。"""
        self._rebuild_list()

    def _rebuild_list(self) -> None:
        """フィルタ適用後の履歴で一覧ウィジェットを作り直す。"""
        # 既存の行（末尾の stretch を除く）を撤去する
        while self._list_layout.count() > 1:
            item = self._list_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                # 消すのはイベントループに戻ってからなので、その前に外して見えなくする
                # （外さないと古い行が新しい行の上に残って重なって見える）
                w.hide()
                w.setParent(None)
                w.deleteLater()

        items = filter_items(self._history.items(), self._query)
        if not items:
            empty = QLabel(
                "「%s」に一致する履歴はありません。" % self._query
                if self._query.strip()
                else "音声入力すると、ここに履歴が残ります。\nクリックでコピーできます。"
            )
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            empty.setWordWrap(True)
            empty.setFont(theme.font(11))
            empty.setContentsMargins(20, 150, 20, 0)
            empty.setStyleSheet(f"color: {theme.palette().text_secondary.name()}; background: transparent;")
            self._list_layout.insertWidget(0, empty)
            return

        for i, entry in enumerate(items):
            row = _HistoryRow(entry, last=(i == len(items) - 1))
            row.clicked.connect(
                lambda _=False, r=row, text=str(entry.get("text", "")): self._copy(text, r)
            )
            self._list_layout.insertWidget(i, row)

    def _copy(self, text: str, row: Optional[_HistoryRow] = None) -> None:
        """履歴テキストをクリップボードへコピーする（フォーカスは奪わない）。"""
        if not text:
            return
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(text)
        if row is not None:
            row.show_copied()

    def changeEvent(self, event) -> None:  # noqa: N802
        """ウィンドウが非アクティブになったら（外側クリック）閉じる合図を出す。"""
        from PySide6.QtCore import QEvent

        if event.type() == QEvent.Type.ActivationChange and not self.isActiveWindow():
            self.deactivated.emit()
        super().changeEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        """Esc で閉じる。"""
        if event.key() == Qt.Key.Key_Escape:
            self.hide()
            return
        super().keyPressEvent(event)

    def paintEvent(self, _event) -> None:  # noqa: N802
        """島の下に影を描く（島そのものは _PanelIsland が描く）。"""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        theme.paint_shadow(
            painter,
            self.island_rect(),
            radius=_PanelIsland.RADIUS,
            blur=12,
            dy=4,
            alpha=theme.palette().shadow_alpha,
        )
        painter.end()


def _icon_pixmap(name: str, size: int, color: QColor, dpr: float):
    """アイコンを画像にする（QLabel に載せる用）。"""
    pix = QPixmap(int(size * dpr), int(size * dpr))
    pix.setDevicePixelRatio(dpr)
    pix.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pix)
    icons.paint(painter, name, QRectF(0, 0, size, size), color)
    painter.end()
    return pix


class SideNotch(QObject):
    """スリットと履歴パネルの生成・配置・状態連動を束ねるコントローラ。

    app.py が所有し、録音状態（status_changed）と side_notch_enabled を橋渡しする。
    「ホームを開く」は open_home_requested を app へ中継する。外観は theme に追従する。
    """

    open_home_requested = Signal()

    # 外側クリック（非アクティブ化）で閉じた直後のスリット再クリックが即開き直すのを防ぐ猶予
    _REOPEN_GUARD_SEC = 0.25

    def __init__(self, history, enabled: bool = True) -> None:
        super().__init__()
        self._history = history
        self._enabled = enabled
        self._last_closed_at = 0.0

        self._slit = SideNotchSlit()
        self._slit.clicked.connect(self.toggle_panel)
        self._panel: Optional[SideNotchHistoryPanel] = None

        # 画面構成が変わったら位置を置き直す
        screen = QGuiApplication.primaryScreen()
        if screen is not None:
            screen.availableGeometryChanged.connect(lambda _r: self._reposition_all())

        self.set_enabled(enabled)

    # ---- 外部 API ----

    def set_enabled(self, enabled: bool) -> None:
        """表示トグルの反映。ON でスリットを出し、OFF で履歴パネルごと隠す。"""
        self._enabled = bool(enabled)
        if self._enabled:
            self._position_slit()
            self._slit.show()
        else:
            self._close_panel()
            self._slit.hide()

    def set_recording(self, recording: bool) -> None:
        """録音状態をスリットの点灯へ反映する。"""
        self._slit.set_recording(recording)

    def set_status(self, state: str) -> None:
        """app の status_changed を受けて録音グローを更新する。"""
        self.set_recording(str(state).startswith("recording"))

    # ---- 開閉 ----

    def toggle_panel(self) -> None:
        """スリットクリックで開閉をトグルする（再クリックの往復を _REOPEN_GUARD_SEC で吸収）。"""
        if self._panel is not None and self._panel.isVisible():
            self._close_panel()
            return
        # 直前に非アクティブ化で閉じたばかりなら開き直さない（スリット再クリックの往復対策）
        if time.monotonic() - self._last_closed_at < self._REOPEN_GUARD_SEC:
            return
        self.open_panel()

    def open_panel(self) -> None:
        """履歴パネルを開く。"""
        if not self._enabled:
            return
        if self._panel is None:
            self._make_panel()
        self._panel.refresh()
        self._position_panel()
        self._panel.show()
        self._panel.raise_()
        self._panel.activateWindow()  # 検索欄へ入力できるようにする
        self._panel._search.setFocus()

    def _close_panel(self) -> None:
        if self._panel is not None and self._panel.isVisible():
            self._panel.hide()
        self._last_closed_at = time.monotonic()

    def _make_panel(self) -> None:
        self._panel = SideNotchHistoryPanel(self._history)
        self._panel.open_home_requested.connect(self._on_open_home)
        self._panel.deactivated.connect(self._close_panel)

    def _on_open_home(self) -> None:
        """「ホームを開く」でパネルを閉じてホームを開く。"""
        self._close_panel()
        self.open_home_requested.emit()

    # ---- 配置 ----

    def _position_slit(self) -> None:
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            return
        geo = screen.availableGeometry()
        x = geo.left()
        y = geo.center().y() - self._slit.height() // 2
        self._slit.move(x, y)

    def _position_panel(self) -> None:
        if self._panel is None:
            return
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            return
        geo = screen.availableGeometry()
        margin = self._panel.MARGIN
        # 島の左端をスリットのすぐ右に置く（窓は影の余白ぶん外へはみ出してよい）
        x = geo.left() + self._slit.NORMAL_WIDTH + 6 - margin
        unclamped_y = geo.center().y() - self._panel.height() // 2
        y = min(max(unclamped_y, geo.top() - margin), geo.bottom() - self._panel.height() + margin)
        self._panel.move(x, y)

    def _reposition_all(self) -> None:
        if self._enabled and self._slit.isVisible():
            self._position_slit()
        if self._panel is not None and self._panel.isVisible():
            self._position_panel()
