"""
ロゴ（アプリアイコンとトレイのアイコン）

Mac 版と同じロゴを出す。出どころを 1 か所にまとめておく:
- アプリアイコン: Mac 版と同じ画像（assets/AppIconLight.png・AppIconDark.png）をそのまま使う。
  描き直すと刻印や灯りの形がずれて別のロゴに見えるため、画像を写しとして持つ。
- トレイのアイコン: Mac のメニューバーと同じ「キーの枠・刻印の棒・灯り」を同じ寸法で描く
  （正本は macos/Sources/Voicekey/VoicekeyApp.swift の StatusIcon）。
"""

import sys
from pathlib import Path
from typing import Dict, Tuple

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QImage, QPainter, QPen, QPixmap

from ..config.types import AppState
from .theme import SIGNAL, rgba

# 凍結した実行ファイルでも、ソースからの実行でも、このファイルの隣に置く（voicekey.spec の datas と対）
_ASSET_DIR = Path(__file__).resolve().parent / "assets"
_ICON_FILES = {False: "AppIconLight.png", True: "AppIconDark.png"}

# 元の画像（512px）は読むたびに数 ms かかるので 1 回だけ読む
_source_cache: Dict[bool, QImage] = {}
# 縮めた結果は（外観, ピクセル数）ごとに使い回す。描くたびに縮めると窓の伸縮がもたつく
_scaled_cache: Dict[Tuple[bool, int], QPixmap] = {}


def _source(dark: bool) -> QImage:
    """元のアイコン画像を返す（ファイルが無ければ空の画像）。"""
    image = _source_cache.get(dark)
    if image is None:
        image = QImage(str(_ASSET_DIR / _ICON_FILES[dark]))
        _source_cache[dark] = image
    return image


def app_icon_pixmap(dark: bool, size: int, device_pixel_ratio: float = 1.0) -> QPixmap:
    """
    アプリアイコンを指定の大きさで返す。

    Args:
        dark: ダーク外観用（カーボン地）なら True。ライト外観用はボーン地
        size: 表示上の一辺（倍率をかける前のピクセル）
        device_pixel_ratio: 画面の倍率（高倍率の画面でぼけないよう、その分だけ細かく作る）

    Returns:
        アイコン。画像が読めないときは空の QPixmap
    """
    pixels = max(1, round(size * device_pixel_ratio))
    key = (dark, pixels)
    pixmap = _scaled_cache.get(key)
    if pixmap is None:
        source = _source(dark)
        if source.isNull():
            return QPixmap()
        scaled = source.scaled(
            pixels, pixels, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
        )
        pixmap = QPixmap.fromImage(scaled)
        _scaled_cache[key] = pixmap
    result = QPixmap(pixmap)
    result.setDevicePixelRatio(pixels / float(size))
    return result


def app_icon() -> QIcon:
    """
    窓・タスクバーに出すアプリアイコンを返す（ボーン地。exe のアイコン icon.ico と同じ絵）。
    """
    icon = QIcon()
    for size in (16, 20, 24, 32, 40, 48, 64, 128, 256):
        pixmap = app_icon_pixmap(False, size)
        if not pixmap.isNull():
            icon.addPixmap(pixmap)
    return icon


# ----- トレイのアイコン -----

# Mac の StatusIcon と同じ寸法（18×18 のマス・y は下向き）
_CANVAS = 18.0
_FRAME = QRectF(1.875, 1.875, 14.25, 14.25)
_FRAME_RADIUS = 4.125
_FRAME_LINE = 1.35
_LEGEND = QRectF(4.5, 11.4, 4.875, 1.35)
_LEGEND_RADIUS = 0.675
_LIGHT_X, _LIGHT_Y = 11.7, 6.3

_TASKBAR_KEY = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"


def taskbar_is_light() -> bool:
    """
    タスクバーが明るい色か（Windows の「Windows モード」がライトか）を返す。

    アプリの外観（ライト／ダーク）とは別の設定なので、レジストリを直接読む。

    Returns:
        明るければ True。Windows 以外・読めないときは False（暗いタスクバーとして描く）
    """
    if sys.platform != "win32":
        return False
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _TASKBAR_KEY) as key:
            value, _ = winreg.QueryValueEx(key, "SystemUsesLightTheme")
        return bool(value)
    except OSError:
        return False


def _ring(painter: QPainter, radius: float, line: float, color: QColor) -> None:
    """灯りの輪（線）を描く。"""
    painter.setPen(QPen(color, line))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawEllipse(QRectF(_LIGHT_X - radius, _LIGHT_Y - radius, radius * 2, radius * 2))


def _status_pixmap(state: AppState, light_taskbar: bool, pixels: int) -> QPixmap:
    """1 つの大きさぶんのトレイのアイコンを描く。"""
    pixmap = QPixmap(pixels, pixels)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.scale(pixels / _CANVAS, pixels / _CANVAS)

    # 枠と刻印はタスクバーの明暗に合わせる（Mac のテンプレート画像・labelColor に当たる）
    label = QColor(0, 0, 0, 217) if light_taskbar else QColor(255, 255, 255, 235)
    signal = rgba(SIGNAL)
    recording = state in (AppState.RECORDING, AppState.RECORDING_AUTO_ENTER)

    painter.setPen(QPen(label, _FRAME_LINE))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawRoundedRect(_FRAME, _FRAME_RADIUS, _FRAME_RADIUS)

    # 自動 Enter のときは刻印の棒も灯す（＝離したら Enter が押される合図）
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(signal if state == AppState.RECORDING_AUTO_ENTER else label)
    painter.drawRoundedRect(_LEGEND, _LEGEND_RADIUS, _LEGEND_RADIUS)

    if recording:
        painter.setBrush(signal)
        painter.drawEllipse(QRectF(_LIGHT_X - 2.1, _LIGHT_Y - 2.1, 4.2, 4.2))
    elif state == AppState.TRANSCRIBING:
        _ring(painter, 1.6, 1.2, signal)
    else:
        _ring(painter, 1.5, 1.05, label)
    painter.end()
    return pixmap


def status_icon(state: AppState, light_taskbar: bool) -> QIcon:
    """
    状態に応じたトレイのアイコンを返す（キーの枠＋刻印の棒＋灯り）。

    待機は灯りの輪だけ、録音中は灯りの点、変換中は灯り色の輪。色が付くのは灯りだけ。

    Args:
        state: アプリの状態
        light_taskbar: タスクバーが明るい色なら True（枠を黒で描く）

    Returns:
        アイコン（通知領域の大きさ・画面の倍率ごとの絵を持つ）
    """
    icon = QIcon()
    # 通知領域は 16px（100%）〜32px（200%）。その間の倍率の分も持たせて、縮小でにじませない
    for pixels in (16, 20, 24, 32, 40, 48, 64):
        icon.addPixmap(_status_pixmap(state, light_taskbar, pixels))
    return icon

