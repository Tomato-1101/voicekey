"""
テーマ（配色・寸法・フォント・動き）モジュール

Mac 版のデザイン（ロゴ「17 LEGEND」の配色と「浮遊するガラス島」）を Windows 版でも
同じ見た目にするための、色・寸法・フォント・アニメーションの定義を 1 か所に集める。
正本は macos/Sources/Voicekey/UI/Brand.swift と Glass.swift。

色をここ以外で直書きしないための置き場。アクセントは signal（灯りの橙）の 1 色だけで、
Windows のアクセント色（利用者の設定で青や紫に変わる）には頼らない。
"""

import math
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from PySide6.QtCore import QObject, QRectF, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontDatabase,
    QGuiApplication,
    QImage,
    QPainter,
    QPainterPath,
    QPixmap,
)

# ---------------------------------------------------------------------------
# ブランドの基本色（Brand.swift と同じ値）
# ---------------------------------------------------------------------------

SIGNAL = "#FF5A1F"       # 灯り。唯一のアクセント（録音の点・トグル ON・選択・進捗）
SIGNAL_DEEP = "#D9480F"  # ライト背景の上に小さい文字で signal を使うときだけ
BONE = "#F4F1EB"         # ライトの地
STONE = "#CFC9BE"        # 線・弱い面
LEGEND = "#7F796F"       # 補助文字（刻印）
CARBON = "#2B2A27"       # ダークの地・ライトの濃い文字

# 外観モード（設定ファイルに保存する値）
APPEARANCE_SYSTEM = "system"
APPEARANCE_LIGHT = "light"
APPEARANCE_DARK = "dark"


def rgba(hex_color: str, alpha: float = 1.0) -> QColor:
    """
    "#RRGGBB" と不透明度（0.0〜1.0）から QColor を作る。

    Args:
        hex_color: "#RRGGBB" 形式の色
        alpha: 不透明度（0.0〜1.0）

    Returns:
        不透明度を反映した QColor
    """
    color = QColor(hex_color)
    color.setAlphaF(max(0.0, min(1.0, alpha)))
    return color


# ---------------------------------------------------------------------------
# パレット（ライト／ダークで切り替わる色の一式）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Palette:
    """
    1 つの外観（ライトまたはダーク）で使う色の一式。

    Attributes:
        dark: ダーク外観なら True
        window: 窓の地（背景のぼかしが使えないときの単色）
        window_tint: 背景のぼかしの上に薄く重ねる地色（不透明度つき）
        wash_top: 地のグラデーションの左上の色（灯りの気配）
        wash_bottom: 地のグラデーションの右下の色
        island: 浮いた島（サイドバー・本文）の面
        island_rim_top: 島の上縁の光（ガラスの厚み）
        island_rim_bottom: 島の下縁の光
        shadow_alpha: 島の影の濃さ（0.0〜1.0）
        row: 設定行をまとめる棚の面
        row_hover: 行にマウスを載せたときの面
        line: 区切り線
        text: 本文の文字
        text_secondary: 補助の文字（説明・見出し）
        text_disabled: 無効な項目の文字
        accent: 灯り（選択・トグル ON・主ボタン）
        accent_text: 地の上に小さい文字で灯りを使うときの色
        on_accent: 灯りの面の上の文字
        control: 入力欄・ピッカー・副ボタンの面
        control_hover: 同、マウスを載せたとき
        control_border: 同、縁
        track_off: トグルが OFF のときの溝
        danger: 破壊的な操作・エラーの文字
        warn: 注意の文字
        ok: 正常の文字
    """

    dark: bool
    window: QColor
    window_tint: QColor
    wash_top: QColor
    wash_bottom: QColor
    island: QColor
    island_rim_top: QColor
    island_rim_bottom: QColor
    shadow_alpha: float
    row: QColor
    row_hover: QColor
    line: QColor
    text: QColor
    text_secondary: QColor
    text_disabled: QColor
    accent: QColor
    accent_text: QColor
    on_accent: QColor
    control: QColor
    control_hover: QColor
    control_border: QColor
    track_off: QColor
    danger: QColor
    warn: QColor
    ok: QColor


def _light_palette() -> Palette:
    """ライト外観（地 = Bone）のパレットを作る。"""
    return Palette(
        dark=False,
        window=rgba(BONE),
        window_tint=rgba(BONE, 0.62),
        wash_top=rgba(SIGNAL, 0.035),
        wash_bottom=rgba(BONE, 0.10),
        # Mac の極薄マテリアル（ライト）は白っぽく透ける面。白 55% で近似する
        island=rgba("#FFFFFF", 0.55),
        island_rim_top=rgba("#FFFFFF", 0.40),
        island_rim_bottom=rgba("#FFFFFF", 0.06),
        shadow_alpha=0.15,
        row=rgba("#FFFFFF", 0.40),
        row_hover=rgba("#FFFFFF", 0.62),
        line=rgba(CARBON, 0.10),
        text=rgba("#1F1E1C"),
        text_secondary=rgba(LEGEND),
        text_disabled=rgba(LEGEND, 0.55),
        accent=rgba(SIGNAL),
        accent_text=rgba(SIGNAL_DEEP),
        on_accent=rgba("#FFFFFF"),
        control=rgba("#FFFFFF", 0.35),
        control_hover=rgba("#FFFFFF", 0.60),
        control_border=rgba(CARBON, 0.12),
        track_off=rgba(CARBON, 0.16),
        danger=rgba("#C8321E"),
        warn=rgba("#A35C00"),
        ok=rgba("#2E7D4F"),
    )


def _dark_palette() -> Palette:
    """ダーク外観（地 = Carbon）のパレットを作る。"""
    return Palette(
        dark=True,
        window=rgba("#232220"),
        window_tint=rgba(CARBON, 0.66),
        wash_top=rgba(SIGNAL, 0.035),
        wash_bottom=rgba("#000000", 0.30),
        island=rgba("#FFFFFF", 0.07),
        island_rim_top=rgba("#FFFFFF", 0.22),
        island_rim_bottom=rgba("#FFFFFF", 0.04),
        shadow_alpha=0.30,
        row=rgba("#FFFFFF", 0.06),
        row_hover=rgba("#FFFFFF", 0.10),
        line=rgba("#FFFFFF", 0.09),
        text=rgba(BONE),
        text_secondary=rgba("#A8A297"),
        text_disabled=rgba("#A8A297", 0.50),
        accent=rgba(SIGNAL),
        accent_text=rgba("#FF7A47"),
        on_accent=rgba("#FFFFFF"),
        control=rgba("#FFFFFF", 0.08),
        control_hover=rgba("#FFFFFF", 0.14),
        control_border=rgba("#FFFFFF", 0.12),
        track_off=rgba("#FFFFFF", 0.18),
        danger=rgba("#FF6B5A"),
        warn=rgba("#F0A94A"),
        ok=rgba("#5FC98A"),
    )


LIGHT: Palette = _light_palette()
DARK: Palette = _dark_palette()


# ---------------------------------------------------------------------------
# 寸法（Mac 版の設定ウィンドウ・HUD と同じ値。単位は論理ピクセル）
# ---------------------------------------------------------------------------


class Metrics:
    """設定ウィンドウと部品の寸法。Mac 版 SettingsView.swift / Glass.swift に合わせる。"""

    WINDOW_MIN_W = 760
    WINDOW_MIN_H = 600
    WINDOW_DEFAULT_W = 900
    WINDOW_DEFAULT_H = 680

    # 窓の縁から島までの余白と、島どうしのすき間
    WINDOW_PADDING = 12
    ISLAND_GAP = 12

    SIDEBAR_W = 216
    ISLAND_RADIUS = 18

    # サイドバーのナビ行
    NAV_ROW_H = 36
    NAV_ROW_RADIUS = 9
    NAV_ICON = 18

    # 本文
    CONTENT_MAX_W = 720
    CONTENT_PADDING = 20

    # 設定行をまとめる棚
    GROUP_RADIUS = 8
    ROW_MIN_H = 44
    ROW_PADDING_H = 14
    ROW_PADDING_V = 9

    # 操作部品
    SWITCH_W = 38
    SWITCH_H = 22
    BUTTON_H = 30
    FIELD_H = 30
    FIELD_RADIUS = 7

    # 島の影（Glass.swift の glassIsland: radius 22・y 10）
    SHADOW_BLUR = 22
    SHADOW_DY = 10


class Motion:
    """動きの時間（ミリ秒）と、ばねの係数。Mac 版 Hud.swift / Glass.swift に合わせる。"""

    PRESS_MS = 100        # ボタンを押したときの沈み
    HOVER_MS = 120        # マウスを載せたときの色の変化
    TOGGLE_MS = 160       # トグルのつまみの移動
    NAV_MS = 220          # ナビの選択が滑って移る
    PAGE_MS = 160         # ページ切替のクロスフェード
    HUD_FOLLOW_MS = 220   # HUD の位置の追従

    # SwiftUI の spring(response: 0.3, dampingFraction: 0.82) と同じ動き
    SPRING_RESPONSE = 0.30
    SPRING_DAMPING = 0.82

    FRAME_MS = 16         # 60fps


class Spring:
    """
    ばねの動きで値を目標へ近づける（SwiftUI の spring と同じ式）。

    HUD の変形（待機ピル ⇄ 録音パネル）のように、途中で目標が変わっても
    速度を保ったまま滑らかにつながる動きが要る所で使う。

    Attributes:
        value: いまの値
        target: 目標の値
        velocity: いまの速度（1 秒あたり）
    """

    def __init__(
        self,
        value: float = 0.0,
        response: float = Motion.SPRING_RESPONSE,
        damping: float = Motion.SPRING_DAMPING,
    ) -> None:
        """
        Args:
            value: 最初の値
            response: ばねの応答時間（秒）。小さいほど速い
            damping: 減衰の割合（1.0 で行き過ぎなし）
        """
        self.value = value
        self.target = value
        self.velocity = 0.0
        # 質量 1 として、SwiftUI と同じ換算で剛性と減衰係数を決める
        self._stiffness = (2.0 * math.pi / response) ** 2
        self._damping = 4.0 * math.pi * damping / response

    def snap(self, value: float) -> None:
        """動かさずに値と目標を同じにする。"""
        self.value = value
        self.target = value
        self.velocity = 0.0

    def step(self, dt: float) -> bool:
        """
        時間を dt 秒だけ進める。

        Args:
            dt: 進める時間（秒）

        Returns:
            まだ動いていれば True、目標に落ち着いたら False
        """
        # 1 コマが長いと発散するので 4ms 刻みに分けて積分する
        remaining = min(dt, 0.1)
        while remaining > 1e-6:
            h = min(remaining, 0.004)
            accel = self._stiffness * (self.target - self.value) - self._damping * self.velocity
            self.velocity += accel * h
            self.value += self.velocity * h
            remaining -= h
        if abs(self.target - self.value) < 0.001 and abs(self.velocity) < 0.01:
            self.snap(self.target)
            return False
        return True


# ---------------------------------------------------------------------------
# フォント
# ---------------------------------------------------------------------------

# 先頭から順に、入っているものを使う。Windows 11 = Segoe UI Variable、Windows 10 = Segoe UI。
# 日本語は Yu Gothic UI（Segoe に無い字はここへ落ちる）。Mac（開発・テスト）では既定の字。
_FONT_CANDIDATES: Tuple[str, ...] = ("Segoe UI Variable Text", "Segoe UI", "Yu Gothic UI")
_font_families: Optional[Tuple[str, ...]] = None


def font_families() -> Tuple[str, ...]:
    """
    この PC に入っている UI 用フォントの名前を、使う順に返す。

    Returns:
        フォント名のタプル（1 つも無ければ空。その場合は OS の既定の字になる）
    """
    global _font_families
    if _font_families is None:
        installed = set(QFontDatabase.families())
        _font_families = tuple(name for name in _FONT_CANDIDATES if name in installed)
    return _font_families


def font(size: int = 13, weight: QFont.Weight = QFont.Weight.Normal) -> QFont:
    """
    UI 用のフォントを作る。大きさは Mac 版と同じ数値（論理ピクセル）で指定する。

    Args:
        size: 文字の大きさ（ピクセル）
        weight: 太さ

    Returns:
        設定済みの QFont
    """
    f = QFont()
    families = font_families()
    if families:
        f.setFamilies(list(families))
    f.setPixelSize(size)
    f.setWeight(weight)
    return f


# ---------------------------------------------------------------------------
# 外観の管理（OS に追従・手動切替）
# ---------------------------------------------------------------------------


class Theme(QObject):
    """
    いまの外観（ライト／ダーク）を持ち、変わったら知らせる。

    既定は OS の設定に追従する。手動でライト／ダークに固定もできる
    （固定した値は呼び出し側が設定ファイルに保存する）。

    Signals:
        changed: 外観が変わったとき（部品はこれを受けて描き直す）
    """

    changed = Signal()

    _instance: Optional["Theme"] = None

    def __init__(self) -> None:
        super().__init__()
        self._mode: str = APPEARANCE_SYSTEM
        self._dark: bool = self._system_is_dark()
        app = QGuiApplication.instance()
        if app is not None:
            # OS の外観が変わったら（追従モードのときだけ）切り替える
            QGuiApplication.styleHints().colorSchemeChanged.connect(self._on_system_changed)

    @classmethod
    def instance(cls) -> "Theme":
        """アプリ全体で 1 つだけのインスタンスを返す。"""
        if cls._instance is None:
            cls._instance = Theme()
        return cls._instance

    @staticmethod
    def _system_is_dark() -> bool:
        """OS の外観がダークかどうかを返す（アプリ未生成ならライト）。"""
        if QGuiApplication.instance() is None:
            return False
        return QGuiApplication.styleHints().colorScheme() == Qt.ColorScheme.Dark

    def _on_system_changed(self, _scheme: Qt.ColorScheme) -> None:
        """OS の外観が変わったとき。追従モードのときだけ反映する。"""
        if self._mode == APPEARANCE_SYSTEM:
            self._apply(self._system_is_dark())

    def _apply(self, dark: bool) -> None:
        """外観を切り替え、変わっていれば知らせる。"""
        if dark != self._dark:
            self._dark = dark
            self.changed.emit()

    @property
    def mode(self) -> str:
        """いまの外観モード（"system" / "light" / "dark"）。"""
        return self._mode

    def set_mode(self, mode: str) -> None:
        """
        外観モードを切り替える。

        Args:
            mode: "system"（OS に追従）/ "light" / "dark"。知らない値は "system" として扱う
        """
        if mode not in (APPEARANCE_SYSTEM, APPEARANCE_LIGHT, APPEARANCE_DARK):
            mode = APPEARANCE_SYSTEM
        self._mode = mode
        if mode == APPEARANCE_SYSTEM:
            self._apply(self._system_is_dark())
        else:
            self._apply(mode == APPEARANCE_DARK)

    @property
    def is_dark(self) -> bool:
        """いまダーク外観なら True。"""
        return self._dark

    @property
    def palette(self) -> Palette:
        """いまの外観のパレット。"""
        return DARK if self._dark else LIGHT


def palette() -> Palette:
    """いまの外観のパレットを返す（Theme.instance().palette の短縮）。"""
    return Theme.instance().palette


# ---------------------------------------------------------------------------
# 影の描画（島の浮遊感）
# ---------------------------------------------------------------------------

# 影は「ぼかした角丸の四角」を 1 回だけ作って使い回す。
# QGraphicsDropShadowEffect は中の部品が描き直されるたびに島全体をぼかし直すので、
# トグルやホバーの動きが重くなる。あらかじめ作った画像を伸ばして貼る方が軽い。
_shadow_cache: Dict[Tuple[int, int, int], QPixmap] = {}


def _blurred_rounded_rect(radius: int, blur: int, alpha: int) -> QPixmap:
    """
    ぼかした角丸の四角（影の素材）を作る。

    Args:
        radius: 角丸の半径
        blur: ぼかしの広がり
        alpha: 影の濃さ（0〜255）

    Returns:
        9 分割して伸ばせる影の画像（一辺 = (radius + blur) * 2 + 2）
    """
    # 角とぼかしが入る最小の大きさ。中央の 2px を伸ばして任意の大きさにする
    edge = radius + blur
    size = edge * 2 + 2
    # 小さく描いて拡大するとぼけた縁になる（箱ぼかしを何度も掛けるより速く、十分なめらか）
    scale = 4
    small = max(8, size // scale)
    image = QImage(small, small, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    inset = blur / scale
    path = QPainterPath()
    path.addRoundedRect(
        QRectF(inset, inset, small - inset * 2, small - inset * 2),
        radius / scale,
        radius / scale,
    )
    painter.fillPath(path, QColor(0, 0, 0, alpha))
    painter.end()
    # 縮小 → 拡大を 2 回くり返して縁をなだらかにする
    blurred = image
    for _ in range(2):
        tiny = blurred.scaled(
            max(4, small // 2), max(4, small // 2),
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        blurred = tiny.scaled(
            small, small,
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
    full = blurred.scaled(
        size, size,
        Qt.AspectRatioMode.IgnoreAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
    return QPixmap.fromImage(full)


def paint_shadow(
    painter: QPainter,
    rect: QRectF,
    radius: int = Metrics.ISLAND_RADIUS,
    blur: int = Metrics.SHADOW_BLUR,
    dy: int = Metrics.SHADOW_DY,
    alpha: float = 0.15,
) -> None:
    """
    角丸の四角の影を描く（島の下に敷く）。

    Args:
        painter: 描画先
        rect: 影を落とす四角（島の外形）
        radius: 島の角丸の半径
        blur: ぼかしの広がり
        dy: 影を下へずらす量
        alpha: 影の濃さ（0.0〜1.0）
    """
    key = (radius, blur, int(alpha * 255))
    pixmap = _shadow_cache.get(key)
    if pixmap is None:
        pixmap = _blurred_rounded_rect(radius, blur, key[2])
        _shadow_cache[key] = pixmap
    edge = radius + blur
    src = pixmap.width()
    # 影の外形は島よりぼかしの分だけ大きい
    x0 = rect.left() - blur
    y0 = rect.top() - blur + dy
    w = rect.width() + blur * 2
    h = rect.height() + blur * 2
    if w < edge * 2 or h < edge * 2:
        return
    # 9 分割（四隅はそのまま、辺と中央は伸ばす）
    xs = (0, edge, src - edge, src)
    ys = (0, edge, src - edge, src)
    dxs = (x0, x0 + edge, x0 + w - edge, x0 + w)
    dys = (y0, y0 + edge, y0 + h - edge, y0 + h)
    for row in range(3):
        for col in range(3):
            painter.drawPixmap(
                QRectF(dxs[col], dys[row], dxs[col + 1] - dxs[col], dys[row + 1] - dys[row]),
                pixmap,
                QRectF(xs[col], ys[row], xs[col + 1] - xs[col], ys[row + 1] - ys[row]),
            )


# ---------------------------------------------------------------------------
# 標準部品の見た目（QSS）
# ---------------------------------------------------------------------------


def _css(color: QColor) -> str:
    """QColor を QSS の rgba(...) 表記にする。"""
    return f"rgba({color.red()}, {color.green()}, {color.blue()}, {color.alpha()})"


def app_qss(p: Optional[Palette] = None) -> str:
    """
    Qt の標準部品（入力欄・ピッカー・スクロールバー・メニュー等）の見た目を返す。

    自前で描く部品（島・トグル・ボタン・ナビ）は components.py が QPainter で描くので、
    ここには含めない。

    Args:
        p: 使うパレット（省略時はいまの外観）

    Returns:
        QApplication / 窓に setStyleSheet する文字列
    """
    p = p or palette()
    families = ", ".join(f'"{name}"' for name in font_families()) or "sans-serif"
    # ポップアップ（ピッカーの一覧・メニュー）は透けると読みにくいので不透明の面にする
    popup = "#35332F" if p.dark else "#FBF9F5"
    r = Metrics.FIELD_RADIUS
    return f"""
    QWidget {{
        color: {_css(p.text)};
        font-family: {families};
        font-size: 13px;
    }}
    QToolTip {{
        background: {popup};
        color: {_css(p.text)};
        border: 1px solid {_css(p.control_border)};
        border-radius: 6px;
        padding: 5px 8px;
    }}
    QLabel {{ background: transparent; }}
    QLineEdit, QPlainTextEdit, QTextEdit {{
        background: {_css(p.control)};
        border: 1px solid {_css(p.control_border)};
        border-radius: {r}px;
        padding: 5px 9px;
        selection-background-color: {_css(rgba(SIGNAL, 0.35))};
        selection-color: {_css(p.text)};
    }}
    QLineEdit {{ min-height: 18px; }}
    QLineEdit:hover, QPlainTextEdit:hover, QTextEdit:hover {{
        background: {_css(p.control_hover)};
    }}
    QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus {{
        border: 1px solid {_css(p.accent)};
        background: {_css(p.control_hover)};
    }}
    QLineEdit:disabled, QPlainTextEdit:disabled, QTextEdit:disabled {{
        color: {_css(p.text_disabled)};
    }}
    QComboBox {{
        background: {_css(p.control)};
        border: 1px solid {_css(p.control_border)};
        border-radius: {r}px;
        padding: 5px 28px 5px 10px;
        min-height: 18px;
    }}
    QComboBox:hover {{ background: {_css(p.control_hover)}; }}
    QComboBox:focus {{ border: 1px solid {_css(p.accent)}; }}
    QComboBox:disabled {{ color: {_css(p.text_disabled)}; }}
    QComboBox::drop-down {{
        subcontrol-origin: padding;
        subcontrol-position: center right;
        width: 24px;
        border: none;
    }}
    QComboBox QAbstractItemView {{
        background: {popup};
        border: 1px solid {_css(p.control_border)};
        border-radius: 8px;
        padding: 4px;
        outline: none;
        selection-background-color: {_css(p.accent)};
        selection-color: {_css(p.on_accent)};
    }}
    QComboBox QAbstractItemView::item {{
        min-height: 26px;
        padding: 2px 8px;
        border-radius: 5px;
    }}
    QMenu {{
        background: {popup};
        border: 1px solid {_css(p.control_border)};
        border-radius: 8px;
        padding: 5px;
    }}
    QMenu::item {{
        padding: 6px 22px 6px 12px;
        border-radius: 5px;
    }}
    QMenu::item:selected {{
        background: {_css(p.accent)};
        color: {_css(p.on_accent)};
    }}
    QMenu::item:disabled {{ color: {_css(p.text_disabled)}; }}
    QMenu::separator {{
        height: 1px;
        background: {_css(p.line)};
        margin: 5px 8px;
    }}
    QScrollArea, QScrollArea > QWidget > QWidget {{ background: transparent; border: none; }}
    QScrollBar:vertical {{
        background: transparent;
        width: 10px;
        margin: 4px 2px 4px 0;
    }}
    QScrollBar::handle:vertical {{
        background: {_css(rgba("#FFFFFF" if p.dark else CARBON, 0.22))};
        border-radius: 4px;
        min-height: 28px;
    }}
    QScrollBar::handle:vertical:hover {{
        background: {_css(rgba("#FFFFFF" if p.dark else CARBON, 0.38))};
    }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
    QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
    QScrollBar:horizontal {{ height: 0; }}
    """
