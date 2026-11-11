"""
メインウィンドウの骨格

左に浮いた島のサイドバー（ブランド行＋ページ一覧）、右に本文を置く。
見た目は Mac 版（SettingsView.swift の normalLayout）と同じ:
浮く島はサイドバーだけで、本文は窓の下地にそのまま載せる。

ページは「開いたとき初めて作る」。全ページを起動時に作ると窓が出るまでが遅くなるため、
登録するのは作り方（関数）だけにして、選ばれたときに 1 回だけ呼ぶ。
"""

import sys
from typing import Callable, Dict, List, Optional

from PySide6.QtCore import QEvent, QRectF, Qt, Signal
from PySide6.QtGui import QFont, QPainter, QPainterPath
from PySide6.QtWidgets import QApplication, QHBoxLayout, QScrollArea, QVBoxLayout, QWidget

from . import brand, theme
from .components import Backdrop, FadeStack, GlassIsland, NavList, Text
from .theme import Metrics, Theme


class BrandMark(QWidget):
    """
    サイドバー先頭のアプリアイコン（Mac 版と同じ画像）。

    外観でボーン地／カーボン地を切り替える。大きさ 30・角丸 7 も Mac の brandHeader と同じ。
    """

    SIZE = 30
    RADIUS = 7.0

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFixedSize(self.SIZE, self.SIZE)
        Theme.instance().changed.connect(self.update)

    def paintEvent(self, _event) -> None:  # noqa: N802
        """外観に合った画像を、角を丸めた枠の中に描く。"""
        pixmap = brand.app_icon_pixmap(theme.palette().dark, self.SIZE, self.devicePixelRatioF())
        if pixmap.isNull():
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        clip = QPainterPath()
        clip.addRoundedRect(QRectF(0.0, 0.0, float(self.SIZE), float(self.SIZE)), self.RADIUS, self.RADIUS)
        painter.setClipPath(clip)
        painter.drawPixmap(0, 0, pixmap)


class WindowShell(Backdrop):
    """
    メインウィンドウの骨格（サイドバー＋本文）。

    使い方: add_section / add_page で並びを作り、select で開くページを選ぶ。
    ページの中身（設定の読み書き）は各ページが自分で持つ。骨格は並べて切り替えるだけ。

    Signals:
        pageChanged(str): 開いているページが変わったとき（引数はページの key）
    """

    pageChanged = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._keys: List[str] = []                       # 並び順の key
        self._factories: Dict[str, Callable[[], QWidget]] = {}
        self._pages: Dict[str, QWidget] = {}             # 作り終えたページ

        self.setWindowIcon(brand.app_icon())
        self.setMinimumSize(Metrics.WINDOW_MIN_W, Metrics.WINDOW_MIN_H)
        self.resize(Metrics.WINDOW_DEFAULT_W, Metrics.WINDOW_DEFAULT_H)

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(Metrics.ISLAND_GAP)

        # 島の左と上下に下地を見せる（右は本文とのすき間）
        side_holder = QWidget()
        side_layout = QVBoxLayout(side_holder)
        side_layout.setContentsMargins(Metrics.WINDOW_PADDING, Metrics.WINDOW_PADDING, 0, Metrics.WINDOW_PADDING)
        self._sidebar = GlassIsland()
        self._sidebar.setFixedWidth(Metrics.SIDEBAR_W)
        side_layout.addWidget(self._sidebar)
        root.addWidget(side_holder)
        self.add_island(self._sidebar)

        island = QVBoxLayout(self._sidebar)
        island.setContentsMargins(0, 0, 0, 0)
        island.setSpacing(0)

        brand_row = QHBoxLayout()
        brand_row.setContentsMargins(14, 14, 14, 6)
        brand_row.setSpacing(8)
        brand_row.addWidget(BrandMark())
        brand_row.addWidget(Text("voicekey", size=13, weight=QFont.Weight.DemiBold))
        brand_row.addStretch(1)
        island.addLayout(brand_row)

        # 一覧が島の高さを超えたら中でスクロールする（スクロールバーは出さない。Mac と同じ）
        self._nav = NavList()
        nav_holder = QWidget()
        nav_holder.setAutoFillBackground(False)
        nav_layout = QVBoxLayout(nav_holder)
        nav_layout.setContentsMargins(8, 0, 8, 10)
        nav_layout.addWidget(self._nav)
        nav_layout.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.viewport().setAutoFillBackground(False)
        scroll.setStyleSheet("QScrollArea { background: transparent; }")
        scroll.setWidget(nav_holder)
        island.addWidget(scroll, 1)

        self._stack = FadeStack()
        root.addWidget(self._stack, 1)

        self._nav.currentChanged.connect(self._on_nav_changed)
        Theme.instance().changed.connect(self._apply_appearance)

    # ----- 並びを作る -----

    def add_section(self, title: str) -> None:
        """
        サイドバーに小見出しを足す（この後に足すページの上に出る）。

        Args:
            title: 小見出しの文字
        """
        self._nav.add_section(title)

    def add_page(self, key: str, title: str, factory: Callable[[], QWidget], icon: str = "") -> None:
        """
        ページを足す。中身は初めて開いたときに factory を呼んで作る。

        Args:
            key: ページを指す名前（select や pageChanged で使う）
            title: サイドバーに出す見出し
            factory: ページのウィジェットを作って返す関数
            icon: サイドバーのアイコンの名前（icons.names() のどれか）
        """
        self._keys.append(key)
        self._factories[key] = factory
        # 作るまでの間は空の板を置いておく（並びの番号をサイドバーと揃えるため）
        self._stack.addWidget(QWidget())
        self._nav.add_item(title, icon)

    # ----- 開く・調べる -----

    def keys(self) -> List[str]:
        """ページの key を並び順に返す。"""
        return list(self._keys)

    def current_key(self) -> str:
        """いま開いているページの key を返す（ページが無ければ空文字）。"""
        index = self._nav.current_index()
        return self._keys[index] if 0 <= index < len(self._keys) else ""

    def select(self, key: str, animate: bool = True) -> None:
        """
        ページを開く。

        Args:
            key: 開くページ（知らない key なら何もしない）
            animate: サイドバーの灯りを滑らせ、本文をふわっと出すなら True
        """
        if key not in self._factories:
            return
        index = self._keys.index(key)
        if index == self._nav.current_index():
            self._show(index, animate=False)
            return
        self._animate_next = animate
        self._nav.set_current_index(index, animate=animate)

    def page(self, key: str) -> Optional[QWidget]:
        """
        ページのウィジェットを返す（まだ作っていなければ作る）。

        Args:
            key: ページの key

        Returns:
            ページ。知らない key なら None
        """
        if key not in self._factories:
            return None
        return self._ensure(self._keys.index(key))

    def created_page(self, key: str) -> Optional[QWidget]:
        """作り終えているページだけを返す（まだ開かれていなければ None。作らない）。"""
        return self._pages.get(key)

    # ----- 内部 -----

    _animate_next = True

    def _ensure(self, index: int) -> QWidget:
        """index 番のページを（まだなら）作り、空の板と入れ替えて返す。"""
        key = self._keys[index]
        page = self._pages.get(key)
        if page is not None:
            return page
        page = self._factories[key]()
        placeholder = self._stack.widget(index)
        self._stack.removeWidget(placeholder)
        placeholder.deleteLater()
        self._stack.insertWidget(index, page)
        self._pages[key] = page
        return page

    def _show(self, index: int, animate: bool) -> None:
        """index 番のページを本文に出す。"""
        page = self._ensure(index)
        if self._stack.currentWidget() is not page:
            self._stack.show_index(index, animate=animate)

    def _on_nav_changed(self, index: int) -> None:
        """サイドバーで行が選ばれたとき。"""
        animate, self._animate_next = self._animate_next, True
        self._show(index, animate=animate)
        self.pageChanged.emit(self._keys[index])

    def _apply_appearance(self) -> None:
        """外観（ライト／ダーク）を標準部品とタイトルバーに反映する。"""
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(theme.app_qss())
        self._apply_titlebar()

    def _apply_titlebar(self) -> None:
        """タイトルバーの明暗を外観に合わせる（Windows のみ。ほかの OS では何もしない）。"""
        if sys.platform != "win32" or not self.isWindow():
            return
        from ..platform.windows.titlebar import set_titlebar_dark

        set_titlebar_dark(self, theme.palette().dark)

    def showEvent(self, event) -> None:  # noqa: N802
        """出る直前に、開いているページを作り、外観を合わせる。"""
        if self._keys:
            self._show(max(0, self._nav.current_index()), animate=False)
        self._apply_titlebar()
        super().showEvent(event)

    def changeEvent(self, event) -> None:  # noqa: N802
        """窓のハンドルが作り直されたら、タイトルバーの明暗を付け直す。"""
        if event.type() == QEvent.Type.WinIdChange:
            self._apply_titlebar()
        super().changeEvent(event)
