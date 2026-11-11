"""
数字を図で見せる部品

ダッシュボードの「レベルの進み具合」（横棒）と「直近 30 日の利用料金」（棒グラフ）を描く。
どちらも高さを固定する。値が 0 件でも最大でも、下に続く部品の位置が動かないようにするため。
"""

from typing import List, Optional, Sequence

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QSizePolicy, QWidget

from . import theme
from .theme import SIGNAL, Theme, rgba


class MeterBar(QWidget):
    """
    進み具合を見せる細い横棒（0.0〜1.0）。
    """

    HEIGHT = 5

    def __init__(self, fraction: float = 0.0, parent: Optional[QWidget] = None) -> None:
        """
        Args:
            fraction: 進み具合（0.0〜1.0）
            parent: 親ウィジェット
        """
        super().__init__(parent)
        self._fraction = 0.0
        self.setFixedHeight(self.HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.set_fraction(fraction)
        Theme.instance().changed.connect(self.update)

    def set_fraction(self, fraction: float) -> None:
        """
        進み具合を変える。

        Args:
            fraction: 0.0〜1.0（範囲の外は端に丸める）
        """
        self._fraction = max(0.0, min(1.0, float(fraction)))
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        """溝と、進んだ分を描く。"""
        p = theme.palette()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        w, h = float(self.width()), float(self.height())
        painter.setBrush(p.track_off)
        painter.drawRoundedRect(QRectF(0.0, 0.0, w, h), h / 2, h / 2)
        if self._fraction > 0.0:
            painter.setBrush(p.accent)
            # 進みがごく小さくても丸い端が潰れない幅を残す
            painter.drawRoundedRect(QRectF(0.0, 0.0, max(h, w * self._fraction), h), h / 2, h / 2)


class BarChart(QWidget):
    """
    日ごとの値を縦棒で並べる（左が古く、右が新しい）。

    目盛り・軸は描かない。いちばん大きい日を枠の高さいっぱいにして、形だけを見せる。
    """

    HEIGHT = 96
    _GAP = 3.0        # 棒と棒のすき間
    _MIN_BAR = 2.0    # 値が 0 の日にも残す高さ（「その日がある」ことを見せる）
    _MAX_BAR_W = 22.0

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._values: List[float] = []
        self.setFixedHeight(self.HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        Theme.instance().changed.connect(self.update)

    def set_values(self, values: Sequence[float]) -> None:
        """
        値を入れ替える。

        Args:
            values: 古い日から順の値（負の値は 0 として描く）
        """
        self._values = [max(0.0, float(value)) for value in values]
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        """棒を描く。"""
        count = len(self._values)
        if count == 0:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(rgba(SIGNAL, 0.78))
        w, h = float(self.width()), float(self.height())
        slot = w / count
        bar_w = max(1.0, min(self._MAX_BAR_W, slot - self._GAP))
        peak = max(self._values)
        for index, value in enumerate(self._values):
            bar_h = self._MIN_BAR if peak <= 0.0 else max(self._MIN_BAR, h * value / peak)
            x = index * slot + (slot - bar_w) / 2
            radius = min(bar_w, bar_h) / 2
            painter.drawRoundedRect(QRectF(x, h - bar_h, bar_w, bar_h), radius, radius)
