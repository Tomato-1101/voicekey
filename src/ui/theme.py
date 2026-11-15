"""
テーマ（配色・寸法・フォント・動き）モジュール

Windows 版の見た目を Windows 11 の標準（Fluent デザイン。「設定」アプリと同じ作法）に合わせるための、
色・寸法・フォント・アニメーションの定義を 1 か所に集める。
画面の並び・文言・アイコン・ロゴ・HUD の形は Mac 版と同じにし（両方使う人が迷わないように）、
質感（文字・色・角丸・部品の形）だけを Windows のものにする。

色の値は WinUI 3 の標準テーマ（Common/Light/Dark の ThemeResource）から取っている。
アクセントは「voicekey のオレンジ」と「Windows のアクセント色」から選べる。
"""

import math
import sys
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from PySide6.QtCore import QObject, QRectF, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontDatabase,
    QGuiApplication,
    QImage,
    QPainter,
    QPainterPath,
    QPalette,
    QPixmap,
)

# ---------------------------------------------------------------------------
# ブランドの基本色（Brand.swift と同じ値。ロゴと、アクセントを「voicekey のオレンジ」にしたときに使う）
# ---------------------------------------------------------------------------

SIGNAL = "#FF5A1F"       # 灯り（ロゴの点・既定のアクセント）
SIGNAL_DEEP = "#D9480F"  # ライト背景の上に小さい文字で signal を使うときだけ
BONE = "#F4F1EB"         # ロゴのライト地
STONE = "#CFC9BE"
LEGEND = "#7F796F"
CARBON = "#2B2A27"       # ロゴのダーク地

# 外観モード（設定ファイルに保存する値）
APPEARANCE_SYSTEM = "system"
APPEARANCE_LIGHT = "light"
APPEARANCE_DARK = "dark"

# アクセント色（設定ファイルに保存する値）
ACCENT_BRAND = "brand"    # voicekey のオレンジ
ACCENT_SYSTEM = "system"  # Windows の設定（個人用設定 › 色）のアクセント色

# Windows の既定のアクセント（青）。アクセント色が読めないときに使う
_WINDOWS_DEFAULT_ACCENT = "#0078D4"


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


def with_alpha(color: QColor, alpha: float) -> QColor:
    """color の不透明度だけを alpha（0.0〜1.0）に変えた色を返す。"""
    out = QColor(color)
    out.setAlphaF(max(0.0, min(1.0, alpha)))
    return out


def _mix(a: QColor, b: QColor, t: float) -> QColor:
    """a と b を t（0.0〜1.0）で混ぜた不透明の色を返す。"""
    return QColor(
        round(a.red() + (b.red() - a.red()) * t),
        round(a.green() + (b.green() - a.green()) * t),
        round(a.blue() + (b.blue() - a.blue()) * t),
    )


# ---------------------------------------------------------------------------
# アクセント色（Windows の設定から読む）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AccentShades:
    """
    1 つのアクセント色の濃淡（Windows の SystemAccentColor の 7 段と同じ並び）。

    Windows 11 はライト外観では Dark1（面）と Dark2（文字）、ダーク外観では Light2（面）と
    Light3（文字）を使う。同じ並びで持っておけば外観ごとの選び方を Windows と揃えられる。
    """

    light3: QColor
    light2: QColor
    light1: QColor
    base: QColor
    dark1: QColor
    dark2: QColor
    dark3: QColor


def _derive_shades(base: QColor) -> AccentShades:
    """1 色から 7 段の濃淡を作る（Windows の実際の濃淡が読めないときの代わり）。"""
    white = QColor("#FFFFFF")
    black = QColor("#000000")
    return AccentShades(
        light3=_mix(base, white, 0.62),
        light2=_mix(base, white, 0.44),
        light1=_mix(base, white, 0.22),
        base=QColor(base),
        dark1=_mix(base, black, 0.20),
        dark2=_mix(base, black, 0.40),
        dark3=_mix(base, black, 0.60),
    )


def _read_windows_accent_palette() -> Optional[AccentShades]:
    """
    Windows が実際に使っているアクセントの濃淡をレジストリから読む（Windows 以外では None）。

    HKCU\\...\\Explorer\\Accent の AccentPalette は 4 バイト（RGBA）× 8 色で、
    Light3 / Light2 / Light1 / 基準 / Dark1 / Dark2 / Dark3 / （未使用）の順に並ぶ。
    ここから取れば、ボタンやトグルの色が「設定」アプリなど Windows 標準の画面と同じになる。
    """
    if sys.platform != "win32":
        return None
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\Accent",
        ) as key:
            data, _kind = winreg.QueryValueEx(key, "AccentPalette")
    except OSError:
        return None
    if not isinstance(data, (bytes, bytearray)) or len(data) < 28:
        return None
    colors = [QColor(data[i * 4], data[i * 4 + 1], data[i * 4 + 2]) for i in range(7)]
    return AccentShades(*colors)


def system_accent_shades() -> AccentShades:
    """
    Windows のアクセント色の濃淡を返す。

    読む順: レジストリの AccentPalette → Qt が OS から受け取ったアクセント色 → Windows の既定の青。
    """
    shades = _read_windows_accent_palette()
    if shades is not None:
        return shades
    if QGuiApplication.instance() is not None:
        color = QGuiApplication.palette().color(QPalette.ColorRole.Accent)
        if color.isValid() and color.alpha() > 0:
            return _derive_shades(color)
    return _derive_shades(QColor(_WINDOWS_DEFAULT_ACCENT))


# ---------------------------------------------------------------------------
# パレット（ライト／ダークで切り替わる色の一式）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Palette:
    """
    1 つの外観（ライトまたはダーク）とアクセントで使う色の一式。

    Attributes:
        dark: ダーク外観なら True
        window: 窓の地（Windows 11 の Mica が無いときの地と同じ色）
        window_tint: 背景のぼかしの上に重ねる地色（いまは窓の地と同じ）
        wash_top / wash_bottom: 旧デザインの名残（透明。使わない）
        island: サイドバーの面（Windows では窓の地にそのまま載せるので透明）
        island_rim_top / island_rim_bottom: 同、縁（透明）
        shadow_alpha: 浮いた面（履歴パネル）の影の濃さ（0.0〜1.0）
        row: 設定行をまとめるカードの面
        row_hover: カードの行にマウスを載せたときの面
        card_border: カードの縁
        line: 区切り線
        text: 本文の文字
        text_secondary: 補助の文字（説明・見出し）
        text_disabled: 無効な項目の文字
        accent: アクセントの面（選択の印・トグル ON・主ボタン）
        accent_hover: 同、マウスを載せたとき
        accent_text: 地の上の文字・リンクにアクセントを使うときの色
        on_accent: アクセントの面の上の文字
        control: 入力欄・コンボボックス・ボタンの面
        control_hover: 同、マウスを載せたとき
        control_pressed: 同、押している間
        control_border: 同、縁（上と左右）
        control_border_bottom: 同、縁（下。Windows のボタンは下だけ少し濃い）
        input_active: 入力中の入力欄の面
        subtle_hover: 面を持たない部品（ナビの行・アイコンボタン）にマウスを載せたときの面
        subtle_pressed: 同、押している間・選ばれている行
        track_off: トグルが OFF のときの溝の面
        track_off_border: 同、縁
        knob_off: 同、つまみ
        popup: メニュー・一覧・ツールチップの面
        popup_border: 同、縁
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
    card_border: QColor
    line: QColor
    text: QColor
    text_secondary: QColor
    text_disabled: QColor
    accent: QColor
    accent_hover: QColor
    accent_text: QColor
    on_accent: QColor
    control: QColor
    control_hover: QColor
    control_pressed: QColor
    control_border: QColor
    control_border_bottom: QColor
    input_active: QColor
    subtle_hover: QColor
    subtle_pressed: QColor
    track_off: QColor
    track_off_border: QColor
    knob_off: QColor
    popup: QColor
    popup_border: QColor
    danger: QColor
    warn: QColor
    ok: QColor


def _accent_colors(dark: bool, accent_mode: str, shades: Optional[AccentShades]) -> Tuple[QColor, QColor, QColor, QColor]:
    """
    アクセントの (面, マウスを載せた面, 文字, 面の上の文字) を決める。

    Windows のアクセントは Windows 11 と同じ選び方（ライト = Dark1 の面・Dark2 の文字・白い文字、
    ダーク = Light2 の面・Light3 の文字・黒い文字）。voicekey のオレンジはロゴと同じ色のまま使う。
    """
    if accent_mode == ACCENT_SYSTEM:
        s = shades or system_accent_shades()
        if dark:
            return s.light2, _mix(s.light2, QColor("#000000"), 0.10), s.light3, QColor("#000000")
        return s.dark1, _mix(s.dark1, QColor("#FFFFFF"), 0.10), s.dark2, QColor("#FFFFFF")
    if dark:
        return rgba(SIGNAL), rgba("#FF6E3A"), rgba("#FF8A5C"), rgba("#FFFFFF")
    return rgba(SIGNAL), rgba("#FF6E3A"), rgba(SIGNAL_DEEP), rgba("#FFFFFF")


def build_palette(dark: bool, accent_mode: str = ACCENT_BRAND, shades: Optional[AccentShades] = None) -> Palette:
    """
    外観とアクセントからパレットを作る。

    Args:
        dark: ダーク外観なら True
        accent_mode: ACCENT_BRAND / ACCENT_SYSTEM
        shades: Windows のアクセントの濃淡（省略時は OS から読む）

    Returns:
        色の一式
    """
    accent, accent_hover, accent_text, on_accent = _accent_colors(dark, accent_mode, shades)
    clear = QColor(0, 0, 0, 0)
    if dark:
        return Palette(
            dark=True,
            window=rgba("#202020"),
            window_tint=rgba("#202020"),
            wash_top=clear,
            wash_bottom=clear,
            island=clear,
            island_rim_top=clear,
            island_rim_bottom=clear,
            shadow_alpha=0.32,
            row=rgba("#FFFFFF", 0.0512),
            row_hover=rgba("#FFFFFF", 0.0837),
            card_border=rgba("#000000", 0.10),
            line=rgba("#FFFFFF", 0.0837),
            text=rgba("#FFFFFF"),
            text_secondary=rgba("#FFFFFF", 0.786),
            text_disabled=rgba("#FFFFFF", 0.3628),
            accent=accent,
            accent_hover=accent_hover,
            accent_text=accent_text,
            on_accent=on_accent,
            control=rgba("#FFFFFF", 0.0605),
            control_hover=rgba("#FFFFFF", 0.0837),
            control_pressed=rgba("#FFFFFF", 0.0326),
            control_border=rgba("#FFFFFF", 0.0698),
            control_border_bottom=rgba("#FFFFFF", 0.0930),
            input_active=rgba("#1E1E1E", 0.70),
            subtle_hover=rgba("#FFFFFF", 0.0605),
            subtle_pressed=rgba("#FFFFFF", 0.0419),
            track_off=rgba("#000000", 0.10),
            track_off_border=rgba("#FFFFFF", 0.5442),
            knob_off=rgba("#FFFFFF", 0.786),
            popup=rgba("#2C2C2C"),
            popup_border=rgba("#757575", 0.40),
            danger=rgba("#FF99A4"),
            warn=rgba("#FCE100"),
            ok=rgba("#6CCB5F"),
        )
    return Palette(
        dark=False,
        window=rgba("#F3F3F3"),
        window_tint=rgba("#F3F3F3"),
        wash_top=clear,
        wash_bottom=clear,
        island=clear,
        island_rim_top=clear,
        island_rim_bottom=clear,
        shadow_alpha=0.14,
        row=rgba("#FFFFFF", 0.70),
        row_hover=rgba("#F9F9F9", 0.50),
        card_border=rgba("#000000", 0.0578),
        line=rgba("#000000", 0.0803),
        text=rgba("#000000", 0.8956),
        text_secondary=rgba("#000000", 0.6063),
        text_disabled=rgba("#000000", 0.3614),
        accent=accent,
        accent_hover=accent_hover,
        accent_text=accent_text,
        on_accent=on_accent,
        control=rgba("#FFFFFF", 0.70),
        control_hover=rgba("#F9F9F9", 0.50),
        control_pressed=rgba("#F9F9F9", 0.30),
        control_border=rgba("#000000", 0.0578),
        control_border_bottom=rgba("#000000", 0.1622),
        input_active=rgba("#FFFFFF"),
        subtle_hover=rgba("#000000", 0.0373),
        subtle_pressed=rgba("#000000", 0.0241),
        track_off=rgba("#000000", 0.0241),
        track_off_border=rgba("#000000", 0.4458),
        knob_off=rgba("#000000", 0.6063),
        popup=rgba("#F9F9F9"),
        popup_border=rgba("#000000", 0.0578),
        danger=rgba("#C42B1C"),
        warn=rgba("#9D5D00"),
        ok=rgba("#0F7B0F"),
    )


LIGHT: Palette = build_palette(False, ACCENT_BRAND, _derive_shades(QColor(_WINDOWS_DEFAULT_ACCENT)))
DARK: Palette = build_palette(True, ACCENT_BRAND, _derive_shades(QColor(_WINDOWS_DEFAULT_ACCENT)))


# ---------------------------------------------------------------------------
# 寸法（Windows 11 の標準部品の寸法。単位は論理ピクセル）
# ---------------------------------------------------------------------------


class Metrics:
    """設定ウィンドウと部品の寸法。WinUI 3 の NavigationView・SettingsCard・Button 等に合わせる。"""

    WINDOW_MIN_W = 760
    WINDOW_MIN_H = 600
    WINDOW_DEFAULT_W = 960
    WINDOW_DEFAULT_H = 700

    # サイドバー（窓の地にそのまま載せる。浮いた島にはしない）
    WINDOW_PADDING = 0
    ISLAND_GAP = 0
    SIDEBAR_W = 248
    ISLAND_RADIUS = 8

    # サイドバーのナビ行（NavigationViewItem: 高さ 36・角丸 4・左右 4 の余白）
    NAV_ROW_H = 36
    NAV_ROW_RADIUS = 4
    NAV_ICON = 16

    # 本文
    CONTENT_MAX_W = 840
    CONTENT_PADDING = 24

    # 設定行をまとめるカード（SettingsCard: 角丸 4・左右 16 の余白）
    GROUP_RADIUS = 4
    ROW_MIN_H = 64
    ROW_PADDING_H = 16
    ROW_PADDING_V = 12

    # 操作部品（ToggleSwitch 40×20、Button / TextBox / ComboBox 高さ 32・角丸 4）
    SWITCH_W = 40
    SWITCH_H = 20
    BUTTON_H = 32
    FIELD_H = 32
    FIELD_RADIUS = 4

    # 浮いた面（履歴パネル）の影
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
# 日本語は Yu Gothic UI（Windows の日本語 UI の標準。Segoe に無い字はここへ落ちる）。
# Mac（開発・テスト）では入っていないので OS の既定の字になる。
_FONT_CANDIDATES: Tuple[str, ...] = ("Segoe UI Variable Text", "Segoe UI", "Yu Gothic UI")
# 見出し（20px 以上）は Windows と同じく Display の字形を先に使う
_DISPLAY_CANDIDATES: Tuple[str, ...] = ("Segoe UI Variable Display", "Segoe UI", "Yu Gothic UI")
_font_families: Optional[Tuple[str, ...]] = None
_display_families: Optional[Tuple[str, ...]] = None


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


def _display_font_families() -> Tuple[str, ...]:
    """見出し用のフォント名を使う順に返す。"""
    global _display_families
    if _display_families is None:
        installed = set(QFontDatabase.families())
        _display_families = tuple(name for name in _DISPLAY_CANDIDATES if name in installed)
    return _display_families


# Mac 版と同じ数値で書かれた文字の大きさを、Windows の文字の大きさの段（Caption 12 /
# Body 14 / Subtitle 20 / Title 28）に読み替える表。各画面は Mac と同じ数値のまま書いておけば、
# 大きさの序列（本文 > 説明 > 注記）を保ったまま Windows の標準の大きさになる。
# Windows の本文は Mac より 1px 大きい（Mac 13 → Windows 14）。表に無い大きさはそのまま使う。
_WINDOWS_SIZE: Dict[int, int] = {
    9: 10,
    10: 11,
    11: 12,
    12: 13,
    13: 14,
    14: 14,
    15: 16,
    16: 16,
    17: 20,
}


def px(size: int) -> int:
    """
    Mac 版の数値で書いた文字の大きさを、Windows の標準の大きさ（ピクセル）にする。

    Args:
        size: Mac 版と同じ数値の大きさ

    Returns:
        Windows で使う大きさ（ピクセル）
    """
    return _WINDOWS_SIZE.get(size, size)


def font(size: int = 13, weight: QFont.Weight = QFont.Weight.Normal) -> QFont:
    """
    UI 用のフォントを作る。大きさは Mac 版と同じ数値で指定する（px() で Windows の大きさになる）。

    Args:
        size: 文字の大きさ（Mac 版と同じ数値）
        weight: 太さ（Windows の見出しは Bold ではなく Semibold を使う）

    Returns:
        設定済みの QFont
    """
    return font_px(px(size), weight)


def font_px(pixels: int, weight: QFont.Weight = QFont.Weight.Normal) -> QFont:
    """
    読み替えずに、そのままの大きさ（ピクセル）でフォントを作る。

    Args:
        pixels: 文字の大きさ（ピクセル）
        weight: 太さ

    Returns:
        設定済みの QFont
    """
    f = QFont()
    families = _display_font_families() if pixels >= 20 else font_families()
    if families:
        f.setFamilies(list(families))
    f.setPixelSize(pixels)
    # Windows の UI は Bold（700）をほとんど使わず Semibold（600）で強調する
    if weight > QFont.Weight.DemiBold:
        weight = QFont.Weight.DemiBold
    f.setWeight(weight)
    return f


# ---------------------------------------------------------------------------
# 外観の管理（OS に追従・手動切替・アクセント色）
# ---------------------------------------------------------------------------


class Theme(QObject):
    """
    いまの外観（ライト／ダーク）とアクセント色を持ち、変わったら知らせる。

    外観の既定は OS の設定に追従する。手動でライト／ダークに固定もできる。
    アクセント色は voicekey のオレンジ（既定）か Windows のアクセント色を選べる
    （どちらも選んだ値は呼び出し側が設定ファイルに保存する）。

    Signals:
        changed: 外観かアクセント色が変わったとき（部品はこれを受けて描き直す）
    """

    changed = Signal()

    _instance: Optional["Theme"] = None

    def __init__(self) -> None:
        super().__init__()
        self._mode: str = APPEARANCE_SYSTEM
        self._dark: bool = self._system_is_dark()
        self._accent_mode: str = ACCENT_BRAND
        self._shades: Optional[AccentShades] = None
        self._palette: Palette = build_palette(self._dark, self._accent_mode)
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
        """OS の外観が変わったとき。追従モードのときだけ反映する（アクセント色も読み直す）。"""
        dark = self._system_is_dark() if self._mode == APPEARANCE_SYSTEM else self._dark
        self._rebuild(dark, reread_accent=True)

    def _rebuild(self, dark: bool, reread_accent: bool = False) -> None:
        """パレットを作り直し、色が変わっていれば知らせる。"""
        if reread_accent and self._accent_mode == ACCENT_SYSTEM:
            self._shades = system_accent_shades()
        palette = build_palette(dark, self._accent_mode, self._shades)
        changed = dark != self._dark or palette != self._palette
        self._dark = dark
        self._palette = palette
        if changed:
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
        dark = self._system_is_dark() if mode == APPEARANCE_SYSTEM else mode == APPEARANCE_DARK
        self._rebuild(dark)

    @property
    def accent_mode(self) -> str:
        """いまのアクセント色（"brand" / "system"）。"""
        return self._accent_mode

    def set_accent_mode(self, mode: str) -> None:
        """
        アクセント色を切り替える。

        Args:
            mode: ACCENT_BRAND（voicekey のオレンジ）/ ACCENT_SYSTEM（Windows のアクセント色）。
                知らない値は ACCENT_BRAND として扱う
        """
        if mode not in (ACCENT_BRAND, ACCENT_SYSTEM):
            mode = ACCENT_BRAND
        self._accent_mode = mode
        self._rebuild(self._dark, reread_accent=True)

    def refresh_system_accent(self) -> None:
        """Windows のアクセント色を読み直す（利用者が Windows の設定で色を変えたとき用）。"""
        if self._accent_mode == ACCENT_SYSTEM:
            self._rebuild(self._dark, reread_accent=True)

    @property
    def is_dark(self) -> bool:
        """いまダーク外観なら True。"""
        return self._dark

    @property
    def palette(self) -> Palette:
        """いまの外観とアクセントのパレット。"""
        return self._palette




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
    Qt の標準部品（入力欄・コンボボックス・スクロールバー・メニュー等）の見た目を返す。

    Windows 11 の TextBox / ComboBox / MenuFlyout / ScrollBar に合わせる。
    自前で描く部品（トグル・ボタン・ナビ・カード）は components.py が QPainter で描くので、
    ここには含めない。

    Args:
        p: 使うパレット（省略時はいまの外観）

    Returns:
        QApplication / 窓に setStyleSheet する文字列
    """
    p = p or palette()
    families = ", ".join(f'"{name}"' for name in font_families()) or "sans-serif"
    r = Metrics.FIELD_RADIUS
    body = px(13)
    # 入力欄の下の縁: 待機中は少し濃い線、入力中はアクセントの 2px の線（Windows の TextBox と同じ）
    return f"""
    QWidget {{
        color: {_css(p.text)};
        font-family: {families};
        font-size: {body}px;
    }}
    QToolTip {{
        background: {_css(p.popup)};
        color: {_css(p.text)};
        border: 1px solid {_css(p.popup_border)};
        border-radius: 4px;
        padding: 6px 8px;
        font-size: {px(11)}px;
    }}
    QLabel {{ background: transparent; }}
    QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox {{
        background: {_css(p.control)};
        border: 1px solid {_css(p.control_border)};
        border-bottom: 1px solid {_css(p.control_border_bottom)};
        border-radius: {r}px;
        padding: 5px 10px 5px 10px;
        selection-background-color: {_css(p.accent)};
        selection-color: {_css(p.on_accent)};
    }}
    QLineEdit, QSpinBox {{ min-height: 20px; }}
    /* 数値欄の中の文字欄には枠と余白を重ねない（重ねると数字が切れて見えなくなる） */
    QSpinBox QLineEdit {{ background: transparent; border: none; padding: 0; min-height: 0; }}
    /* 上下ボタンは使わない（NoButtons）。QSS を当てると既定幅の場所が残って文字欄が潰れるので 0 にする */
    QSpinBox::up-button, QSpinBox::down-button {{
        subcontrol-origin: border; width: 0; height: 0; border: none; image: none;
    }}
    QLineEdit:hover, QPlainTextEdit:hover, QTextEdit:hover, QSpinBox:hover {{
        background: {_css(p.control_hover)};
    }}
    /* 入力中は下の縁だけ 2px のアクセントにする。上下の余白を 1px 減らして高さを変えない */
    QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QSpinBox:focus {{
        background: {_css(p.input_active)};
        border-bottom: 2px solid {_css(p.accent)};
        padding-bottom: 4px;
    }}
    QLineEdit:disabled, QPlainTextEdit:disabled, QTextEdit:disabled, QSpinBox:disabled {{
        color: {_css(p.text_disabled)};
        background: {_css(p.control_pressed)};
    }}
    QComboBox {{
        background: {_css(p.control)};
        border: 1px solid {_css(p.control_border)};
        border-bottom: 1px solid {_css(p.control_border_bottom)};
        border-radius: {r}px;
        padding: 5px 32px 5px 11px;
        min-height: 20px;
    }}
    QComboBox:hover {{ background: {_css(p.control_hover)}; }}
    QComboBox:disabled {{ color: {_css(p.text_disabled)}; }}
    QComboBox::drop-down {{
        subcontrol-origin: padding;
        subcontrol-position: center right;
        width: 30px;
        border: none;
    }}
    QComboBox QAbstractItemView {{
        background: {_css(p.popup)};
        border: 1px solid {_css(p.popup_border)};
        border-radius: 8px;
        padding: 4px;
        outline: none;
        selection-background-color: {_css(p.subtle_hover)};
        selection-color: {_css(p.text)};
    }}
    QComboBox QAbstractItemView::item {{
        min-height: 32px;
        padding: 0 10px;
        border-radius: 4px;
    }}
    QComboBox QAbstractItemView::item:hover,
    QComboBox QAbstractItemView::item:selected {{
        background: {_css(p.subtle_hover)};
        color: {_css(p.text)};
    }}
    QMenu {{
        background: {_css(p.popup)};
        border: 1px solid {_css(p.popup_border)};
        border-radius: 8px;
        padding: 4px;
    }}
    QMenu::item {{
        padding: 7px 28px 7px 12px;
        border-radius: 4px;
        margin: 1px 0;
    }}
    QMenu::item:selected {{
        background: {_css(p.subtle_hover)};
        color: {_css(p.text)};
    }}
    QMenu::item:disabled {{ color: {_css(p.text_disabled)}; }}
    QMenu::separator {{
        height: 1px;
        background: {_css(p.line)};
        margin: 4px -4px;
    }}
    QScrollArea, QScrollArea > QWidget > QWidget {{ background: transparent; border: none; }}
    /* Windows 11 のスクロールバー: 細い角丸のつまみだけ。溝は描かない */
    QScrollBar:vertical {{
        background: transparent;
        width: 12px;
        margin: 4px 2px 4px 2px;
    }}
    QScrollBar::handle:vertical {{
        background: {_css(with_alpha(p.text, 0.32 if p.dark else 0.30))};
        border-radius: 3px;
        min-height: 32px;
        margin: 0 2px;
    }}
    QScrollBar::handle:vertical:hover {{
        background: {_css(with_alpha(p.text, 0.50 if p.dark else 0.45))};
        margin: 0 1px;
    }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
    QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
    QScrollBar:horizontal {{ height: 0; }}
    """
