"""
線で描くアイコン

サイドバーなどに出す小さなアイコンを、フォントや画像を使わず線で描く。
Windows のアイコン用フォント（Segoe Fluent Icons）は Windows 10 に入っておらず、
「丸の中の数字」のような Mac と同じ形も持っていないため、形を自分で持つことにした。
どの OS・どの倍率でも同じ形になり、色は文字と同じ色をその場で受け取る。
"""

import math
from typing import Callable, Dict

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen

from . import theme

# 形は 16×16 のマス目で決め、描くときに枠の大きさへ伸び縮みさせる
_GRID = 16.0
# 線の太さ（マス目での値。Mac の SF Symbols の regular に近い太さ）
_STROKE = 1.25


def _grid(painter: QPainter, _color: QColor) -> None:
    """ダッシュボード（2×2 の四角）。"""
    for x, y in ((2.2, 2.2), (9.0, 2.2), (2.2, 9.0), (9.0, 9.0)):
        painter.drawRoundedRect(QRectF(x, y, 4.8, 4.8), 1.2, 1.2)


def _key(painter: QPainter, color: QColor) -> None:
    """API キー（鍵）。"""
    painter.drawEllipse(QPointF(5.6, 5.6), 3.4, 3.4)
    painter.drawLine(QPointF(8.1, 8.1), QPointF(13.6, 13.6))
    painter.drawLine(QPointF(11.2, 11.2), QPointF(9.7, 12.7))
    painter.drawLine(QPointF(13.0, 13.0), QPointF(11.6, 14.4))
    # 鍵の頭の穴
    painter.save()
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(color)
    painter.drawEllipse(QPointF(4.7, 4.7), 0.85, 0.85)
    painter.restore()


def _numbered(digit: str) -> Callable[[QPainter, QColor], None]:
    """丸の中に数字（録音キー 1・2）を描く関数を作る。"""

    def draw(painter: QPainter, _color: QColor) -> None:
        painter.drawEllipse(QPointF(8.0, 8.0), 6.2, 6.2)
        font = theme.font(8, QFont.Weight.DemiBold)
        font.setPixelSize(8)
        painter.setFont(font)
        painter.drawText(QRectF(0.0, 0.2, _GRID, _GRID), Qt.AlignmentFlag.AlignCenter, digit)

    return draw


def _book(painter: QPainter, _color: QColor) -> None:
    """ユーザー辞書（本）。"""
    painter.drawRoundedRect(QRectF(3.0, 2.0, 10.0, 12.0), 1.6, 1.6)
    painter.drawLine(QPointF(5.6, 2.2), QPointF(5.6, 13.8))
    painter.drawLine(QPointF(7.8, 5.6), QPointF(10.8, 5.6))
    painter.drawLine(QPointF(7.8, 8.2), QPointF(10.8, 8.2))


def _translate(painter: QPainter, _color: QColor) -> None:
    """翻訳して入力（吹き出しの中に「あ」）。"""
    path = QPainterPath()
    path.moveTo(4.4, 2.6)
    path.lineTo(11.6, 2.6)
    path.quadTo(14.2, 2.6, 14.2, 5.2)
    path.lineTo(14.2, 8.6)
    path.quadTo(14.2, 11.2, 11.6, 11.2)
    path.lineTo(7.4, 11.2)
    path.lineTo(4.6, 13.8)
    path.lineTo(4.6, 11.2)
    path.lineTo(4.4, 11.2)
    path.quadTo(1.8, 11.2, 1.8, 8.6)
    path.lineTo(1.8, 5.2)
    path.quadTo(1.8, 2.6, 4.4, 2.6)
    painter.drawPath(path)
    font = theme.font(6, QFont.Weight.DemiBold)
    font.setPixelSize(6)
    painter.setFont(font)
    painter.drawText(QRectF(1.8, 2.6, 12.4, 8.4), Qt.AlignmentFlag.AlignCenter, "あ")


def _people(painter: QPainter, _color: QColor) -> None:
    """議事録・会議（2 人）。"""
    painter.drawEllipse(QPointF(6.0, 5.2), 2.4, 2.4)
    front = QPainterPath()
    front.moveTo(1.6, 13.4)
    front.cubicTo(1.6, 8.4, 10.4, 8.4, 10.4, 13.4)
    painter.drawPath(front)
    # 後ろの人は、前の人に隠れない右側だけを描く
    head = QPainterPath()
    head.arcMoveTo(QRectF(8.8, 3.2, 4.4, 4.4), 100.0)
    head.arcTo(QRectF(8.8, 3.2, 4.4, 4.4), 100.0, -200.0)
    painter.drawPath(head)
    back = QPainterPath()
    back.moveTo(12.0, 9.4)
    back.cubicTo(13.8, 9.8, 14.6, 11.4, 14.6, 13.4)
    painter.drawPath(back)


def _gear(painter: QPainter, _color: QColor) -> None:
    """一般（歯車）。"""
    path = QPainterPath()
    inner, outer = 4.7, 6.3
    for tooth in range(8):
        center = tooth * 45.0
        for offset, radius in ((-13.0, inner), (-8.0, outer), (8.0, outer), (13.0, inner)):
            angle = math.radians(center + offset)
            point = QPointF(8.0 + radius * math.cos(angle), 8.0 + radius * math.sin(angle))
            if tooth == 0 and offset == -13.0:
                path.moveTo(point)
            else:
                path.lineTo(point)
    path.closeSubpath()
    painter.drawPath(path)
    painter.drawEllipse(QPointF(8.0, 8.0), 2.1, 2.1)


def _info(painter: QPainter, color: QColor) -> None:
    """バージョン情報（丸の中に i）。"""
    painter.drawEllipse(QPointF(8.0, 8.0), 6.2, 6.2)
    painter.drawLine(QPointF(8.0, 7.5), QPointF(8.0, 11.3))
    painter.save()
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(color)
    painter.drawEllipse(QPointF(8.0, 5.0), 0.85, 0.85)
    painter.restore()


def _mic(painter: QPainter, color: QColor) -> None:
    """マイク（塗りの胴・受け皿・脚）。"""
    painter.save()
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(color)
    painter.drawRoundedRect(QRectF(5.6, 1.4, 4.8, 8.2), 2.4, 2.4)
    painter.restore()
    cradle = QPainterPath()
    cradle.moveTo(3.4, 7.4)
    cradle.cubicTo(3.4, 13.0, 12.6, 13.0, 12.6, 7.4)
    painter.drawPath(cradle)
    painter.drawLine(QPointF(8.0, 11.6), QPointF(8.0, 14.2))
    painter.drawLine(QPointF(5.6, 14.2), QPointF(10.4, 14.2))


def _waveform(painter: QPainter, _color: QColor) -> None:
    """波形（長さの違う縦線）。"""
    for x, half in ((2.0, 1.4), (4.4, 3.6), (6.8, 5.8), (9.2, 2.6), (11.6, 4.6), (14.0, 1.8)):
        painter.drawLine(QPointF(x, 8.0 - half), QPointF(x, 8.0 + half))


def _copy(painter: QPainter, _color: QColor) -> None:
    """コピー（手前の紙と、後ろにずれた紙の見えている 2 辺）。"""
    painter.drawRoundedRect(QRectF(5.4, 5.0, 8.4, 9.4), 1.8, 1.8)
    back = QPainterPath()
    back.moveTo(2.6, 10.6)
    back.lineTo(2.6, 3.4)
    back.cubicTo(2.6, 2.2, 3.2, 1.6, 4.4, 1.6)
    back.lineTo(10.2, 1.6)
    painter.drawPath(back)


def _refresh(painter: QPainter, _color: QColor) -> None:
    """読み込み直し（右回りの矢印。右上が切れた円と、円の終わりに付く矢じり）。"""
    arc = QPainterPath()
    # 3 時の少し上から時計回りに回し、1 時の手前で止める（切れ目は右上）
    arc.arcMoveTo(QRectF(2.8, 2.8, 10.4, 10.4), 5.0)
    arc.arcTo(QRectF(2.8, 2.8, 10.4, 10.4), 5.0, -310.0)
    painter.drawPath(arc)
    head = QPainterPath()
    head.moveTo(11.4, 1.0)
    head.lineTo(11.4, 4.3)
    head.lineTo(8.1, 4.3)
    painter.drawPath(head)


def _sun(painter: QPainter, _color: QColor) -> None:
    """太陽（丸と 8 本の光）。"""
    painter.drawEllipse(QPointF(8.0, 8.0), 2.9, 2.9)
    for step in range(8):
        angle = math.pi / 4 * step
        dx, dy = math.cos(angle), math.sin(angle)
        painter.drawLine(QPointF(8.0 + dx * 5.0, 8.0 + dy * 5.0), QPointF(8.0 + dx * 6.6, 8.0 + dy * 6.6))


def _bed(painter: QPainter, _color: QColor) -> None:
    """ベッド（頭の板・枕・寝台・脚）。"""
    painter.drawLine(QPointF(1.8, 3.6), QPointF(1.8, 13.0))
    painter.drawLine(QPointF(14.2, 8.4), QPointF(14.2, 13.0))
    painter.drawLine(QPointF(1.8, 11.0), QPointF(14.2, 11.0))
    painter.drawRoundedRect(QRectF(6.8, 6.6, 7.4, 4.4), 1.6, 1.6)
    painter.drawEllipse(QPointF(4.3, 8.6), 1.5, 1.5)


def _film(painter: QPainter, _color: QColor) -> None:
    """フィルム（枠と左右の送り穴）。"""
    painter.drawRoundedRect(QRectF(1.8, 3.0, 12.4, 10.0), 1.6, 1.6)
    painter.drawLine(QPointF(4.8, 3.0), QPointF(4.8, 13.0))
    painter.drawLine(QPointF(11.2, 3.0), QPointF(11.2, 13.0))
    for y in (6.3, 9.7):
        painter.drawLine(QPointF(1.8, y), QPointF(4.8, y))
        painter.drawLine(QPointF(11.2, y), QPointF(14.2, y))


def _tram(painter: QPainter, _color: QColor) -> None:
    """電車（車体・窓の線・足回り）。"""
    painter.drawRoundedRect(QRectF(3.2, 2.2, 9.6, 9.6), 2.2, 2.2)
    painter.drawLine(QPointF(3.2, 7.4), QPointF(12.8, 7.4))
    painter.drawLine(QPointF(5.6, 9.6), QPointF(5.7, 9.6))
    painter.drawLine(QPointF(10.3, 9.6), QPointF(10.4, 9.6))
    painter.drawLine(QPointF(5.4, 11.8), QPointF(3.8, 14.2))
    painter.drawLine(QPointF(10.6, 11.8), QPointF(12.2, 14.2))


def _cup(painter: QPainter, _color: QColor) -> None:
    """カップと受け皿。"""
    body = QPainterPath()
    body.moveTo(3.0, 4.2)
    body.lineTo(10.6, 4.2)
    body.lineTo(10.6, 7.6)
    body.cubicTo(10.6, 12.2, 3.0, 12.2, 3.0, 7.6)
    body.closeSubpath()
    painter.drawPath(body)
    handle = QPainterPath()
    handle.moveTo(10.6, 5.4)
    handle.cubicTo(14.4, 5.0, 14.4, 9.4, 10.2, 9.0)
    painter.drawPath(handle)
    painter.drawLine(QPointF(2.2, 13.6), QPointF(11.4, 13.6))


def _hourglass(painter: QPainter, _color: QColor) -> None:
    """砂時計。"""
    painter.drawLine(QPointF(4.0, 2.2), QPointF(12.0, 2.2))
    painter.drawLine(QPointF(4.0, 13.8), QPointF(12.0, 13.8))
    glass = QPainterPath()
    glass.moveTo(5.0, 2.2)
    glass.cubicTo(5.0, 6.0, 7.4, 6.6, 8.0, 8.0)
    glass.cubicTo(8.6, 6.6, 11.0, 6.0, 11.0, 2.2)
    glass.moveTo(5.0, 13.8)
    glass.cubicTo(5.0, 10.0, 7.4, 9.4, 8.0, 8.0)
    glass.cubicTo(8.6, 9.4, 11.0, 10.0, 11.0, 13.8)
    painter.drawPath(glass)


def _sparkles(painter: QPainter, color: QColor) -> None:
    """きらめき（大小の 4 つ角の星）。"""
    painter.save()
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(color)
    for cx, cy, r in ((6.4, 9.2, 5.4), (12.2, 4.0, 2.8)):
        star = QPainterPath()
        star.moveTo(cx, cy - r)
        star.quadTo(cx, cy, cx + r, cy)
        star.quadTo(cx, cy, cx, cy + r)
        star.quadTo(cx, cy, cx - r, cy)
        star.quadTo(cx, cy, cx, cy - r)
        painter.drawPath(star)
    painter.restore()


_ICONS: Dict[str, Callable[[QPainter, QColor], None]] = {
    "grid": _grid,
    "key": _key,
    "one": _numbered("1"),
    "two": _numbered("2"),
    "book": _book,
    "translate": _translate,
    "people": _people,
    "gear": _gear,
    "info": _info,
    "mic": _mic,
    "waveform": _waveform,
    "sparkles": _sparkles,
    "sun": _sun,
    "bed": _bed,
    "film": _film,
    "tram": _tram,
    "cup": _cup,
    "hourglass": _hourglass,
    "copy": _copy,
    "refresh": _refresh,
}


def names() -> tuple:
    """持っているアイコンの名前を返す。"""
    return tuple(_ICONS)


def paint(painter: QPainter, name: str, rect: QRectF, color: QColor) -> None:
    """
    アイコンを描く。

    Args:
        painter: 描く先
        name: アイコンの名前（names() のどれか。知らない名前なら何も描かない）
        rect: 描く枠（正方形を想定。枠いっぱいに伸び縮みさせる）
        color: 線の色
    """
    draw = _ICONS.get(name)
    if draw is None:
        return
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.translate(rect.topLeft())
    painter.scale(rect.width() / _GRID, rect.height() / _GRID)
    pen = QPen(color, _STROKE)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    draw(painter, color)
    painter.restore()
