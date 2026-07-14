"""
サイドノッチ（画面左端の履歴スリット → クリックで履歴パネル）モジュール

Mac 版 SideNotch.swift と対の Windows 実装:
- 常駐スリット: 画面左端・垂直中央に、フォーカスを奪わない細い黒バー（うっすら外枠）。
  ホバーで少し太くなり、録音中はアクセント色のグローで点灯する。side_notch_enabled で表示切替。
- クリックで履歴パネル（ガラス島）が左端から出る。上部に検索フィールド、行クリックでコピー、
  下部に「ホームを開く」。外側クリック（パネルの非アクティブ化）or スリット再クリックで閉じる。
  全消去はホーム側だけに置く（ここには置かない）。
- 背景は ⑤ の DWM アクリル/ブラー（対応 OS）＋ QSS 疑似ガラスのフォールバック。

最重要の制約: スリットは絶対にフォーカスを奪ってはならない（貼り付け先＝音声入力の入力先を
維持する）。履歴パネルは検索フィールドへ入力するためフォーカスを取るが、これはユーザーが
自分でスリットをクリックして履歴を見る操作なので許容する（口述中ではない）。
"""

import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

from PySide6.QtCore import QObject, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QGuiApplication, QPainter, QPainterPath
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..platform.windows import acrylic

# アクセント色（styles.py の ACCENT と一致）。録音中グロー・フォーカスに使う
_ACCENT_DARK = "#0A84FF"
_ACCENT_LIGHT = "#007AFF"


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


class _HistoryRow(QPushButton):
    """履歴 1 行（テキスト 2 行省略＋相対時刻）。クリックでコピー。"""

    def __init__(self, entry: Dict[str, str], is_dark: bool, parent=None) -> None:
        super().__init__(parent)
        self._text = str(entry.get("text", ""))
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFlat(True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        primary = "#F5F5F7" if is_dark else "#1D1D1F"
        secondary = "#A8AEBD" if is_dark else "#6C6C74"
        hover = "rgba(255,255,255,0.06)" if is_dark else "rgba(0,0,0,0.05)"
        self.setStyleSheet(
            f"QPushButton {{ border: none; border-radius: 8px; text-align: left; padding: 0; }}"
            f"QPushButton:hover {{ background-color: {hover}; }}"
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(3)
        text_label = QLabel(self._text)
        text_label.setWordWrap(True)
        text_label.setStyleSheet(f"color: {primary}; font-size: 12px; background: transparent;")
        text_label.setMaximumHeight(38)  # 約 2 行に抑える
        text_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        layout.addWidget(text_label)
        time_label = QLabel(relative_time(str(entry.get("date", ""))))
        time_label.setStyleSheet(f"color: {secondary}; font-size: 10px; background: transparent;")
        time_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        layout.addWidget(time_label)


class SideNotchSlit(QWidget):
    """画面左端に常駐する細いスリット（フォーカスを奪わない・クリックで開閉トグル）。"""

    clicked = Signal()

    SLIT_HEIGHT = 96          # バー本体の高さ
    NORMAL_WIDTH = 2          # 通常時の幅
    HOVER_WIDTH = 10          # ホバー時の幅
    PANEL_WIDTH = 14          # ウィンドウ幅（ホバー拡大＋録音グローの余白を含む）
    V_MARGIN = 16             # 上下の余白（グローがはみ出す分）

    def __init__(self, is_dark: bool = False) -> None:
        super().__init__()
        self._is_dark = is_dark
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

    def set_dark(self, is_dark: bool) -> None:
        self._is_dark = is_dark
        self.update()

    def set_recording(self, recording: bool) -> None:
        """録音状態を反映する（点灯グロー。値が変わったときだけ再描画）。"""
        recording = bool(recording)
        if recording == self._recording:
            return
        self._recording = recording
        self.update()

    def enterEvent(self, event) -> None:
        self._hovering = True
        self.update()

    def leaveEvent(self, event) -> None:
        self._hovering = False
        self.update()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()

    def _accent(self) -> QColor:
        return QColor(_ACCENT_DARK if self._is_dark else _ACCENT_LIGHT)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        w = self.HOVER_WIDTH if self._hovering else self.NORMAL_WIDTH
        h = self.SLIT_HEIGHT
        y = (self.height() - h) / 2
        bar_rect = QRectF(0, y, w, h)  # 左端に密着
        radius = min(w, 4)

        # 録音中はアクセント色のソフトグロー（透過ウィンドウでは影エフェクトが効かないため自前で重ねる）
        if self._recording:
            accent = self._accent()
            for i in range(1, 5):
                glow = bar_rect.adjusted(-i, -i, i * 2, i)
                path = QPainterPath()
                path.addRoundedRect(glow, radius + i, radius + i)
                accent.setAlpha(max(0, 60 - i * 12))
                painter.fillPath(path, accent)

        # バー本体（黒基調。録音中・ホバーで不透明度だけ上げる）
        alpha = 235 if self._recording else (217 if self._hovering else 179)
        path = QPainterPath()
        path.addRoundedRect(bar_rect, radius, radius)
        painter.fillPath(path, QColor(0, 0, 0, alpha))
        # うっすら外枠（録音中はアクセント寄り）
        if self._recording:
            border = self._accent()
            border.setAlpha(230)
        else:
            border = QColor(255, 255, 255, 102 if self._hovering else 71)
        pen = painter.pen()
        pen.setColor(border)
        pen.setWidthF(0.75)
        painter.setPen(pen)
        painter.drawPath(path)
        painter.end()


class SideNotchHistoryPanel(QWidget):
    """クリックで開く履歴パネル（検索＋一覧＋ホームを開く）。"""

    open_home_requested = Signal()
    deactivated = Signal()

    PANEL_WIDTH = 320
    PANEL_HEIGHT = 440

    def __init__(self, history, is_dark: bool = False) -> None:
        super().__init__()
        self._history = history
        self._is_dark = is_dark
        self._query = ""
        self._setup_window()
        self.resize(self.PANEL_WIDTH, self.PANEL_HEIGHT)
        self._build_ui()

    def _setup_window(self) -> None:
        """フレームレス最前面ウィンドウ。検索フィールドへ入力するためフォーカスは受ける。"""
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        # 背景はガラス（アクリル or 疑似）。paintEvent で角丸の半透明地を描く
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

    # ---- 色 ----
    def _primary(self) -> str:
        return "#F5F5F7" if self._is_dark else "#1D1D1F"

    def _secondary(self) -> str:
        return "#A8AEBD" if self._is_dark else "#6C6C74"

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ヘッダ（「履歴」＋ 閉じる ×）
        header = QWidget()
        hl = QHBoxLayout(header)
        hl.setContentsMargins(14, 12, 10, 10)
        title = QLabel("履歴")
        title.setStyleSheet(
            f"color: {self._primary()}; font-size: 14px; font-weight: 600; background: transparent;"
        )
        hl.addWidget(title)
        hl.addStretch()
        close_btn = QPushButton("✕")
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        close_btn.setFixedSize(24, 24)
        close_btn.setStyleSheet(
            f"QPushButton {{ border: none; color: {self._secondary()}; background: transparent; font-size: 12px; }}"
            "QPushButton:hover { color: #FF453A; }"
        )
        close_btn.clicked.connect(self.hide)
        hl.addWidget(close_btn)
        root.addWidget(header)

        # 検索フィールド
        self._search = QLineEdit()
        self._search.setPlaceholderText("履歴を検索")
        self._search.setClearButtonEnabled(True)
        field_bg = "rgba(255,255,255,0.08)" if self._is_dark else "rgba(0,0,0,0.05)"
        self._search.setStyleSheet(
            f"QLineEdit {{ border: none; border-radius: 8px; padding: 7px 10px; "
            f"color: {self._primary()}; background-color: {field_bg}; font-size: 12px; }}"
        )
        self._search.textChanged.connect(self._on_query_changed)
        search_wrap = QWidget()
        swl = QVBoxLayout(search_wrap)
        swl.setContentsMargins(14, 0, 14, 10)
        swl.addWidget(self._search)
        root.addWidget(search_wrap)

        # 一覧（スクロール）
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setStyleSheet("QScrollArea { background: transparent; } QWidget { background: transparent; }")
        self._list_container = QWidget()
        self._list_layout = QVBoxLayout(self._list_container)
        self._list_layout.setContentsMargins(8, 4, 8, 4)
        self._list_layout.setSpacing(2)
        self._list_layout.addStretch()
        self._scroll.setWidget(self._list_container)
        root.addWidget(self._scroll, 1)

        # フッタ（「ホームを開く」）
        footer = QWidget()
        fl = QHBoxLayout(footer)
        fl.setContentsMargins(14, 10, 14, 12)
        home_btn = QPushButton("🏠  ホームを開く")
        home_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        home_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        home_btn.setFlat(True)
        home_btn.setStyleSheet(
            f"QPushButton {{ border: none; color: {self._secondary()}; background: transparent; font-size: 12px; }}"
            f"QPushButton:hover {{ color: {self._primary()}; }}"
        )
        home_btn.clicked.connect(self.open_home_requested.emit)
        fl.addWidget(home_btn)
        fl.addStretch()
        root.addWidget(footer)

    def set_dark(self, is_dark: bool) -> None:
        self._is_dark = is_dark

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
            empty.setStyleSheet(f"color: {self._secondary()}; font-size: 11px; background: transparent;")
            self._list_layout.insertWidget(0, empty)
            return

        for i, entry in enumerate(items):
            row = _HistoryRow(entry, self._is_dark)
            row.clicked.connect(lambda _=False, text=str(entry.get("text", "")): self._copy(text))
            self._list_layout.insertWidget(i, row)

    def _copy(self, text: str) -> None:
        """履歴テキストをクリップボードへコピーする（フォーカスは奪わない）。"""
        if not text:
            return
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(text)

    def changeEvent(self, event) -> None:
        """ウィンドウが非アクティブになったら（外側クリック）閉じる合図を出す。"""
        from PySide6.QtCore import QEvent

        if event.type() == QEvent.Type.ActivationChange and not self.isActiveWindow():
            self.deactivated.emit()
        super().changeEvent(event)

    def keyPressEvent(self, event) -> None:
        """Esc で閉じる。"""
        if event.key() == Qt.Key.Key_Escape:
            self.hide()
            return
        super().keyPressEvent(event)

    def paintEvent(self, event) -> None:
        """角丸の半透明ガラス地を描く（アクリル非対応時の疑似ガラス／アクリル時は上乗せのティント）。"""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(0.5, 0.5, self.width() - 1, self.height() - 1)
        radius = 14.0
        path = QPainterPath()
        path.addRoundedRect(rect, radius, radius)
        if self._is_dark:
            painter.fillPath(path, QColor(30, 30, 32, 235))
            painter.setPen(QColor(255, 255, 255, 26))
        else:
            painter.fillPath(path, QColor(246, 246, 248, 235))
            painter.setPen(QColor(0, 0, 0, 20))
        painter.drawPath(path)
        painter.end()


class SideNotch(QObject):
    """スリットと履歴パネルの生成・配置・状態連動を束ねるコントローラ。

    app.py が所有し、録音状態（status_changed）と side_notch_enabled を橋渡しする。
    「ホームを開く」は open_home_requested を app へ中継する。
    """

    open_home_requested = Signal()

    # 外側クリック（非アクティブ化）で閉じた直後のスリット再クリックが即開き直すのを防ぐ猶予
    _REOPEN_GUARD_SEC = 0.25

    def __init__(self, history, is_dark: bool = False, enabled: bool = True) -> None:
        super().__init__()
        self._history = history
        self._is_dark = is_dark
        self._enabled = enabled
        self._last_closed_at = 0.0

        self._slit = SideNotchSlit(is_dark=is_dark)
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

    def set_dark(self, is_dark: bool) -> None:
        self._is_dark = is_dark
        self._slit.set_dark(is_dark)
        if self._panel is not None:
            self._panel.set_dark(is_dark)

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
        self._panel.set_dark(self._is_dark)
        self._panel.refresh()
        self._position_panel()
        self._panel.show()
        self._panel.raise_()
        self._panel.activateWindow()  # 検索フィールドへ入力できるようにする
        # 対応 OS では実アクリル/ブラーを背景に適用（非対応は paintEvent の疑似ガラス）
        acrylic.apply_blur(self._panel, dark=self._is_dark)
        self._panel._search.setFocus()

    def _close_panel(self) -> None:
        if self._panel is not None and self._panel.isVisible():
            self._panel.hide()
        self._last_closed_at = time.monotonic()

    def _make_panel(self) -> None:
        self._panel = SideNotchHistoryPanel(self._history, is_dark=self._is_dark)
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
        x = geo.left() + self._slit.NORMAL_WIDTH + 4  # スリットのすぐ右
        unclamped_y = geo.center().y() - self._panel.height() // 2
        y = min(max(unclamped_y, geo.top()), geo.bottom() - self._panel.height())
        self._panel.move(x, y)

    def _reposition_all(self) -> None:
        if self._enabled and self._slit.isVisible():
            self._position_slit()
        if self._panel is not None and self._panel.isVisible():
            self._position_panel()
