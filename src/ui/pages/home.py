"""
設定ウィンドウの「ダッシュボード」ページ

Mac 版 UI/HomeView.swift と同じ並び・同じ文言で、入力の実績・API の利用料金・マイクテスト・
セットアップの導線・アプリ別の使用状況・最近の履歴を見せる。

恒久要件として、数字の桁・履歴の件数・メッセージの有無が変わっても、カードの高さと
下に続く部品の位置が 1px も動かないように作る（行数を先に確保し、出し入れは
setVisible ではなく文字を空にするか QStackedWidget の切り替えで行う）。

ページは窓が開いたときに初めて作られるので、__init__ では部品を組むだけにして、
ファイルやキーチェーンを読む重い処理は refresh() で行う。

Mac にあって、ここに出さないもの:
- 更新ピル（Mac の Sparkle 更新）・「推定」の表示（Windows の料金記録に推定分が無い）
"""

from datetime import datetime
from typing import Callable, List, Optional

from PySide6.QtCore import QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ...core import api_pricing, engines
from ..charts import BarChart, LevelMeter, MeterBar
from ..components import Card, CapsuleButton, MessageLabel, ScrollPage, Segmented, Text
from .. import icons, theme
from ..theme import Theme
from .context import PageContext


# ---------------------------------------------------------------------------
# 表記（Mac の HomeView と同じ規則）
# ---------------------------------------------------------------------------

# 最近の履歴に出す件数（Mac と同じ）。高さもこの件数ぶん先に確保する
HISTORY_ROWS = 8
# アプリ別の使用状況に出す件数（Mac と同じ上位 5 件）。高さもこの件数ぶん先に確保する
APP_ROWS = 5
# 料金の内訳に出す行数の上限。行が増えても高さを変えないため、この行数ぶんを先に確保する
BREAKDOWN_ROWS = 6
# 直近の料金の推移の日数（Mac と同じ）
COST_DAYS = 30
# マイクテストのレベルを読む間隔（ミリ秒）。30fps あれば動きは滑らかで、UI スレッドも詰まらない
MIC_POLL_MS = 33
# 「コピーしました」を出しておく時間（ミリ秒）
COPIED_MS = 1500

# 期間切替（この期間）の選択肢と日数。Mac の home.periodDays と同じ値
_PERIODS = (("今日", 1), ("今週", 7))
# 内訳の期間切替。Mac の home.apiCostPeriod と同じ値（既定は今月）
_COST_PERIODS = (("今日", "today"), ("今月", "month"), ("累計", "all"))
# 料金の表示切替。Mac の ApiCostMode と同じ値（既定は定価）
_COST_MODES = (("定価（無料枠も含む）", api_pricing.MODE_LIST), ("実際に払った分", api_pricing.MODE_PAID))


def format_duration(seconds: float) -> str:
    """
    時間の表記（Mac の HomeView と同じ）。

    Args:
        seconds: 秒数

    Returns:
        3600 秒以上は「X時間Y分」、60 秒以上は「X分Y秒」、それ未満は「X秒」
    """
    s = max(0, int(seconds))
    if s >= 3600:
        return f"{s // 3600}時間{s % 3600 // 60}分"
    if s >= 60:
        return f"{s // 60}分{s % 60}秒"
    return f"{s}秒"


def saved_time_message(seconds: float) -> str:
    """
    節約できた時間を、身近なものに言い換えた一言（Mac の HomeView と同じ段階と文言）。

    Args:
        seconds: 推定節約時間（秒）
    """
    s = max(0.0, float(seconds))
    if s >= 86400:
        return f"まるっと {int(s // 86400)} 日ぶんの時間"
    if s >= 28800:
        return f"ぐっすり睡眠 {int(s // 28800)} 回ぶん"
    if s >= 7200:
        return f"映画 {int(s // 7200)} 本ぶんの尺"
    if s >= 1800:
        return f"通勤 {int(s // 1800)} 回ぶんの移動時間"
    if s >= 180:
        return f"カップ麺 {int(s // 180)} 個ぶんの待ち時間"
    return "これから時間が積み上がっていきます"


def saved_time_icon(seconds: float) -> str:
    """
    節約できた時間の一言に添えるアイコンの名前（saved_time_message と同じ段階）。

    Args:
        seconds: 推定節約時間（秒）
    """
    s = max(0.0, float(seconds))
    for threshold, name in ((86400, "sun"), (28800, "bed"), (7200, "film"), (1800, "tram"), (180, "cup")):
        if s >= threshold:
            return name
    return "hourglass"


def relative_time(date_str: str, now: Optional[datetime] = None) -> str:
    """
    履歴の日時を「3 分前」のような相対表記にする（Mac の RelativeDateTimeFormatter ja_JP に寄せる）。

    Args:
        date_str: ISO 8601 の日時（末尾 Z・タイムゾーン無しも受ける）
        now: 基準の時刻（テスト用。省略時は現在）

    Returns:
        相対表記。読めない値はそのまま返す
    """
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return str(date_str)
    if dt.tzinfo is None:
        # タイムゾーンの無い旧形式はローカル時刻として扱う（history_sync.parse_iso_date と同じ）
        dt = dt.astimezone()
    now = now or datetime.now().astimezone()
    sec = max(0, int((now - dt).total_seconds()))
    for unit, name in ((31536000, "年"), (2592000, "か月"), (604800, "週間"), (86400, "日"), (3600, "時間"), (60, "分")):
        if sec >= unit:
            return f"{sec // unit} {name}前"
    return f"{sec} 秒前"


def _usage_amount(row: dict) -> str:
    """
    内訳 1 行の数量（音声は時間、LLM はトークン数）。Mac の HomeView と同じ表記。

    Args:
        row: ApiUsageStore.breakdown() の 1 行
    """
    if row.get("input_tokens", 0) or row.get("output_tokens", 0):
        return f"入力 {row['input_tokens']:,} / 出力 {row['output_tokens']:,} トークン"
    audio = float(row.get("audio_seconds", 0.0))
    billed = float(row.get("billed_audio_seconds", 0.0))
    # 最低課金秒数で伸びたときだけ、課金対象の時間を添える（1 秒未満の差は丸めの誤差とみなす）
    if billed - audio >= 1.0:
        return f"{format_duration(audio)}（課金対象 {format_duration(billed)}）"
    return format_duration(audio)


# ---------------------------------------------------------------------------
# このページの小部品（共通化の候補）
# ---------------------------------------------------------------------------


class _FitText(Text):
    """
    幅が足りないときだけ文字を小さくして 1 行に収める大きい数字。

    高さは元の大きさで固定する。桁が増えても行の高さが変わらず、カードの外へもはみ出さないため。
    """

    _MIN_SIZE = 14

    def __init__(self, text: str = "", size: int = 28, weight: QFont.Weight = QFont.Weight.Bold) -> None:
        """
        Args:
            text: 表示する文字
            size: 本来の文字の大きさ（ピクセル）
            weight: 太さ
        """
        super().__init__(text, size=size, weight=weight)
        self._base_size = size
        self._weight = weight
        self.setFixedHeight(QFontMetrics(theme.font(size, weight)).height())
        self.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)

    def sizeHint(self) -> QSize:  # noqa: N802
        """本来の大きさで描いたときの幅を返す（縮める前の希望の幅）。"""
        metrics = QFontMetrics(theme.font(self._base_size, self._weight))
        return QSize(metrics.horizontalAdvance(self.text()) + 2, self.height())

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        """どこまでも縮められる（縮めた分は文字を小さくして収める）。"""
        return QSize(0, self.height())

    def setText(self, text: str) -> None:  # noqa: N802
        """文字を変えたら、収まる大きさを決め直す。"""
        super().setText(text)
        self.updateGeometry()
        self._fit()

    def resizeEvent(self, event) -> None:  # noqa: N802
        """幅が変わったら、収まる大きさを決め直す。"""
        super().resizeEvent(event)
        self._fit()

    def _fit(self) -> None:
        """いまの幅に収まる文字の大きさを選ぶ（本来の大きさより大きくはしない）。"""
        need = QFontMetrics(theme.font(self._base_size, self._weight)).horizontalAdvance(self.text())
        size = self._base_size
        if need > 0 and self.width() > 0 and need > self.width():
            size = max(self._MIN_SIZE, int(self._base_size * self.width() / need))
        if size != self._size:
            # Text は大きさを QSS で指定しているので、_size を変えて色ごと付け直す
            self._size = size
            self.setFont(theme.font(size, self._weight))
            self._refresh()


class _Hairline(QWidget):
    """カードの中の 1px の区切り線。"""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(1)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        Theme.instance().changed.connect(self.update)

    def paintEvent(self, _event) -> None:  # noqa: N802
        """線を描く。"""
        QPainter(self).fillRect(self.rect(), theme.palette().line)


def _index_of(options, value, fallback: int) -> int:
    """
    (表示名, 値) の並びから、値の位置を返す。

    Args:
        options: (表示名, 値) の並び
        value: 探す値（保存されていた設定値）
        fallback: 見つからないときの位置（設定が壊れていても既定の表示で開く）
    """
    for index, (_label, option) in enumerate(options):
        if option == value:
            return index
    return fallback


class _IconNote(QWidget):
    """
    小さいアイコンつきの短い文を横に並べた 1 行（回数・録音時間など）。

    高さは 1 行ぶんで固定。幅が足りないときは最後の文を「…」で切り、折り返さない。
    """

    _ICON_GAP = 4    # アイコンと文のすき間
    _ITEM_GAP = 12   # 文と次のアイコンのすき間

    def __init__(self, size: int = 11, weight: QFont.Weight = QFont.Weight.Normal, role: str = "secondary") -> None:
        """
        Args:
            size: 文字の大きさ
            weight: 太さ
            role: 色の役割（"primary" は本文の色、ほかは控えめな色）
        """
        super().__init__()
        self._items: List[tuple] = []
        self._font = theme.font(size, weight)
        self._role = role
        self.setFixedHeight(QFontMetrics(self._font).height())
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        Theme.instance().changed.connect(self.update)

    def set_items(self, items) -> None:
        """
        並べる中身を変える。

        Args:
            items: (アイコンの名前, 文) の並び
        """
        self._items = list(items)
        self.updateGeometry()
        self.update()

    def text(self) -> str:
        """文だけをつないで返す（テスト・読み上げ用）。"""
        return " · ".join(text for _name, text in self._items)

    def _icon_size(self) -> float:
        """アイコンの一辺（文字の高さに合わせる）。"""
        return float(QFontMetrics(self._font).ascent())

    def sizeHint(self) -> QSize:  # noqa: N802
        """全部を切らずに並べたときの幅を返す。"""
        metrics = QFontMetrics(self._font)
        width = 0.0
        for index, (_name, text) in enumerate(self._items):
            width += self._icon_size() + self._ICON_GAP + metrics.horizontalAdvance(text)
            if index:
                width += self._ITEM_GAP
        return QSize(int(width) + 2, self.height())

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        """どこまでも縮められる（縮めた分は文を切る）。"""
        return QSize(0, self.height())

    def paintEvent(self, _event) -> None:  # noqa: N802
        """アイコンと文を左から順に描く。"""
        p = theme.palette()
        color = p.text if self._role == "primary" else p.text_secondary
        painter = QPainter(self)
        painter.setFont(self._font)
        metrics = QFontMetrics(self._font)
        icon = self._icon_size()
        h = float(self.height())
        x = 0.0
        for name, text in self._items:
            if x + icon > self.width():
                break
            icons.paint(painter, name, QRectF(x, (h - icon) / 2, icon, icon), color)
            x += icon + self._ICON_GAP
            room = self.width() - x
            if room <= 0:
                break
            shown = metrics.elidedText(text, Qt.TextElideMode.ElideRight, int(room))
            painter.setPen(color)
            painter.drawText(QRectF(x, 0.0, room, h), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, shown)
            x += metrics.horizontalAdvance(shown) + self._ITEM_GAP


def _big_row(value: _FitText, unit: str = "") -> QHBoxLayout:
    """
    大きい数字と、その右下の単位を並べた行を作る。

    Args:
        value: 大きい数字の部品
        unit: 単位（空なら付けない）
    """
    row = QHBoxLayout()
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(4)
    row.addWidget(value, 1 if not unit else 0, Qt.AlignmentFlag.AlignBottom)
    if unit:
        label = Text(unit, size=13, role="secondary")
        # 数字のベースラインに単位を揃える（大きい数字の下の余白ぶん持ち上げる）
        label.setContentsMargins(0, 0, 0, 5)
        row.addWidget(label, 0, Qt.AlignmentFlag.AlignBottom)
        row.addStretch(1)
    return row


def _head_row(title: QWidget, right: Optional[QWidget] = None) -> QHBoxLayout:
    """
    見出しと、その右端の部品（切替など）を並べた行を作る。

    Args:
        title: 左の見出し
        right: 右端に置く部品（無ければ見出しだけ）
    """
    row = QHBoxLayout()
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(8)
    row.addWidget(title, 0, Qt.AlignmentFlag.AlignVCenter)
    row.addStretch(1)
    if right is not None:
        row.addWidget(right, 0, Qt.AlignmentFlag.AlignVCenter)
    return row


class _BreakdownRow(QWidget):
    """料金の内訳 1 行（上段「プロバイダー · モデル」、下段「用途 · 数量 · 回数」、右に料金）。"""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 4)
        layout.setSpacing(12)
        left = QVBoxLayout()
        left.setContentsMargins(0, 0, 0, 0)
        left.setSpacing(2)
        self.title = MessageLabel(lines=1, size=13)
        self.detail = MessageLabel(lines=1, size=11)
        left.addWidget(self.title)
        left.addWidget(self.detail)
        layout.addLayout(left, 1)
        self.amount = Text("", size=13, weight=QFont.Weight.DemiBold)
        self.amount.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        layout.addWidget(self.amount, 0, Qt.AlignmentFlag.AlignTop)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)

    def show_row(self, row: Optional[dict], mode: str) -> None:
        """
        1 行ぶんを出す（None なら空にする。行の高さはそのまま）。

        Args:
            row: ApiUsageStore.breakdown() の 1 行
            mode: 料金の表示モード
        """
        if row is None:
            self.title.show_message("", "primary")
            self.detail.show_message("")
            self.amount.setText("")
            return
        self.title.show_message(f"{row['provider_label']} · {row['model']}", "primary")
        self.detail.show_message(f"{row['purpose_label']} · {_usage_amount(row)} · {row['requests']:,} 回")
        if api_pricing.is_free_tier(mode, row["provider"]):
            self.amount.setText("無料枠")
            self.amount.set_role("secondary")
        elif row.get("usd") is None:
            self.amount.setText("単価未確認")
            self.amount.set_role("secondary")
        else:
            self.amount.setText(api_pricing.format_usd(row["usd"]))
            self.amount.set_role("primary")


class _HistoryRow(QWidget):
    """
    最近の履歴 1 行。行のどこを押しても全文をクリップボードへコピーする。

    Signals:
        clicked: 押されたとき（引数なし。コピーは持ち主がする）
    """

    clicked = Signal()

    def __init__(self, last: bool, parent: Optional[QWidget] = None) -> None:
        """
        Args:
            last: 最後の行なら True（下に区切り線を引かない）
            parent: 親ウィジェット
        """
        super().__init__(parent)
        self._last = last
        self._hover = False
        self._active = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(2)
        self.body = MessageLabel(lines=2, size=12)
        self.body.set_role("primary")
        layout.addWidget(self.body)
        foot = QHBoxLayout()
        foot.setContentsMargins(0, 0, 0, 0)
        self.when = MessageLabel(lines=1, size=11)
        self.copied = Text("", size=11, role="ok")
        self.copied.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        # 「コピーしました」の出し入れで左の時刻の幅が変わらないよう、幅を先に確保する
        self.copied.setFixedWidth(QFontMetrics(self.copied.font()).horizontalAdvance("コピーしました") + 4)
        foot.addWidget(self.when, 1)
        foot.addWidget(self.copied, 0)
        layout.addLayout(foot)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        Theme.instance().changed.connect(self.update)

    def set_active(self, active: bool) -> None:
        """中身のある行かどうか（空の行は押せず、手の形のカーソルも出さない）。"""
        self._active = active
        self.setCursor(Qt.CursorShape.PointingHandCursor if active else Qt.CursorShape.ArrowCursor)
        self.update()

    def enterEvent(self, event) -> None:  # noqa: N802
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        """中身のある行を左クリックで離したら知らせる。"""
        if self._active and event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit()
        super().mouseReleaseEvent(event)

    def paintEvent(self, _event) -> None:  # noqa: N802
        """マウスが載っている間の面と、行の間の区切り線を描く。"""
        p = theme.palette()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self._active and self._hover:
            path = QPainterPath()
            path.addRoundedRect(QRectF(self.rect()), 8, 8)
            painter.fillPath(path, p.row_hover)
        if not self._last:
            painter.fillRect(10, self.height() - 1, self.width() - 20, 1, p.line)
        if self._active and not self.copied.text():
            # 普段は右端にコピーの印を出す（「コピーしました」が出ている間は同じ場所を譲る）
            slot = self.copied.geometry()
            size = 12.0
            icons.paint(
                painter,
                "copy",
                QRectF(slot.right() + 1 - size, self.when.geometry().center().y() + 0.5 - size / 2, size, size),
                p.text_secondary,
            )


class _AppRow(QWidget):
    """
    アプリ別の使用状況 1 行（アイコン・名前・文字数・割合バー）。Mac の appUsageRow と同じ形。

    中身が無い行は何も描かない（高さは保つので、件数が増えても下の部品が動かない）。
    """

    ICON = 22
    HEIGHT = 28

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(self.HEIGHT)
        self._name = ""
        self._count = ""
        self._ratio = 0.0
        self._icon = None
        Theme.instance().changed.connect(self.update)

    def set_entry(self, name: str = "", count: str = "", ratio: float = 0.0, icon=None) -> None:
        """
        行の中身を入れる（引数なしで空にする）。

        Args:
            name: アプリの表示名
            count: 右に出す文字数の表記（例: 「1234 文字」）
            ratio: 最も使ったアプリを 1.0 とした割合
            icon: アプリのアイコン（QPixmap。無ければ点線の四角を描く）
        """
        self._name, self._count, self._ratio, self._icon = name, count, max(0.0, min(1.0, ratio)), icon
        self.update()

    def name(self) -> str:
        return self._name

    def paintEvent(self, _event) -> None:  # noqa: N802
        if not self._name:
            return
        p = theme.palette()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        size = self.ICON
        icon_rect = QRectF(0, (self.height() - size) / 2, size, size)
        if self._icon is not None and not self._icon.isNull():
            clip = QPainterPath()
            clip.addRoundedRect(icon_rect, size * 0.22, size * 0.22)
            painter.save()
            painter.setClipPath(clip)
            painter.drawPixmap(icon_rect.toRect(), self._icon)
            painter.restore()
        else:
            # Mac の app.dashed に当たる点線の角丸四角
            pen = painter.pen()
            pen.setColor(p.text_secondary)
            pen.setWidthF(1.2)
            pen.setStyle(Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.drawRoundedRect(icon_rect.adjusted(2.5, 2.5, -2.5, -2.5), 4, 4)

        left = size + 10
        width = self.width() - left
        name_font = QFont(self.font())
        name_font.setPixelSize(13)
        count_font = QFont(self.font())
        count_font.setPixelSize(11)
        count_metrics = QFontMetrics(count_font)
        count_width = count_metrics.horizontalAdvance(self._count)
        name_metrics = QFontMetrics(name_font)
        text_h = name_metrics.height()
        bar_h = 6
        top = (self.height() - (text_h + 3 + bar_h)) / 2
        painter.setFont(name_font)
        painter.setPen(p.text)
        name_rect = QRectF(left, top, max(0, width - count_width - 8), text_h)
        painter.drawText(
            name_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            name_metrics.elidedText(self._name, Qt.TextElideMode.ElideRight, int(name_rect.width())),
        )
        painter.setFont(count_font)
        painter.setPen(p.text_secondary)
        painter.drawText(
            QRectF(left, top, width, text_h), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, self._count
        )
        bar = QRectF(left, top + text_h + 3, width, bar_h)
        track = QPainterPath()
        track.addRoundedRect(bar, bar_h / 2, bar_h / 2)
        painter.fillPath(track, p.line)
        fill = QRectF(bar.left(), bar.top(), max(6.0, bar.width() * self._ratio), bar_h)
        path = QPainterPath()
        path.addRoundedRect(fill, bar_h / 2, bar_h / 2)
        color = QColor(p.accent)
        color.setAlphaF(0.75)
        painter.fillPath(path, color)


# ---------------------------------------------------------------------------
# ページ本体
# ---------------------------------------------------------------------------


class HomePage(QWidget):
    """
    ダッシュボード。

    Signals:
        navigate(str): 別ページへ移りたいとき（引数はページの key。例: "api_keys"）

    Attributes:
        ctx: 設定ページ共通の文脈
    """

    navigate = Signal(str)

    def __init__(self, ctx: PageContext, parent: Optional[QWidget] = None) -> None:
        """
        部品を組むだけで、データは読まない（読むのは refresh()）。

        Args:
            ctx: 設定ページ共通の文脈
            parent: 親ウィジェット
        """
        super().__init__(parent)
        self.ctx = ctx
        self._own_api_usage = None
        self._monitor = None
        self._history_texts: List[str] = [""] * HISTORY_ROWS
        # 「コピーしました」を消すタイマーが、後から押した行の表示まで消さないための世代
        self._copy_generation = 0

        self._mic_timer = QTimer(self)
        self._mic_timer.setInterval(MIC_POLL_MS)
        self._mic_timer.timeout.connect(self._poll_mic)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.scroll = ScrollPage()
        outer.addWidget(self.scroll)
        body = self.scroll.body
        # Mac のカード間隔 16
        body.setSpacing(16)

        self._build_header(body)
        self._build_stats(body)
        self._build_costs(body)
        self._build_tools(body)
        self._build_apps(body)
        self._build_history(body)
        self.scroll.finish()

    # ----- 組み立て -----

    def _build_header(self, body: QVBoxLayout) -> None:
        """見出し「ダッシュボード」と、今日の入力の一言。"""
        head = QVBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(2)
        head.addWidget(Text("ダッシュボード", size=17, weight=QFont.Weight.Bold))
        self.subtitle = MessageLabel(lines=1, size=12)
        head.addWidget(self.subtitle)
        body.addLayout(head)

    def _build_stats(self, body: QVBoxLayout) -> None:
        """統計カード（上段に累計入力と節約できた時間、下段にこの期間）。"""
        top = QHBoxLayout()
        top.setSpacing(12)

        total = Card()
        total.body.addWidget(Text("累計入力", size=11, role="secondary"))
        self.total_chars = _FitText("0")
        total.body.addLayout(_big_row(self.total_chars, "文字"))
        self.total_sub = _IconNote()
        total.body.addWidget(self.total_sub)
        total.body.addSpacing(4)
        total.body.addWidget(_Hairline())
        total.body.addSpacing(4)
        level_row = QHBoxLayout()
        level_row.setSpacing(8)
        self.level = Text("レベル 1", size=13, weight=QFont.Weight.DemiBold)
        self.level_next = MessageLabel(lines=1, size=11)
        self.level_next.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        level_row.addWidget(self.level, 0)
        level_row.addWidget(self.level_next, 1)
        total.body.addLayout(level_row)
        self.level_bar = MeterBar(0.0)
        total.body.addWidget(self.level_bar)
        total.body.addStretch(1)

        saved = Card()
        saved.body.addWidget(Text("節約できた時間", size=11, role="secondary"))
        self.saved_time = _FitText("0秒")
        saved.body.addLayout(_big_row(self.saved_time))
        self.saved_message = _IconNote()
        saved.body.addWidget(self.saved_message)
        saved.body.addStretch(1)

        top.addWidget(total, 1)
        top.addWidget(saved, 1)
        body.addLayout(top)

        period = Card()
        self.period_switch = Segmented(
            [label for label, _ in _PERIODS], current=_index_of(_PERIODS, self.ctx.get("home_period_days", 1), 0)
        )
        self.period_switch.currentChanged.connect(self._on_period_changed)
        period.body.addLayout(_head_row(Text("この期間", size=11, role="secondary"), self.period_switch))
        self.period_chars = _FitText("0")
        period.body.addLayout(_big_row(self.period_chars, "文字"))
        self.period_sub = _IconNote()
        period.body.addWidget(self.period_sub)
        body.addWidget(period)

        self.stats_cards = (total, saved, period)

    def _build_costs(self, body: QVBoxLayout) -> None:
        """API の利用料金（合計 3 枚・内訳・直近 30 日・注記）。"""
        self.cost_mode_switch = Segmented(
            [label for label, _ in _COST_MODES],
            current=_index_of(_COST_MODES, self.ctx.get("api_cost_mode", api_pricing.MODE_LIST), 0),
        )
        self.cost_mode_switch.currentChanged.connect(self._on_cost_mode_changed)
        body.addLayout(_head_row(Text("API の利用料金", size=13, weight=QFont.Weight.DemiBold), self.cost_mode_switch))

        trio = QHBoxLayout()
        trio.setSpacing(12)
        self.cost_totals = {}
        for key, label in (("today", "今日"), ("month", "今月"), ("all_time", "累計")):
            card = Card()
            card.body.addWidget(Text(label, size=11, role="secondary"))
            value = _FitText("$0.00")
            card.body.addLayout(_big_row(value))
            # 「単価未確認を含む」の行は、出ないときも高さを確保しておく（3 枚の高さを揃えたまま動かさない）
            note = MessageLabel(lines=1, size=11)
            card.body.addWidget(note)
            card.body.addStretch(1)
            trio.addWidget(card, 1)
            self.cost_totals[key] = (card, value, note)
        body.addLayout(trio)

        breakdown = Card()
        self.cost_period_switch = Segmented(
            [label for label, _ in _COST_PERIODS],
            current=_index_of(_COST_PERIODS, self.ctx.get("home_api_cost_period", "month"), 1),
        )
        self.cost_period_switch.currentChanged.connect(self._on_cost_period_changed)
        breakdown.body.addLayout(_head_row(Text("内訳", size=11, role="secondary"), self.cost_period_switch))
        # 行の一覧と「利用はありません」を同じ枠で切り替える（枠の高さは行 BREAKDOWN_ROWS 本ぶんで固定）
        self.breakdown_stack = QStackedWidget()
        rows_page = QWidget()
        rows_layout = QVBoxLayout(rows_page)
        rows_layout.setContentsMargins(0, 0, 0, 0)
        rows_layout.setSpacing(0)
        self.breakdown_rows = [_BreakdownRow() for _ in range(BREAKDOWN_ROWS)]
        for row in self.breakdown_rows:
            rows_layout.addWidget(row)
        rows_layout.addStretch(1)
        empty_page = QWidget()
        empty_layout = QVBoxLayout(empty_page)
        empty_layout.setContentsMargins(0, 8, 0, 0)
        empty_layout.addWidget(Text("この期間に API の利用はありません。", size=11, role="secondary", wrap=True))
        empty_layout.addStretch(1)
        self.breakdown_stack.addWidget(rows_page)
        self.breakdown_stack.addWidget(empty_page)
        self.breakdown_stack.setFixedHeight(rows_page.sizeHint().height())
        breakdown.body.addWidget(self.breakdown_stack)
        body.addWidget(breakdown)

        chart_card = Card()
        self.cost_month_total = Text("合計 $0.00", size=11, role="secondary")
        chart_card.body.addLayout(_head_row(Text("直近 30 日", size=11, role="secondary"), self.cost_month_total))
        self.cost_chart = BarChart()
        chart_card.body.addWidget(self.cost_chart)
        axis = QHBoxLayout()
        axis.setContentsMargins(0, 0, 0, 0)
        self.cost_first_day = Text("", size=11, role="secondary")
        axis.addWidget(self.cost_first_day)
        axis.addStretch(1)
        axis.addWidget(Text("今日", size=11, role="secondary"))
        chart_card.body.addLayout(axis)
        body.addWidget(chart_card)

        notes = QVBoxLayout()
        notes.setContentsMargins(0, 0, 0, 0)
        notes.setSpacing(2)
        notes.addWidget(Text(
            "使用量 × 各社の公開単価から計算した推定で、実際の請求額とは異なることがあります。",
            size=11, role="secondary", wrap=True,
        ))
        notes.addWidget(Text(
            f"単価は {api_pricing.CHECKED_ON} 時点（Gemini は有料枠の単価で計算）。",
            size=11, role="secondary", wrap=True,
        ))
        # 「実際に払った分」のときだけ出す注記。切り替えても下がずれないよう 2 行ぶんを常に確保する
        self.paid_note = MessageLabel(lines=2, size=11)
        notes.addWidget(self.paid_note)
        body.addLayout(notes)

        self.cost_cards = (breakdown, chart_card)

    def _build_tools(self, body: QVBoxLayout) -> None:
        """ツール（マイクテスト・セットアップガイド）を横に並べる。"""
        row = QHBoxLayout()
        row.setSpacing(12)

        mic = Card()
        self.mic_button = CapsuleButton("テスト", kind="link", size="small")
        # 「テスト」と「停止」で幅が変わらないよう、長い方の幅で固定する
        widths = []
        for label in ("テスト", "停止"):
            self.mic_button.setText(label)
            widths.append(self.mic_button.sizeHint().width())
        self.mic_button.setText("テスト")
        self.mic_button.setFixedWidth(max(widths))
        self.mic_button.clicked.connect(self.toggle_mic_test)
        mic_title = _IconNote(size=13, weight=QFont.Weight.DemiBold, role="primary")
        mic_title.set_items((("mic", "マイクテスト"),))
        mic.body.addLayout(_head_row(mic_title, self.mic_button))
        self.mic_caption = MessageLabel(lines=2, size=11)
        mic.body.addWidget(self.mic_caption)
        self.mic_meter = LevelMeter()
        mic.body.addWidget(self.mic_meter)
        mic.body.addStretch(1)

        setup = Card()
        setup_title = _IconNote(size=13, weight=QFont.Weight.DemiBold, role="primary")
        setup_title.set_items((("sparkles", "セットアップガイド"),))
        setup.body.addWidget(setup_title)
        setup.body.addWidget(Text("使い方をはじめから、もう一度見られます。", size=11, role="secondary", wrap=True))
        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.setSpacing(12)
        self.guide_button = CapsuleButton("もう一度見る", kind="link", size="small")
        self.guide_button.clicked.connect(self.ctx.open_onboarding_requested.emit)
        buttons.addWidget(self.guide_button)
        # 鍵が 1 つも無いときだけ出す導線。出し入れで周りが動かないよう、空の面との切り替えにする
        self.api_key_slot = QStackedWidget()
        self.api_key_button = CapsuleButton("API キーを設定", kind="primary", size="small")
        self.api_key_button.clicked.connect(lambda: self.navigate.emit("api_keys"))
        self.api_key_slot.addWidget(QWidget())
        self.api_key_slot.addWidget(self.api_key_button)
        self.api_key_slot.setFixedSize(self.api_key_button.sizeHint())
        buttons.addWidget(self.api_key_slot)
        buttons.addStretch(1)
        setup.body.addLayout(buttons)
        setup.body.addStretch(1)

        row.addWidget(mic, 1)
        row.addWidget(setup, 1)
        body.addLayout(row)
        self.tool_cards = (mic, setup)
        self._show_mic_idle()

    def _build_apps(self, body: QVBoxLayout) -> None:
        """アプリ別の使用状況（上位 5 件ぶんの高さを確保した面）。"""
        body.addWidget(Text("アプリ別の使用状況", size=13, weight=QFont.Weight.DemiBold))
        card = Card(padding=14)
        self.apps_stack = QStackedWidget()
        rows_page = QWidget()
        rows_layout = QVBoxLayout(rows_page)
        rows_layout.setContentsMargins(0, 0, 0, 0)
        rows_layout.setSpacing(8)
        self.app_rows = []
        for _ in range(APP_ROWS):
            row = _AppRow()
            rows_layout.addWidget(row)
            self.app_rows.append(row)
        empty_page = QWidget()
        empty_layout = QVBoxLayout(empty_page)
        empty_layout.setContentsMargins(0, 0, 0, 0)
        empty = Text("使うほど、ここにアプリごとの使用状況がたまります。", size=11, role="secondary", wrap=True)
        empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.addWidget(empty)
        self.apps_stack.addWidget(rows_page)
        self.apps_stack.addWidget(empty_page)
        self.apps_stack.setFixedHeight(_AppRow.HEIGHT * APP_ROWS + 8 * (APP_ROWS - 1))
        card.body.addWidget(self.apps_stack)
        body.addWidget(card)
        self.apps_card = card
        # 実行ファイルのアイコンは読むのが重いので、パスごとに 1 回だけ読む
        self._app_icons = {}

    def _build_history(self, body: QVBoxLayout) -> None:
        """最近の履歴（8 行ぶんの高さを確保した面）。"""
        body.addWidget(Text("最近の履歴", size=13, weight=QFont.Weight.DemiBold))
        card = Card(padding=6)
        self.history_stack = QStackedWidget()
        rows_page = QWidget()
        rows_layout = QVBoxLayout(rows_page)
        rows_layout.setContentsMargins(0, 0, 0, 0)
        rows_layout.setSpacing(0)
        self.history_rows = []
        for index in range(HISTORY_ROWS):
            row = _HistoryRow(last=index == HISTORY_ROWS - 1)
            row.clicked.connect(lambda i=index: self._copy_history(i))
            rows_layout.addWidget(row)
            self.history_rows.append(row)
        rows_layout.addStretch(1)
        empty_page = QWidget()
        empty_layout = QVBoxLayout(empty_page)
        empty_layout.setContentsMargins(10, 10, 10, 10)
        empty_layout.addWidget(Text(
            "音声入力すると、ここに最近の履歴が残ります（クリックでコピーできます）。",
            size=11, role="secondary", wrap=True,
        ))
        empty_layout.addStretch(1)
        self.history_stack.addWidget(rows_page)
        self.history_stack.addWidget(empty_page)
        self.history_stack.setFixedHeight(rows_page.sizeHint().height())
        card.body.addWidget(self.history_stack)
        body.addWidget(card)
        self.history_card = card

    # ----- 読み直し -----

    def refresh(self) -> None:
        """数字と一覧を読み直す（表示されるたび・履歴が届いたときに窓から呼ばれる）。"""
        self._refresh_stats()
        self._refresh_period()
        self._refresh_costs()
        self._refresh_setup()
        self._refresh_apps()
        self._refresh_history()

    def _refresh_stats(self) -> None:
        """見出しの一言・累計入力・節約できた時間。"""
        stats = self.ctx.stats
        snap = stats.snapshot() if stats is not None else {}
        today = stats.daily_series(1)[0]["characters"] if stats is not None else 0
        # Mac と同じく、見出しの今日の文字数は 3 桁区切りにしない
        self.subtitle.show_message(
            f"今日はここまで {today} 文字を入力しました" if today > 0 else "今日はまだ入力していません"
        )
        level = int(snap.get("level", 1))
        self.total_chars.setText(f"{int(snap.get('total_characters', 0)):,}")
        self.total_sub.set_items((
            ("mic", f"{int(snap.get('total_sessions', 0)):,} 回"),
            ("waveform", format_duration(snap.get("total_recording_seconds", 0.0))),
        ))
        self.level.setText(f"レベル {level}")
        self.level_next.show_message(f"あと {int(snap.get('xp_to_next_level', 0)):,} 文字で Lv.{level + 1}")
        self.level_bar.set_fraction(float(snap.get("level_progress", 0.0)))
        saved = float(snap.get("saved_seconds", 0.0))
        self.saved_time.setText(format_duration(saved))
        self.saved_message.set_items(((saved_time_icon(saved), saved_time_message(saved)),))

    def _on_period_changed(self, index: int) -> None:
        """期間（今日・今週）を切り替えたら、選んだ値を覚えて数え直す。"""
        self.ctx.commit({"home_period_days": _PERIODS[index][1]})
        self._refresh_period()

    def _on_cost_mode_changed(self, index: int) -> None:
        """料金の数え方を切り替えたら、選んだ値を覚えて数え直す（トレイの料金行も同じ値を読む）。"""
        self.ctx.commit({"api_cost_mode": _COST_MODES[index][1]})
        self._refresh_costs()

    def _on_cost_period_changed(self, index: int) -> None:
        """内訳の期間を切り替えたら、選んだ値を覚えて数え直す。"""
        self.ctx.commit({"home_api_cost_period": _COST_PERIODS[index][1]})
        self._refresh_breakdown()

    def _refresh_period(self) -> None:
        """この期間（今日・今週）の文字数・録音時間・回数。"""
        days = _PERIODS[self.period_switch.current_index()][1]
        series = self.ctx.stats.daily_series(days) if self.ctx.stats is not None else []
        chars = sum(day["characters"] for day in series)
        seconds = sum(day["recording_seconds"] for day in series)
        sessions = sum(day["sessions"] for day in series)
        self.period_chars.setText(f"{chars:,}")
        self.period_sub.set_items((("waveform", format_duration(seconds)), ("mic", f"{sessions:,} 回")))

    def _api_usage(self):
        """料金の記録を返す（窓から渡されていなければ、既定の保存先を読むだけのものを 1 つ作る）。"""
        if self.ctx.api_usage is not None:
            return self.ctx.api_usage
        if self._own_api_usage is None:
            from ...core.api_usage import ApiUsageStore

            self._own_api_usage = ApiUsageStore()
        return self._own_api_usage

    def _cost_mode(self) -> str:
        """いま選んでいる料金の表示モード。"""
        return _COST_MODES[self.cost_mode_switch.current_index()][1]

    def _refresh_costs(self) -> None:
        """合計 3 枚・内訳・直近 30 日・注記（表示モードを変えたときもここ）。"""
        usage = self._api_usage()
        mode = self._cost_mode()
        sums = {
            "today": usage.today_summary(mode),
            "month": usage.month_summary(mode),
            "all_time": usage.all_time_summary(mode),
        }
        for key, (_card, value, note) in self.cost_totals.items():
            value.setText(api_pricing.format_usd(sums[key]["usd"]))
            note.show_message("単価未確認を含む" if sums[key]["has_unpriced"] else "")

        daily = usage.daily_series(COST_DAYS, mode)
        self.cost_chart.set_values([day["usd"] for day in daily])
        self.cost_month_total.setText(f"合計 {api_pricing.format_usd(sum(day['usd'] for day in daily))}")
        if daily:
            first = datetime.strptime(daily[0]["day"], "%Y-%m-%d")
            self.cost_first_day.setText(f"{first.month}/{first.day}")
        self.paid_note.show_message(
            "「実際に払った分」は OpenAI・Soniox・Microsoft・Google（Gemini）だけを計上し、"
            "ほかは無料枠として 0 ドルにしています。"
            if mode == api_pricing.MODE_PAID else ""
        )
        self._refresh_breakdown()

    def _refresh_breakdown(self) -> None:
        """内訳（期間を変えたときもここ）。"""
        usage = self._api_usage()
        mode = self._cost_mode()
        period = _COST_PERIODS[self.cost_period_switch.current_index()][1]
        now = datetime.now().astimezone()
        today = now.strftime("%Y-%m-%d")
        include: Callable[[str], bool]
        if period == "today":
            include = lambda d: d == today  # noqa: E731
        elif period == "all":
            include = lambda d: True  # noqa: E731
        else:
            include = lambda d: d.startswith(today[:7])  # noqa: E731
        rows = usage.breakdown(include, mode)
        self.breakdown_stack.setCurrentIndex(0 if rows else 1)
        for index, widget in enumerate(self.breakdown_rows):
            widget.show_row(rows[index] if index < len(rows) else None, mode)

    def _refresh_setup(self) -> None:
        """鍵が 1 つも無いときだけ「API キーを設定」を出す（キーチェーンを読むのでここで）。"""
        self.api_key_slot.setCurrentIndex(0 if engines.has_any_api_key() else 1)

    def _history_entries(self) -> list:
        """ローカル履歴に、同期がオンなら Mac 側の履歴も混ぜて返す（新しい順。現行の履歴ページと同じ読み方）。"""
        local = self.ctx.history.items() if self.ctx.history is not None else []
        sync = self.ctx.history_sync
        if sync is not None and sync.enabled:
            return sync.merged_items(local)
        return local

    def _refresh_apps(self) -> None:
        """アプリ別の使用状況（累計文字数の上位 5 件）。"""
        stats = self.ctx.stats
        apps = stats.top_apps(APP_ROWS) if stats is not None else []
        apps = [a for a in apps if a.get("characters", 0) > 0]
        self.apps_stack.setCurrentIndex(0 if apps else 1)
        top = max(1, apps[0]["characters"]) if apps else 1
        for index, row in enumerate(self.app_rows):
            if index >= len(apps):
                row.set_entry()
                continue
            app = apps[index]
            row.set_entry(
                app.get("name") or app["id"], f"{app['characters']} 文字", app["characters"] / top,
                self._app_icon(app.get("path", "")),
            )

    def _app_icon(self, path: str):
        if not path or self.ctx.platform is None:
            return None
        if path not in self._app_icons:
            self._app_icons[path] = self.ctx.platform.app_icon(path)
        return self._app_icons[path]

    def _refresh_history(self) -> None:
        """最近の履歴 8 件。"""
        entries = self._history_entries()[:HISTORY_ROWS]
        self.history_stack.setCurrentIndex(0 if entries else 1)
        self._copy_generation += 1
        for index, row in enumerate(self.history_rows):
            entry = entries[index] if index < len(entries) else None
            row.copied.setText("")
            if entry is None:
                self._history_texts[index] = ""
                row.body.show_message("", "primary")
                row.when.show_message("")
                row.setToolTip("")
                row.set_active(False)
                continue
            text = entry.get("text", "")
            device = entry.get("device", "windows")
            # Mac の記録には印を付ける（Mac 側は Windows の記録に「[Windows] 」を付けるのと対になる）
            prefix = "" if device == "windows" else ("[Mac] " if device == "mac" else f"[{device}] ")
            self._history_texts[index] = text
            # 改行は 1 行に詰めて 2 行の枠に収める（全文はツールチップとコピーで読める）
            row.body.show_message(prefix + " ".join(text.split()), "primary")
            row.setToolTip(text)
            row.when.show_message(relative_time(entry.get("date", "")))
            row.set_active(True)

    def _copy_history(self, index: int) -> None:
        """
        履歴の全文をクリップボードへコピーし、その行に「コピーしました」を少しの間出す。

        Args:
            index: 行の番号
        """
        text = self._history_texts[index]
        if not text:
            return
        QApplication.clipboard().setText(text)
        self._copy_generation += 1
        generation = self._copy_generation
        for i, row in enumerate(self.history_rows):
            row.copied.setText("コピーしました" if i == index else "")
        QTimer.singleShot(COPIED_MS, lambda: self._clear_copied(generation))

    def _clear_copied(self, generation: int) -> None:
        """「コピーしました」を消す（その後に別の行を押していたら、その表示は残す）。"""
        if generation != self._copy_generation:
            return
        for row in self.history_rows:
            row.copied.setText("")

    # ----- マイクテスト -----

    def _make_monitor(self):
        """レベルだけを測るモニタを作る（sounddevice を開くまで読み込まない。テストで差し替える）。"""
        from ...core.mic_monitor import MicLevelMonitor

        return MicLevelMonitor()

    def toggle_mic_test(self) -> None:
        """マイクテストの開始・停止を切り替える（録音はせず、レベルを見るだけ）。"""
        if self._monitor is not None and self._monitor.is_running:
            self.stop_mic_test()
            return
        if self._monitor is None:
            self._monitor = self._make_monitor()
        if self._monitor.start(self.ctx.get("audio_input_device", "default") or "default"):
            self.mic_button.setText("停止")
            self.mic_caption.show_message("話すと、下のバーが動きます。")
            self._mic_timer.start()
        else:
            # 開けないときは黙らずに知らせる（欄の高さは変わらない）
            self.mic_caption.show_message("マイクを開けませんでした。", "danger")

    def stop_mic_test(self) -> None:
        """マイクテストを止めてストリームを閉じる（ページが隠れたときにも必ず呼ぶ）。"""
        self._mic_timer.stop()
        if self._monitor is not None:
            self._monitor.stop()
        self._show_mic_idle()

    def _show_mic_idle(self) -> None:
        """止まっているときの表示に戻す。"""
        self.mic_button.setText("テスト")
        self.mic_caption.show_message("「テスト」を押して、マイクが拾えているか確かめます。")
        self.mic_meter.set_fraction(0.0)

    def _poll_mic(self) -> None:
        """いまのレベルをバーへ写す（PortAudio のスレッドが書いた値を読むだけなので待たない）。"""
        if self._monitor is not None:
            self.mic_meter.set_fraction(self._monitor.read_level())

    def hideEvent(self, event) -> None:  # noqa: N802
        """ページを離れる・窓を隠すときは、マイクを開いたままにしない。"""
        self.stop_mic_test()
        super().hideEvent(event)
