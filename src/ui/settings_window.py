"""
設定ウィンドウモジュール

Mac 版 SettingsView.swift（2026-10-19 の作り直し）と同じ構成・同じ日本語文言で設定を管理する。
サイドバーは見出しつきで、Windows にだけあるダッシュボード系（ホーム / 実績 / 履歴）を先頭に置く:
  ホーム・実績・履歴
  はじめに: API キー
  音声入力: 入力の基本 / 録音キー 1 / 録音キー 2 / プロンプト / 辞書と数字 / 翻訳して入力
  アプリ: 一般 /（アカウント）/ バージョン情報
各ページの先頭にアイコン・ページ名・「このページで何を決めるか」の 1 行を出す。
録音キーの行の右端には割り当て中のキー（例 右Ctrl）か「未割り当て」を出す。
レイアウトは macOS System Settings 風の「左サイドバーナビ + カード型セクション」。
ダーク/ライトテーマ切り替えに対応（Qt は OS テーマに自動追従しないため Windows 版のみ）。
設定値の保存形式（settings.yaml のキー）は変えていない。
"""

import os
import threading
from datetime import datetime
from typing import Optional

from PySide6.QtCore import (
    Property,
    Qt,
    QEasingCurve,
    QPointF,
    QPropertyAnimation,
    QRectF,
    QSize,
    QTimer,
    QUrl,
    QVariantAnimation,
    Signal,
)
from PySide6.QtGui import (
    QBrush,
    QColor,
    QDesktopServices,
    QFont,
    QIcon,
    QKeyEvent,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressBar,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSlider,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ..config import ConfigManager, HotkeyMode, TranscriptionBackend
from ..config import backends, llm_models
from ..config.constants import APP_VERSION
from ..core import text_formatter
from ..core.audio_recorder import AudioRecorder
from ..core.history import MAX_ITEMS as HISTORY_MAX_ITEMS, HistoryStore
from ..core.mic_monitor import MicLevelMonitor
from ..core.stats import StatsStore
from ..platform import PlatformAdapter, get_platform_adapter
from ..utils import autostart, secrets
from .settings_widgets import (
    KIND_HEADER,
    KIND_PAGE,
    ROLE_KIND,
    ROLE_TRAILING,
    CostTrendBars,
    GroupedChoiceButton,
    NavItemDelegate,
    key_label,
    keys_conflict,
    translate_once_key_warnings,
)
from .styles import MacTheme

try:
    from ..core.history_sync import HistorySync, parse_iso_date
except Exception:  # pragma: no cover - 同期モジュール未配置時でも設定画面は開けるようにする
    HistorySync = None  # type: ignore
    parse_iso_date = None  # type: ignore


# 設定 › API キー の行（Mac の ApiKeysTab と同じ並び・見出し）。
# (見出し, [(項目, 表示名, 補足, 秘密か, プレースホルダ), ...])
_API_KEY_SECTIONS = [
    ("文字起こし", [
        ("soniox", "Soniox", "", True, "API キーを入力"),
        ("openai", "OpenAI", "文字起こし（REST / ライブ）で共用", True, "API キーを入力"),
        ("azure_key", "Microsoft MAI（キー）", "", True, "API キーを入力"),
        ("azure_endpoint", "Microsoft MAI（エンドポイント）", "", False,
         "https://<リソース名>.cognitiveservices.azure.com"),
        ("elevenlabs", "ElevenLabs", "", True, "API キーを入力"),
        ("groq", "Groq", "文字起こしと文章の自動整形・翻訳入力で共用", True, "API キーを入力"),
    ]),
    ("翻訳して入力", [
        ("gemini", "Gemini", "翻訳エンジンに Gemini を選んだときだけ使います（Groq なら不要）", True, "API キーを入力"),
    ]),
]
# 選択肢から外した旧エンジン（保存済みの設定で使っている人だけに出す）
_LEGACY_API_KEY_ROW = ("deepgram", "Deepgram（旧エンジン）", "選択肢から外したエンジンです。今の設定で使っている間だけ表示します", True, "API キーを入力")

# ホットキー動作モードの表示順と UI ラベル（Mac 版 HotkeyMode.label と完全一致させる）
_MODE_LABELS = [
    (HotkeyMode.HOLD.value, "押している間だけ"),
    (HotkeyMode.TOGGLE.value, "押すたびに開始・停止"),
]

# 言語選択肢（Mac 版の Picker と完全一致。空文字 = API 側の自動判定）
_LANGUAGE_OPTIONS = [
    ("日本語", "ja"),
    ("英語", "en"),
    ("自動判定", ""),
]

def _make_caption(text: str) -> QLabel:
    """設定項目の補足説明ラベルを作る（Mac 版の .caption 相当。色は QSS 側で管理）。"""
    label = QLabel(text)
    label.setWordWrap(True)
    label.setObjectName("caption")
    return label


def _backend_caption_text(backend: str) -> str:
    """選択中の文字起こしエンジンの説明文（Mac 版 SlotSettingsTab.backendCaption と同一文言）。"""
    return backends.caption(backend)


def _mode_caption(mode: str) -> str:
    """キーの押し方の説明（Mac の modeCaption と同じ文言）。"""
    if mode == HotkeyMode.TOGGLE.value:
        return "1 回押すと録音が始まり、もう 1 回押すと止まって入力します。長く話すときに向いています。"
    return "押している間だけ録音し、離すと入力します。"


# ページの見出し（アイコン名, 説明 1 行）。説明は Mac の subtitle（Mac 固有の言い回しは Windows に直した）
_PAGE_SUBTITLES = {
    "ホーム": "入力した量と、最近の履歴です。",
    "実績": "これまでの入力量・節約できた時間・レベルです。",
    "履歴": "これまでの入力です。クリックするとコピーします。",
    "API キー": "使うサービスのキーを入れます。キーはこの PC の資格情報マネージャーにだけ保存します。",
    "入力の基本": "話す言語・使うマイク・録音キーと一緒に使うキーを決めます。",
    "録音キー 1": "メインの録音キー。押して話すと、いま使っているアプリに文字が入ります。",
    "録音キー 2": "2 つ目の録音キー。エンジンや押し方を 1 と変えて使い分けられます。",
    "プロンプト": "文章を整えるときに AI へ渡す指示です。録音キー 1・2 で共通です。",
    "辞書と数字": "聞き間違えやすい言葉の直し方と、数字の書き方を決めます。",
    "翻訳して入力": "話した内容を別の言語に訳してから入力します。",
    "一般": "表示・音・起動・履歴など、アプリ全体の設定です。",
    "アカウント": "ログインとライセンスの状態です。",
    "バージョン情報": "いまのバージョンと、アップデートの確認です。",
}


def _format_history_date(date_str: str) -> str:
    """履歴一覧の日時表示を作る（ISO 8601 → 端末のローカル時刻 "YYYY-MM-DD HH:MM"）。

    parse_iso_date が未配置、または変換に失敗した場合は素朴な文字列整形にフォールバックする
    （history_sync 未配置でも履歴タブ自体は動く必要があるため）。
    """
    if parse_iso_date is not None:
        try:
            return parse_iso_date(date_str).astimezone().strftime("%Y-%m-%d %H:%M")
        except Exception:
            pass
    return date_str.replace("T", " ")[:16]


# ----------------------------------------------------------------------
# カード型レイアウトのヘルパー（macOS System Settings 風）
# ----------------------------------------------------------------------

# カード内の行の左右パディング（ヘアラインのインデントと揃える）
_CARD_PAD_X = 14


def _make_card() -> tuple[QFrame, QVBoxLayout]:
    """設定カード（角丸の面）とその縦レイアウトを作る。"""
    card = QFrame()
    card.setObjectName("card")
    layout = QVBoxLayout(card)
    layout.setContentsMargins(0, 2, 0, 2)
    layout.setSpacing(0)
    return card, layout


def _make_hairline() -> QWidget:
    """カード内の行区切り線（左右にインデントした 1px ライン）を作る。"""
    wrap = QWidget()
    lay = QHBoxLayout(wrap)
    lay.setContentsMargins(_CARD_PAD_X, 0, _CARD_PAD_X, 0)
    line = QFrame()
    line.setObjectName("hairline")
    line.setFixedHeight(1)
    lay.addWidget(line)
    return wrap


def _add_block(card_layout: QVBoxLayout, widget: QWidget) -> None:
    """カードへ自由構成のブロックを 1 行ぶん追加する（2 行目以降はヘアライン付き）。"""
    if card_layout.count() > 0:
        card_layout.addWidget(_make_hairline())
    wrap = QWidget()
    lay = QVBoxLayout(wrap)
    lay.setContentsMargins(_CARD_PAD_X, 9, _CARD_PAD_X, 9)
    lay.setSpacing(6)
    lay.addWidget(widget)
    card_layout.addWidget(wrap)


def _add_row(
    card_layout: QVBoxLayout,
    label_text: str,
    control: Optional[QWidget] = None,
    caption: str = "",
    caption_widget: Optional[QWidget] = None,
) -> None:
    """カードへ標準行（左ラベル + 右コントロール、任意で下に補足）を追加する。

    caption_widget を渡すと、その QLabel を補足として使う（動的に更新したい場合に使用。
    caption 文字列より優先）。
    """
    if card_layout.count() > 0:
        card_layout.addWidget(_make_hairline())
    row = QWidget()
    v = QVBoxLayout(row)
    v.setContentsMargins(_CARD_PAD_X, 9, _CARD_PAD_X, 9)
    v.setSpacing(5)

    h = QHBoxLayout()
    h.setContentsMargins(0, 0, 0, 0)
    h.setSpacing(8)
    h.addWidget(QLabel(label_text))
    h.addStretch()
    if control is not None:
        h.addWidget(control)
    v.addLayout(h)

    if caption_widget is not None:
        v.addWidget(caption_widget)
    elif caption:
        v.addWidget(_make_caption(caption))
    card_layout.addWidget(row)


# ----------------------------------------------------------------------
# サイドバーのアイコン（依存追加なしで自前 SVG → QIcon。Mac 版 SF Symbols に対応）
# ----------------------------------------------------------------------

# ページタイトル → アイコン名のマッピング
_NAV_ICON_NAME = {
    "ホーム": "home",
    "一般": "general",
    "入力の基本": "mic",
    "録音キー 1": "hotkey1",
    "録音キー 2": "hotkey2",
    "プロンプト": "prompt",
    "実績": "stats",
    "履歴": "history",
    "辞書と数字": "dictionary",
    "翻訳して入力": "translate",
    "アカウント": "account",
    "バージョン情報": "version",
    "API キー": "apikey",
}


def _nav_svg(name: str, color: str) -> str:
    """ナビ項目のラインアイコン SVG を返す（stroke=指定色）。"""
    head = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
        f'stroke="{color}" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">'
    )
    bodies = {
        "home": (
            '<path d="M4 11.5 12 4l8 7.5"/><path d="M6 10.5V20h12v-9.5"/>'
            '<rect x="10" y="14" width="4" height="6"/>'
        ),
        "general": (
            '<line x1="3" y1="9" x2="21" y2="9"/><circle cx="8" cy="9" r="2.3"/>'
            '<line x1="3" y1="15" x2="21" y2="15"/><circle cx="15" cy="15" r="2.3"/>'
        ),
        "hotkey1": (
            '<rect x="3" y="6" width="18" height="12" rx="3"/>'
            f'<text x="12" y="15.5" text-anchor="middle" font-size="9" '
            f'font-family="sans-serif" fill="{color}" stroke="none">1</text>'
        ),
        "hotkey2": (
            '<rect x="3" y="6" width="18" height="12" rx="3"/>'
            f'<text x="12" y="15.5" text-anchor="middle" font-size="9" '
            f'font-family="sans-serif" fill="{color}" stroke="none">2</text>'
        ),
        "stats": (
            '<polygon points="12 3 14.5 9 21 9.3 16 13.6 17.6 20 12 16.2 6.4 20 8 13.6 3 9.3 9.5 9"/>'
        ),
        "history": '<circle cx="12" cy="12" r="9"/><polyline points="12 7 12 12 16 14"/>',
        "dictionary": (
            '<path d="M5 4h11a2 2 0 0 1 2 2v14H7a2 2 0 0 1-2-2z"/>'
            '<line x1="5" y1="16.5" x2="18" y2="16.5"/>'
        ),
        "account": '<circle cx="12" cy="8.5" r="3.4"/><path d="M5.5 20a6.5 6.5 0 0 1 13 0"/>',
        "version": (
            '<circle cx="12" cy="12" r="9"/><line x1="12" y1="11" x2="12" y2="16.5"/>'
            f'<circle cx="12" cy="7.7" r="0.7" fill="{color}" stroke="none"/>'
        ),
        "apikey": (
            '<circle cx="8" cy="12" r="4"/><line x1="11.5" y1="12" x2="21" y2="12"/>'
            '<line x1="18" y1="12" x2="18" y2="15.5"/><line x1="21" y1="12" x2="21" y2="15"/>'
        ),
        "mic": (
            '<rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5.5 11a6.5 6.5 0 0 0 13 0"/>'
            '<line x1="12" y1="17.5" x2="12" y2="21"/>'
        ),
        "prompt": (
            '<path d="M4 20 15 9"/><path d="M14 5l1 2 2 1-2 1-1 2-1-2-2-1 2-1z"/>'
            '<path d="M19 12l.6 1.2 1.2.6-1.2.6-.6 1.2-.6-1.2-1.2-.6 1.2-.6z"/>'
        ),
        "translate": (
            '<path d="M4 5h8"/><path d="M8 3v2"/><path d="M5.5 5c.5 3 2.5 5.5 5.5 7"/>'
            '<path d="M10.5 5c-.5 3-2.5 5.5-5.5 7"/><path d="M13 21l4-9 4 9"/><line x1="14.5" y1="18" x2="19.5" y2="18"/>'
        ),
    }
    return head + bodies.get(name, "") + "</svg>"


def _svg_pixmap(svg: str, size: int) -> QPixmap:
    """SVG 文字列を QPixmap に描画する（hiDPI 用に 2 倍解像度でレンダリング）。"""
    renderer = QSvgRenderer(bytearray(svg, encoding="utf-8"))
    dpr = 2
    pm = QPixmap(size * dpr, size * dpr)
    pm.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pm)
    renderer.render(painter)
    painter.end()
    pm.setDevicePixelRatio(dpr)
    return pm


def _make_nav_icon(name: str, normal_color: str, size: int = 18) -> QIcon:
    """通常色＋選択時（白）の 2 状態を持つナビアイコンを作る。"""
    icon = QIcon()
    icon.addPixmap(_svg_pixmap(_nav_svg(name, normal_color), size), QIcon.Mode.Normal)
    icon.addPixmap(_svg_pixmap(_nav_svg(name, "#FFFFFF"), size), QIcon.Mode.Selected)
    return icon


class UsageBarChart(QWidget):
    """
    入力量の棒グラフ（依存追加なしの自前描画・アニメーション付き）。

    set_data() でラベルと値を渡すと、棒が 0 から伸びるアニメーションで描画する。
    色は set_colors() でテーマに追従させる。値は文字数を想定する。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(170)
        self._bars: list[tuple[str, int]] = []  # [(ラベル, 値)]
        self._max = 1
        self._progress = 0.0
        self._accent = QColor("#FF5A1F")  # ブランドの灯り
        self._text = QColor("#9A9AA2")
        self._grid = QColor(0, 0, 0, 40)
        self._empty_text = "まだデータがありません。音声入力するとここに表示されます。"

        # 棒の伸びアニメーション（0→1 を progress に流す）
        self._anim = QPropertyAnimation(self, b"progress")
        self._anim.setDuration(700)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)

    def set_colors(self, accent, text, grid) -> None:
        """テーマ色を設定する（accent/text は #hex 文字列か QColor、grid は QColor 推奨）。"""
        self._accent = QColor(accent)
        self._text = QColor(text)
        self._grid = QColor(grid)
        self.update()

    def set_data(self, bars) -> None:
        """棒データ [(ラベル, 値)] を設定し、0 から伸びるアニメーションを開始する。"""
        self._bars = list(bars)
        self._max = max([v for _, v in self._bars] + [1])
        self._anim.stop()
        self._progress = 0.0
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.start()
        self.update()

    def get_progress(self) -> float:
        return self._progress

    def set_progress(self, value: float) -> None:
        self._progress = float(value)
        self.update()

    # QPropertyAnimation が駆動する Qt プロパティ
    progress = Property(float, get_progress, set_progress)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        pad_l, pad_r, pad_t, pad_b = 8, 8, 18, 22
        plot_w = max(1, w - pad_l - pad_r)
        plot_h = max(1, h - pad_t - pad_b)
        baseline = pad_t + plot_h

        # ベースライン
        painter.setPen(QPen(self._grid, 1))
        painter.drawLine(pad_l, baseline, w - pad_r, baseline)

        n = len(self._bars)
        has_data = any(v > 0 for _, v in self._bars)
        if n == 0 or not has_data:
            painter.setPen(self._text)
            painter.drawText(
                QRectF(pad_l, pad_t, plot_w, plot_h),
                Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
                self._empty_text,
            )
            return

        show_values = n <= 7
        # ラベルが詰まりすぎないよう間引く（12 本以下は全部、以降は約 6 個）
        label_step = 1 if n <= 12 else max(1, round(n / 6))
        slot = plot_w / n
        bar_w = min(34.0, slot * 0.6)
        font = QFont(self.font())
        font.setPointSize(8)
        painter.setFont(font)

        for i, (label, value) in enumerate(self._bars):
            cx = pad_l + slot * (i + 0.5)
            bh = (value / self._max) * plot_h * self._progress if self._max > 0 else 0.0
            x = cx - bar_w / 2
            y = baseline - bh
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(self._accent if value > 0 else self._grid)
            r = min(3.0, bar_w / 2)
            painter.drawRoundedRect(QRectF(x, y, bar_w, bh), r, r)

            # X 軸ラベル（間引き対象）
            if i % label_step == 0:
                painter.setPen(self._text)
                painter.drawText(
                    QRectF(cx - slot / 2, baseline + 3, slot, pad_b - 3),
                    Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
                    label,
                )
            # 値ラベル（棒が少ないときだけ上に出す）
            if show_values and value > 0:
                painter.setPen(self._text)
                painter.drawText(
                    QRectF(cx - slot / 2, y - 14, slot, 12),
                    Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom,
                    str(value),
                )


class ToggleSwitch(QCheckBox):
    """
    iOS 風トグルスイッチ（QCheckBox 互換のオン/オフコントロール）。

    QCheckBox を継承するため isChecked()/setChecked()/toggled をそのまま使える。
    見た目だけを paintEvent で全面的に置き換える（標準チェックボックスは
    Windows ネイティブ描画で安っぽく見えるため）。
    """

    _TRACK_W = 40
    _TRACK_H = 22

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(self._TRACK_W, self._TRACK_H)
        self._dark = False
        self._offset = 1.0 if self.isChecked() else 0.0

        # ノブのスライドアニメーション
        self._anim = QPropertyAnimation(self, b"offset")
        self._anim.setDuration(140)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.toggled.connect(self._on_toggled)

    def set_dark(self, dark: bool) -> None:
        """テーマに応じてトラック色を切り替える（QSS では描画しないため自前管理）。"""
        self._dark = dark
        self.update()

    def _on_toggled(self, checked: bool) -> None:
        end = 1.0 if checked else 0.0
        # 非表示中（設定ロード時など）はアニメーションせず即座に反映する
        if not self.isVisible():
            self._anim.stop()
            self.offset = end
            return
        self._anim.stop()
        self._anim.setStartValue(self._offset)
        self._anim.setEndValue(end)
        self._anim.start()

    def get_offset(self) -> float:
        """ノブ位置（0.0=オフ 〜 1.0=オン）を取得する。"""
        return self._offset

    def set_offset(self, value: float) -> None:
        """ノブ位置を設定して再描画する（アニメーションから呼ばれる）。"""
        self._offset = float(value)
        self.update()

    # QPropertyAnimation が参照する Qt プロパティ（Python の property では動かない）
    offset = Property(float, get_offset, set_offset)

    def paintEvent(self, event) -> None:
        """トラック（角丸）と白いノブを描画する。"""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self.isEnabled():
            painter.setOpacity(0.4)

        t = self._offset
        off = QColor("#48484E") if self._dark else QColor("#D9D9DE")
        on = QColor("#FF5A1F")  # トグル ON はブランドの灯り（Mac の Brand.signal）
        # オフ→オンのトラック色をノブ位置に合わせて補間する
        track = QColor(
            round(off.red() + (on.red() - off.red()) * t),
            round(off.green() + (on.green() - off.green()) * t),
            round(off.blue() + (on.blue() - off.blue()) * t),
        )

        h = self.height()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(track)
        painter.drawRoundedRect(QRectF(0, 0, self.width(), h), h / 2, h / 2)

        # ノブ（白円 + 下方向のわずかな影で浮かせる）
        knob_d = h - 4
        x = 2 + (self.width() - knob_d - 4) * t
        painter.setBrush(QColor(0, 0, 0, 30))
        painter.drawEllipse(QRectF(x, 3, knob_d, knob_d))
        painter.setBrush(QColor("#FFFFFF"))
        painter.drawEllipse(QRectF(x, 2, knob_d, knob_d))



class ThemeToggleButton(QPushButton):
    """
    アニメーション付きテーマ切替ボタン（太陽/月アイコン）。

    クリックでダーク/ライトモードを切り替え、
    180度回転アニメーションで視覚的フィードバックを提供する。
    """

    def __init__(self, parent=None, is_dark: bool = False):
        """
        テーマ切替ボタンを初期化する。

        Args:
            parent: 親ウィジェット
            is_dark: ダークモードの場合True
        """
        super().__init__(parent)
        self.setFixedSize(32, 32)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._is_dark = is_dark
        self._angle = 0  # 回転角度

        # 回転アニメーション設定
        self._anim = QPropertyAnimation(self, b"angle")
        self._anim.setDuration(500)
        self._anim.setEasingCurve(QEasingCurve.Type.OutBack)

        self.clicked.connect(self._animate_toggle)

        # カスタム描画のためデフォルトスタイルを無効化
        self.setStyleSheet("border: none; background: transparent;")

    def _animate_toggle(self):
        """テーマ切替時の回転アニメーションを実行する。"""
        self._is_dark = not self._is_dark

        # 180度回転
        start = self._angle
        end = start + 180

        self._anim.setStartValue(start)
        self._anim.setEndValue(end)
        self._anim.start()

        self.update()

    def set_dark(self, dark: bool) -> None:
        """外部からテーマ状態を同期する（設定の再ロード/キャンセル時にプレビューを戻す）。"""
        self._is_dark = dark
        self.update()

    def get_angle(self):
        """現在の回転角度を取得する。"""
        return self._angle

    def set_angle(self, value):
        """回転角度を設定する。"""
        self._angle = value
        self.update()

    # QPropertyAnimation は Qt が認識できる Property を必要とする。
    # Python 組み込みの property() だとアニメーションが回転角度を駆動できない
    angle = Property(float, get_angle, set_angle)

    def paintEvent(self, event):
        """太陽または月のアイコンを描画する。"""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # テーマに応じた色（太陽：黄、月：白）
        color = QColor("#FFD60A") if not self._is_dark else QColor("#F2F2F7")

        width = self.width()
        height = self.height()
        center = QPointF(width / 2, height / 2)

        painter.translate(center)
        painter.rotate(self._angle)

        if not self._is_dark:
            # 太陽を描画
            painter.setBrush(QBrush(color))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(QPointF(0, 0), 6, 6)

            # 光線を描画
            painter.setPen(QPen(color, 2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            for i in range(8):
                painter.rotate(45)
                painter.drawLine(0, 9, 0, 11)

        else:
            # 月（三日月）を描画
            painter.setBrush(QBrush(color))
            painter.setPen(Qt.PenStyle.NoPen)

            # 2つの円で三日月形状を作成
            path = QPainterPath()
            path.addEllipse(QPointF(0, 0), 8, 8)

            cutout = QPainterPath()
            cutout.addEllipse(QPointF(4, -2), 7, 7)

            final_path = path.subtracted(cutout)
            painter.drawPath(final_path)


class HotkeyInput(QLineEdit):
    """
    ホットキー録音用カスタムウィジェット。

    クリックしてキーを押すと、そのキーコンビネーションを
    pynput形式の文字列として記録する。

    左右のAlt/Ctrl/Shift/Winキーを区別し、
    修飾キー単独もショートカットとして登録可能。
    """

    # Qt Keyから基本的な修飾キー種別への変換
    MODIFIER_KEY_MAP = {
        Qt.Key.Key_Control: "ctrl",
        Qt.Key.Key_Shift: "shift",
        Qt.Key.Key_Alt: "alt",
        Qt.Key.Key_Meta: "cmd",
    }

    def __init__(
        self,
        parent=None,
        platform_adapter: Optional[PlatformAdapter] = None,
        empty_label: Optional[str] = None,
    ):
        """ホットキー入力ウィジェットを初期化する。

        Args:
            empty_label: 未割り当て（空）のときに出すプレースホルダ文言。
                スロットでは「未割り当て」を渡す（Mac 版 HotkeyRecorderView の emptyLabel と対応）。
        """
        super().__init__(parent)
        self._platform = platform_adapter or get_platform_adapter()
        self.setReadOnly(True)
        self.setPlaceholderText(empty_label or "クリックしてキーを押す…")
        self._pressed_keys = []  # 押下中のキーを順番に追跡
        self._is_recording = False

    def focusInEvent(self, event):
        """フォーカス取得時は録音待機状態にする。

        ここでは _pressed_keys をクリアしない。クリックしただけでキーを押さずに
        フォーカスを外すと「内部状態は空なのに表示は旧値」という不整合になるため。
        最初のキー押下（keyPressEvent）で前の値をクリアして録音を開始する。
        """
        super().focusInEvent(event)
        self._is_recording = False

    def focusOutEvent(self, event):
        """フォーカス喪失時に録音状態を終了。"""
        super().focusOutEvent(event)
        self._is_recording = False

    def _get_modifier_key_name(self, virtual_key: int, scan_code: int = 0) -> str:
        """
        仮想キーコードまたはスキャンコードから修飾キー名を取得する。

        Args:
            virtual_key: Windows仮想キーコード
            scan_code: ネイティブスキャンコード（フォールバック用）

        Returns:
            pynput形式のキー名、またはマッチしない場合空文字
        """
        return self._platform.modifier_hotkey_from_native(
            virtual_key=virtual_key,
            scan_code=scan_code
        )

    def keyPressEvent(self, event: QKeyEvent):
        """
        キー押下イベントを処理してホットキーを記録する。

        複数キーの組み合わせを追跡し、左右の修飾キーを区別する。
        新しい入力開始時は古い設定をクリアする。

        Args:
            event: キーイベント
        """
        key = event.key()
        virtual_key = event.nativeVirtualKey()
        scan_code = event.nativeScanCode()

        # ESC は「割り当てなし」（空）として確定する（Mac 版と同じ挙動。ユーザー要望）。
        # 空ホットキーのスロットは app._slot_matches が False を返し録音が発火しない。
        if key == Qt.Key.Key_Escape:
            self._pressed_keys = []
            self._is_recording = False
            self.clear()  # 空文字にしてプレースホルダ（未割り当て）を表示
            event.accept()
            return

        # 新しい入力開始時は古いキーをクリア
        if not self._is_recording:
            self._pressed_keys = []
            self._is_recording = True

        # 修飾キーの場合
        if key in self.MODIFIER_KEY_MAP:
            # 仮想キーコードまたはスキャンコードで左右を判別
            key_name = self._get_modifier_key_name(virtual_key, scan_code)
            if not key_name:
                # 仮想キーコードが不明な場合は汎用名を使用
                base_name = self.MODIFIER_KEY_MAP[key]
                key_name = f"<{base_name}>"

            # まだ追加されていなければ追加
            if key_name not in self._pressed_keys:
                self._pressed_keys.append(key_name)
                self._update_display()

            event.accept()
            return

        # 通常キーの場合
        key_text = self._get_key_text(key, scan_code)
        if key_text and key_text not in self._pressed_keys:
            self._pressed_keys.append(key_text)
            self._update_display()

        event.accept()

    def keyReleaseEvent(self, event: QKeyEvent):
        """
        キーリリースイベントを処理する。

        最初のキーが離された時点で組み合わせを確定する。
        """
        # キーが離されたら現在の組み合わせを確定（それ以上追加しない）
        self._is_recording = False
        event.accept()

    def _update_display(self):
        """現在押されているキーの組み合わせを表示する。"""
        if self._pressed_keys:
            self.setText("+".join(self._pressed_keys))

    def _get_key_text(self, key: int, scan_code: int = 0) -> str:
        """
        Qtキーコードをpynput形式の文字列に変換する。

        Args:
            key: Qtキーコード
            scan_code: ネイティブスキャンコード（左右判別用）

        Returns:
            pynput形式のキー文字列
        """
        return self._platform.qt_key_to_hotkey_token(key, scan_code)


class SettingsWindow(QWidget):
    """
    アプリケーション設定ウィンドウ。

    Mac 版と同じ 5 タブ（一般 / 録音キー 1（メイン）/ 録音キー 2（サブ）/ 履歴 / API キー）で設定を管理する。
    設定は settings.yaml に保存し、アプリ側のホットリロードで即反映される。
    """

    # マイク自動検出の完了通知（ワーカースレッド → メインスレッドへ結果を渡す）
    _mic_detect_done = Signal(object)
    # 設定保存の完了通知（アプリ本体が設定変更を即座に適用するために購読する）
    settings_saved = Signal()
    # 利用権確認の完了通知（ワーカースレッド → メインスレッドへ）。表示更新のトリガ。
    _entitlement_done = Signal()
    # アクティベーションキー登録の完了通知（成功か, メッセージ）
    _redeem_done = Signal(bool, str)
    # 「セットアップガイドを開く」要求（アプリ本体が受けてオンボーディングを再表示する）
    open_onboarding_requested = Signal()

    def __init__(
        self,
        platform_adapter: Optional[PlatformAdapter] = None,
        history: Optional[HistoryStore] = None,
        stats: Optional[StatsStore] = None,
        config_manager: Optional[ConfigManager] = None,
        updater=None,
        history_sync: Optional["HistorySync"] = None,
    ) -> None:
        """
        設定ウィンドウを初期化する。

        Args:
            platform_adapter: プラットフォーム依存処理のアダプタ
            history: 音声入力履歴ストア（「履歴」タブで表示・再コピーする）
            stats: 使用実績ストア（「実績」タブで表示する）
            config_manager: アプリ本体と共有する設定マネージャ（None なら単体生成）
            updater: 自動アップデータ（「バージョン情報」タブで更新確認/実行に使う。None なら無効表示）
            history_sync: Mac との履歴同期（「履歴」タブに設定 UI を出す。None なら同期 UI は非表示相当）
        """
        super().__init__()
        self._platform = platform_adapter or get_platform_adapter()
        self._history = history
        self._stats = stats
        self._updater = updater
        self._history_sync = history_sync

        # DIST ビルドでは API キーページを作らないため、ページ生成前に空で初期化しておく
        # （_load_current_settings が無条件に _refresh_api_key_status を呼ぶ）
        self._api_key_inputs = {}
        self._api_key_status = {}

        # テーマ切替時にトラック色を更新するトグルスイッチの一覧
        self._toggles: list[ToggleSwitch] = []
        # 録音キーごとのエンジン＋モデルの選択（ポップアップの値。保存時に hotkeyN.backend / api_model へ）
        self._slot_choice = {1: (backends.SONIOX, ""), 2: (backends.GROQ, "")}

        # 本体と同じインスタンスを共有して保存値の乖離を防ぐ（未指定時のみ単体生成）
        self._config_manager = config_manager or ConfigManager()

        # テーマ設定を読み込み（デフォルトはライトモード）
        config = self._config_manager.config
        self._is_dark_mode = config.get("dark_mode", False)

        self._setup_window()
        self._setup_ui()
        self._load_current_settings()

        # 初期テーマを適用
        self._apply_theme(self._is_dark_mode)

    def _setup_window(self) -> None:
        """ウィンドウプロパティを設定する。"""
        self.setWindowTitle("voicekey 設定")
        # ルートだけが背景色を持つ（カード上のウィジェットを潰さないため。styles.py 参照）
        self.setObjectName("settingsRoot")
        self.resize(720, 600)

    def _setup_ui(self) -> None:
        """UIコンポーネントを設定する（左サイドバーナビ + 右コンテンツ）。"""
        root = QHBoxLayout(self)
        # 四周に backdrop を覗かせ、間にも隙間を空けてサイドバー島とコンテンツ島を「浮かせる」
        # （v1 の「2 画面分割」に見える問題への対策。styles.py の浮遊ガラス島デザイン参照）
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        # --- 左サイドバー: ブランド + 見出しつきナビゲーション ---
        # Mac と同じく開閉はやめ、全ページを見出し付きで常に並べる（1 クリックでどのページへも行ける）
        self._SIDE_W = 200
        self._sidebar = QFrame()
        self._sidebar.setObjectName("sidebarPane")
        self._sidebar.setFixedWidth(self._SIDE_W)
        sv = QVBoxLayout(self._sidebar)
        sv.setContentsMargins(8, 14, 8, 10)
        sv.setSpacing(2)

        brand_row = QHBoxLayout()
        brand_row.setContentsMargins(6, 0, 0, 0)
        brand_row.setSpacing(8)
        # 新ロゴ（icon.ico・「17 LEGEND」）。見つからないビルドでは文字だけにする
        self._sidebar_logo = QLabel()
        logo = self._brand_icon_pixmap(28)
        if logo is not None:
            self._sidebar_logo.setPixmap(logo)
            brand_row.addWidget(self._sidebar_logo)
        brand_box = QVBoxLayout()
        brand_box.setSpacing(0)
        self._sidebar_brand = QLabel("voicekey")
        self._sidebar_brand.setObjectName("brand")
        self._sidebar_caption = QLabel("設定")
        self._sidebar_caption.setObjectName("brandCaption")
        brand_box.addWidget(self._sidebar_brand)
        brand_box.addWidget(self._sidebar_caption)
        brand_row.addLayout(brand_box)
        brand_row.addStretch()
        sv.addLayout(brand_row)
        sv.addSpacing(10)

        self._nav = QListWidget()
        self._nav.setObjectName("sidebar")
        self._nav.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._nav.setIconSize(QSize(18, 18))
        self._nav.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._nav_delegate = NavItemDelegate(self._nav)
        self._nav.setItemDelegate(self._nav_delegate)
        sv.addWidget(self._nav, 1)
        root.addWidget(self._sidebar)

        # --- 右コンテンツ島: ページ見出し + ページ + 保存/キャンセル ---
        # QFrame にして objectName を付け、QSS の QFrame#contentPane（浮遊ガラス島）を効かせる
        right = QFrame()
        right.setObjectName("contentPane")
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.setSpacing(0)

        header = QWidget()
        hh = QHBoxLayout(header)
        hh.setContentsMargins(24, 18, 20, 8)
        hh.setSpacing(12)
        # ページ見出し: アイコン + ページ名 + 「このページで何を決めるか」の 1 行（Mac と同じ作り）
        self._page_icon = QLabel()
        self._page_icon.setFixedSize(28, 28)
        title_box = QVBoxLayout()
        title_box.setSpacing(2)
        self._page_title = QLabel("")
        self._page_title.setObjectName("pageTitle")
        self._page_subtitle = QLabel("")
        self._page_subtitle.setObjectName("caption")
        self._page_subtitle.setWordWrap(True)
        title_box.addWidget(self._page_title)
        title_box.addWidget(self._page_subtitle)
        # テーマ切替ボタン（Qt は OS テーマに追従しないため Windows 版独自）
        self._theme_toggle = ThemeToggleButton(is_dark=self._is_dark_mode)
        self._theme_toggle.clicked.connect(self._toggle_theme)
        hh.addWidget(self._page_icon, 0, Qt.AlignmentFlag.AlignTop)
        hh.addLayout(title_box, 1)
        hh.addWidget(self._theme_toggle, 0, Qt.AlignmentFlag.AlignTop)
        rv.addWidget(header)

        # ページ。(見出し or None, [(ページ名, ウィジェット), ...]) の順にサイドバーへ並べる
        self._pages = QStackedWidget()
        sections = [
            (None, [
                ("ホーム", self._create_home_page()),
                ("実績", self._create_stats_page()),
                ("履歴", self._create_history_page()),
            ]),
        ]
        # 配布版は自社サーバー経由でキーを持たないので API キーページを出さない（テスターの混乱防止）
        if not secrets.is_dist_build():
            sections.append(("はじめに", [("API キー", self._create_api_keys_page())]))
        voice_pages = [
            ("入力の基本", self._create_input_basics_page()),
            ("録音キー 1", self._create_slot_page(1)),
            ("録音キー 2", self._create_slot_page(2)),
            ("プロンプト", self._create_prompt_page()),
            ("辞書と数字", self._create_dictionary_page()),
        ]
        translate_page = self._create_translate_page()
        if translate_page is not None:
            voice_pages.append(("翻訳して入力", translate_page))
        sections.append(("音声入力", voice_pages))
        app_pages = [("一般", self._create_general_page())]
        # アカウント（ブラウザ経由ログイン）。personal はアカウント/サーバーを一切使わないので出さない
        if not secrets.is_personal_build():
            app_pages.append(("アカウント", self._create_account_page()))
        app_pages.append(("バージョン情報", self._create_version_page()))
        sections.append(("アプリ", app_pages))

        # ナビ行 → ページの対応（見出し行は None）。ページ名 → ナビ行も持つ
        self._nav_titles = []
        self._nav_row_of: dict = {}
        for header_title, pages in sections:
            if header_title:
                head = QListWidgetItem(header_title)
                head.setFlags(Qt.ItemFlag.NoItemFlags)
                head.setData(ROLE_KIND, KIND_HEADER)
                self._nav.addItem(head)
                self._nav_titles.append(None)
            for title, page in pages:
                item = QListWidgetItem(title)
                item.setToolTip(title)
                item.setData(ROLE_KIND, KIND_PAGE)
                item.setData(Qt.ItemDataRole.UserRole, self._pages.count())
                self._nav.addItem(item)
                self._nav_row_of[title] = self._nav.count() - 1
                self._nav_titles.append(title)
                self._pages.addWidget(page)
        self._home_page_index = self._nav_row_of["ホーム"]
        self._stats_page_index = self._nav_row_of["実績"]
        self._history_page_index = self._nav_row_of["履歴"]
        rv.addWidget(self._pages, 1)
        # アイコンは色がテーマ依存なので _apply_theme で設定する
        self._apply_nav_icons()

        # ボタンエリア（保存/キャンセル）。Mac 版は即時反映だが、Windows 版は
        # settings.yaml + ホットリロードのため明示保存とする
        button_layout = QHBoxLayout()
        button_layout.setContentsMargins(24, 10, 24, 16)
        button_layout.addStretch()

        self._cancel_btn = QPushButton("キャンセル")
        self._cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._cancel_btn.clicked.connect(self.close)

        self._save_btn = QPushButton("保存")
        self._save_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._save_btn.setProperty("class", "primary")  # プライマリボタンスタイル
        self._save_btn.clicked.connect(self._save_settings)

        button_layout.addWidget(self._cancel_btn)
        button_layout.addWidget(self._save_btn)
        rv.addLayout(button_layout)
        root.addWidget(right, 1)

        # ナビ選択でページ切替（履歴ページを開いた時点で一覧を最新化する）
        self._nav.currentRowChanged.connect(self._on_nav_changed)
        self._nav.setCurrentRow(self._home_page_index)

    @staticmethod
    def _brand_icon_pixmap(size: int) -> Optional[QPixmap]:
        """新ロゴ（リポジトリ直下 / 実行ファイル隣の icon.ico）を指定サイズで返す（無ければ None）。"""
        import sys
        from pathlib import Path

        candidates = []
        if getattr(sys, "frozen", False):
            candidates.append(Path(sys.executable).parent / "icon.ico")
            candidates.append(Path(getattr(sys, "_MEIPASS", "")) / "icon.ico")
        candidates.append(Path(__file__).resolve().parent.parent.parent / "icon.ico")
        for path in candidates:
            if path.exists():
                pixmap = QIcon(str(path)).pixmap(size, size)
                if not pixmap.isNull():
                    return pixmap
        return None

    def _make_toggle(self) -> ToggleSwitch:
        """テーマ追従するトグルスイッチを作って登録する。"""
        toggle = ToggleSwitch()
        toggle.set_dark(self._is_dark_mode)
        self._toggles.append(toggle)
        return toggle

    def _sidebar_icon_color(self) -> str:
        """サイドバーアイコンの通常色（テーマに追従）。"""
        return "#CFC9BE" if self._is_dark_mode else "#7F796F"

    def _apply_nav_icons(self) -> None:
        """ナビ各項目に現テーマ色のアイコンを設定する（選択時は白に切り替わる）。"""
        color = self._sidebar_icon_color()
        for i in range(self._nav.count()):
            title = self._nav_titles[i]
            if title is None:
                continue
            name = _NAV_ICON_NAME.get(title, "general")
            self._nav.item(i).setIcon(_make_nav_icon(name, color))
        self._nav_delegate.secondary_color = QColor(color)
        # ページ見出しのアイコンも選択中のページに合わせて描き直す
        row = self._nav.currentRow()
        if 0 <= row < len(self._nav_titles) and self._nav_titles[row]:
            self._set_page_header(self._nav_titles[row])

    def _set_page_header(self, title: str) -> None:
        """ページ見出し（アイコン・ページ名・説明 1 行）を出す。"""
        self._page_title.setText(title)
        self._page_subtitle.setText(_PAGE_SUBTITLES.get(title, ""))
        accent = MacTheme.Colors(self._is_dark_mode).ACCENT
        name = _NAV_ICON_NAME.get(title, "general")
        self._page_icon.setPixmap(_svg_pixmap(_nav_svg(name, accent), 26))

    def _refresh_nav_trailing(self) -> None:
        """録音キーの行の右端に、割り当て中のキー（例 右Ctrl）か「未割り当て」を出す。"""
        for slot_id in (1, 2):
            row = self._nav_row_of.get(f"録音キー {slot_id}")
            hotkey_input = getattr(self, f"_hotkey{slot_id}_input", None)
            if row is None or hotkey_input is None:
                continue
            self._nav.item(row).setData(ROLE_TRAILING, key_label(hotkey_input.text()))

    @staticmethod
    def _wrap_scroll(page: QWidget) -> QScrollArea:
        """タブページを縦スクロール可能にする（小さい画面でも全項目に届くように）。"""
        scroll = QScrollArea()
        scroll.setWidget(page)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        # 数 px の溢れで横スクロールバーが常駐するのを防ぐ（スクロールは縦のみ）
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        return scroll

    # ------------------------------------------------------------------
    # 一般ページ（Mac 版 GeneralSettingsTab と同項目 + Windows 版固有項目）
    # ------------------------------------------------------------------

    @staticmethod
    def _section_title(text: str) -> QLabel:
        """カードの上に置く見出し（Mac の Section の見出しに当たる）。"""
        label = QLabel(text)
        label.setObjectName("sectionTitle")
        label.setStyleSheet("font-weight: 600; padding: 4px 2px 0 2px;")
        return label

    def _new_page(self) -> tuple:
        """ページの土台（縦に見出し＋カードを積む）を作る。"""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(24, 8, 24, 24)
        layout.setSpacing(8)
        return page, layout

    def _hotkey_row(self, hotkey_input: "HotkeyInput", clear_text: str = "クリア") -> QWidget:
        """キー入力欄＋外すボタンの 1 行を作る。"""
        hotkey_input.setFixedWidth(200)
        clear = QPushButton(clear_text)
        clear.setCursor(Qt.CursorShape.PointingHandCursor)
        clear.clicked.connect(hotkey_input.clear)
        row = QWidget()
        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        lay.addWidget(hotkey_input)
        lay.addWidget(clear)
        lay.addStretch()
        return row

    # ------------------------------------------------------------------
    # 入力の基本（Mac の InputBasicsTab と同じ 3 グループ）
    # ------------------------------------------------------------------

    def _create_input_basics_page(self) -> QWidget:
        """入力の基本ページ（話す言語とマイク / 録音キーと一緒に使うキー / そのほかのショートカット）。"""
        page, layout = self._new_page()

        # --- 話す言語とマイク ---
        layout.addWidget(self._section_title("話す言語とマイク"))
        card, cl = _make_card()
        self._lang_combo = QComboBox()
        for label, value in _LANGUAGE_OPTIONS:
            self._lang_combo.addItem(label, value)
        self._lang_combo.setMinimumWidth(160)
        _add_row(
            cl, "話す言語", self._lang_combo,
            caption="文字起こしの精度が上がるので、決まっているなら選んでおくのがおすすめです。",
        )

        # 入力デバイス（一覧 + 更新 / 自動検出ボタンを 1 ブロックにまとめる）
        self._input_device_combo = QComboBox()
        refresh_button = QPushButton("更新")
        refresh_button.setCursor(Qt.CursorShape.PointingHandCursor)
        refresh_button.setToolTip("デバイス一覧を更新")
        refresh_button.clicked.connect(self._populate_input_devices)
        # マイク自動検出（Mac 版と同機能）: 押した後に喋ると声が入ったマイクを自動選択する
        self._mic_detect_button = QPushButton("自動検出")
        self._mic_detect_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._mic_detect_button.setToolTip("全マイクを監視し、喋った声が入ったマイクを自動選択します")
        self._mic_detect_button.clicked.connect(self._start_mic_auto_detect)
        device_block = QWidget()
        device_layout = QVBoxLayout(device_block)
        device_layout.setContentsMargins(0, 0, 0, 0)
        device_layout.setSpacing(6)
        device_head = QHBoxLayout()
        device_head.setSpacing(8)
        device_head.addWidget(QLabel("マイク"))
        device_head.addStretch()
        device_head.addWidget(refresh_button)
        device_head.addWidget(self._mic_detect_button)
        device_layout.addLayout(device_head)
        device_layout.addWidget(self._input_device_combo)
        # 検出中・検出結果の表示（待ち時間を可視化する。無表示の待ちはバグと区別できない）
        self._mic_detect_status = _make_caption("")
        self._mic_detect_status.setVisible(False)
        device_layout.addWidget(self._mic_detect_status)
        _add_block(cl, device_block)
        self._mic_detect_done.connect(self._on_mic_detect_done)
        # 利用権確認・キー登録の完了通知をメインスレッドで受ける
        self._entitlement_done.connect(self._on_entitlement_done)
        self._redeem_done.connect(self._on_redeem_done)

        # マイクテスト（録音キーを押さずに拾えているか確認・無料枠を消費しない）
        self._mic_monitor = MicLevelMonitor()
        self._mic_test_timer = QTimer(self)
        self._mic_test_timer.setInterval(33)
        self._mic_test_timer.timeout.connect(self._poll_mic_test_level)
        self._mic_test_button = QPushButton("テスト")
        self._mic_test_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._mic_test_button.clicked.connect(self._toggle_mic_test)
        self._mic_test_bar = QProgressBar()
        self._mic_test_bar.setRange(0, 100)
        self._mic_test_bar.setValue(0)
        self._mic_test_bar.setTextVisible(False)
        self._mic_test_bar.setFixedHeight(10)
        mic_test_row = QWidget()
        mic_test_layout = QHBoxLayout(mic_test_row)
        mic_test_layout.setContentsMargins(0, 0, 0, 0)
        mic_test_layout.setSpacing(8)
        mic_test_layout.addWidget(self._mic_test_button)
        mic_test_layout.addWidget(self._mic_test_bar, 1)
        _add_row(
            cl, "マイクテスト", mic_test_row,
            caption="「テスト」を押して声を出すと、拾えていればバーが動きます（録音キーは押さなくて大丈夫です）。",
        )
        layout.addWidget(card)

        # --- 録音キーと一緒に使うキー ---
        layout.addWidget(self._section_title("録音キーと一緒に使うキー"))
        card, cl = _make_card()
        # ハンズフリー切替キー（この切替キー＋ホットキーで toggle 録音になる。空＝無効）
        self._handsfree_input = HotkeyInput(platform_adapter=self._platform)
        _add_row(
            cl, "ハンズフリーキー", self._hotkey_row(self._handsfree_input),
            caption="押しながら録音キーを押すと、押しっぱなしにしなくても録音が続きます"
            "（もう一度録音キーで停止）。右 Shift などの修飾キーがおすすめです。",
        )
        # Auto Enter 遅延（ダブルタップ時、テキスト挿入後の Enter 押下までの待機時間）
        self._auto_enter_delay_slider = QSlider(Qt.Orientation.Horizontal)
        self._auto_enter_delay_slider.setRange(0, 500)
        self._auto_enter_delay_slider.setSingleStep(10)
        self._auto_enter_delay_slider.setPageStep(50)
        self._auto_enter_delay_slider.setFixedWidth(180)
        self._auto_enter_delay_label = QLabel("50 ミリ秒")
        self._auto_enter_delay_label.setMinimumWidth(64)
        self._auto_enter_delay_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self._auto_enter_delay_slider.valueChanged.connect(
            lambda v: self._auto_enter_delay_label.setText(f"{v} ミリ秒")
        )
        delay_row = QWidget()
        delay_row_layout = QHBoxLayout(delay_row)
        delay_row_layout.setContentsMargins(0, 0, 0, 0)
        delay_row_layout.setSpacing(8)
        delay_row_layout.addWidget(self._auto_enter_delay_slider)
        delay_row_layout.addWidget(self._auto_enter_delay_label)
        _add_row(
            cl, "2 回押しで送信するときの待ち時間", delay_row,
            caption="録音キーを素早く 2 回押すと、入力したあとに Enter も押します。その Enter までの待ち時間です。",
        )
        layout.addWidget(card)
        layout.addWidget(_make_caption("録音キーそのものは「録音キー 1」「録音キー 2」のページで決めます。"))

        # --- そのほかのショートカット ---
        layout.addWidget(self._section_title("そのほかのショートカット"))
        card, cl = _make_card()
        self._repaste_input = HotkeyInput(platform_adapter=self._platform)
        _add_row(
            cl, "直前の入力をもう一度貼り付け", self._hotkey_row(self._repaste_input),
            caption="最後に入力したテキストを、いまのカーソル位置へもう一度貼り付けます。"
            "Windows では Ctrl+Win+V や Ctrl+Alt+V が他の機能と重なるので、既定は未設定です。",
        )
        layout.addWidget(card)
        layout.addStretch()
        return self._wrap_scroll(page)

    # ------------------------------------------------------------------
    # 一般（Mac の GeneralSettingsTab: 画面の表示・音・起動・履歴）
    # ------------------------------------------------------------------

    def _create_general_page(self) -> QWidget:
        """一般ページ（画面の表示 / 音 / 起動 / 履歴）。"""
        page, layout = self._new_page()

        layout.addWidget(self._section_title("画面の表示"))
        card, cl = _make_card()
        # 待機中も小型ピルを常時表示する（Mac 版 hudAlwaysVisible と同義・既定 OFF）
        self._hud_always_visible_check = self._make_toggle()
        _add_row(
            cl, "待機中もピルを表示", self._hud_always_visible_check,
            caption="画面下の小さなピルを、録音していない間も出しておきます（フルスクリーンのアプリでは隠れます）。",
        )
        # 画面左端のサイドノッチ（履歴スリット）を表示する（Mac 版 sideNotchEnabled と同義・既定 ON）
        self._side_notch_check = self._make_toggle()
        _add_row(
            cl, "画面左端の履歴スリットを表示", self._side_notch_check,
            caption="クリックすると、これまでの入力の履歴を開いてコピーできます。",
        )
        layout.addWidget(card)

        layout.addWidget(self._section_title("音"))
        card, cl = _make_card()
        self._sound_effects_check = self._make_toggle()
        _add_row(
            cl, "操作音", self._sound_effects_check,
            caption="録音の開始と停止のときに、短い効果音を鳴らします。",
        )
        self._duck_media_check = self._make_toggle()
        _add_row(
            cl, "音声入力中はメディアの音量を下げる", self._duck_media_check,
            caption="録音している間だけ、再生中の音楽や動画の音量を下げて、停止すると元に戻します。",
        )
        layout.addWidget(card)

        layout.addWidget(self._section_title("起動"))
        card, cl = _make_card()
        # ログイン時に自動起動（Windows のみ）。状態はレジストリ側が真実なので settings.yaml には保存しない
        self._autostart_check = self._make_toggle()
        self._autostart_check.setEnabled(autostart.is_supported())
        _add_row(
            cl, "ログイン時に起動", self._autostart_check,
            caption="" if autostart.is_supported() else "この機能は Windows でのみ利用できます。",
        )
        guide_button = QPushButton("セットアップガイドを開く")
        guide_button.setCursor(Qt.CursorShape.PointingHandCursor)
        guide_button.clicked.connect(self.open_onboarding_requested.emit)
        _add_row(cl, "はじめかた", guide_button, caption="使い方をはじめから、もう一度見られます。")
        layout.addWidget(card)

        layout.addWidget(self._section_title("履歴"))
        card, cl = _make_card()
        self._history_enabled_check = self._make_toggle()
        _add_row(
            cl, "入力の履歴を保存", self._history_enabled_check,
            caption="ホームの最近の履歴と、画面左端の履歴スリットに使います。オフにすると新しい入力を残しません。",
        )
        layout.addWidget(card)
        layout.addWidget(self._create_history_sync_card())
        layout.addStretch()
        return self._wrap_scroll(page)

    # ------------------------------------------------------------------
    # ホットキーページ（Mac 版 SlotSettingsTab と同項目・同文言）
    # ------------------------------------------------------------------

    def _create_slot_page(self, slot_id: int) -> QWidget:
        """
        録音キーのページを作成する（キー → 文字起こし → 文章の整形・Mac の SlotSettingsTab と同じ順）。

        Args:
            slot_id: スロットID（1または2）

        Returns:
            ホットキー設定のページ
        """
        page, layout = self._new_page()

        # --- キー（キー・重複警告・押し方をキーの直下に）---
        layout.addWidget(self._section_title("キー"))
        card, cl = _make_card()
        # ホットキー入力（空＝未割り当て。未割り当てのスロットは録音しない）
        hotkey_input = HotkeyInput(platform_adapter=self._platform, empty_label="未割り当て")
        setattr(self, f"_hotkey{slot_id}_input", hotkey_input)
        warn = _make_caption("もう一方の録音キーと同じキーです。どちらかを変えてください")
        warn.setStyleSheet(MacTheme.status_warn_style(self._is_dark_mode))
        warn.setVisible(False)
        setattr(self, f"_hotkey{slot_id}_warning", warn)
        key_block = QWidget()
        kb = QVBoxLayout(key_block)
        kb.setContentsMargins(0, 0, 0, 0)
        kb.setSpacing(4)
        kb.addWidget(self._hotkey_row(hotkey_input, "割り当てを外す"))
        kb.addWidget(warn)
        _add_row(
            cl, "録音キー", key_block,
            caption="枠をクリックしてからキーを押すと割り当てます（ESC で取り消し）。右 Ctrl のように修飾キー 1 つでも使えます。",
        )
        hotkey_input.textChanged.connect(lambda _t: self._on_hotkey_changed())

        # 押し方はエンジンやモデルとは関係の無い「キーの使い方」なので、キーの行の直下に置く
        mode_combo = QComboBox()
        for value, label in _MODE_LABELS:
            mode_combo.addItem(label, value)
        mode_combo.setMinimumWidth(180)
        setattr(self, f"_mode{slot_id}_combo", mode_combo)
        mode_caption = _make_caption(_mode_caption(HotkeyMode.HOLD.value))
        setattr(self, f"_mode{slot_id}_caption", mode_caption)
        mode_combo.currentIndexChanged.connect(
            lambda _i, c=mode_combo, cap=mode_caption: cap.setText(_mode_caption(c.currentData()))
        )
        _add_row(cl, "キーの押し方", mode_combo, caption_widget=mode_caption)
        layout.addWidget(card)

        # --- 文字起こし（エンジンとモデルを 1 つのポップアップで決める）---
        layout.addWidget(self._section_title("文字起こし"))
        card, cl = _make_card()
        engine_button = GroupedChoiceButton()
        engine_button.setMinimumWidth(240)
        setattr(self, f"_engine{slot_id}_button", engine_button)
        backend_caption = _make_caption("")
        setattr(self, f"_backend{slot_id}_caption", backend_caption)
        engine_button.chosen.connect(lambda value, sid=slot_id: self._set_slot_choice(sid, value))
        _add_row(cl, "文字起こしエンジン", engine_button, caption_widget=backend_caption)
        layout.addWidget(card)
        layout.addWidget(_make_caption(
            "一覧は日本語で使うときのおすすめ順です。「API キー」ページで、使うサービスのキーを入れてください。"
        ))

        # --- 文章の整形 ---
        layout.addWidget(self._section_title("文章の整形"))
        card, cl = _make_card()
        format_check = self._make_toggle()
        setattr(self, f"_format{slot_id}_check", format_check)
        _add_row(
            cl, "文章を自動で整える", format_check,
            caption="文字起こしのあと、AI（Groq）が「プロンプト」ページの指示で句読点や言いよどみを整えます。"
            "オフにすると、文字起こしをそのまま入力します（いちばん速い）。",
        )
        prompt_row = QWidget()
        pr = QVBoxLayout(prompt_row)
        pr.setContentsMargins(0, 0, 0, 0)
        pr.setSpacing(4)
        prompt_head = QHBoxLayout()
        prompt_head.addWidget(QLabel("使う指示"))
        prompt_head.addStretch()
        edit_btn = QPushButton("プロンプトを編集")
        edit_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        edit_btn.clicked.connect(lambda: self.select_page("プロンプト"))
        prompt_head.addWidget(edit_btn)
        pr.addLayout(prompt_head)
        summary = _make_caption("")
        setattr(self, f"_prompt_summary{slot_id}", summary)
        pr.addWidget(summary)
        _add_block(cl, prompt_row)
        # 整形がオフのときは「使う指示」を出さない（Mac と同じ。表示の切り替えは _sync_format_toggle）
        setattr(self, f"_prompt_row{slot_id}", prompt_row)
        prompt_row.setVisible(False)
        format_check.toggled.connect(lambda on, sid=slot_id: self._sync_format_toggle(sid, on))
        layout.addWidget(card)

        layout.addStretch()
        return self._wrap_scroll(page)

    def _set_slot_choice(self, slot_id: int, value) -> None:
        """エンジン＋モデルを選んだ: 選択を覚えてポップアップと説明を描き直す。

        エンジンとモデルが 1 回で決まるので、エンジン変更で推奨モデルへ切り替える処理は要らない。
        整形のオン/オフは利用者が決めた設定なのでエンジンを変えても触らない（Mac と同じ）。
        """
        backend, model = value
        self._slot_choice[slot_id] = (backend, model)
        self._render_engine_button(slot_id)

    def _render_engine_button(self, slot_id: int) -> None:
        """エンジンのポップアップと説明文を、いまの選択で描き直す。"""
        backend, model = self._slot_choice[slot_id]
        groups = [
            (title, [((b, m), label) for b, m, label in options])
            for title, options in backends.picker_groups(backend, model)
        ]
        button = getattr(self, f"_engine{slot_id}_button")
        button.set_groups(groups, (backend, backends.resolve_model(backend, model)),
                          backends.choice_title(backend, model))
        getattr(self, f"_backend{slot_id}_caption").setText(_backend_caption_text(backend))
        # プロンプトページの「この指示で整える録音キー」の説明（使うエンジン）も合わせる
        if hasattr(self, "_prompt_slot_captions"):
            self._refresh_prompt_slot_rows()

    def _on_hotkey_changed(self) -> None:
        """録音キーが変わった: 重複の警告・サイドバーの右端・プロンプトページの表示を更新する。"""
        a = self._hotkey1_input.text() if hasattr(self, "_hotkey1_input") else ""
        b = self._hotkey2_input.text() if hasattr(self, "_hotkey2_input") else ""
        conflict = keys_conflict(a, b)
        for slot_id in (1, 2):
            warn = getattr(self, f"_hotkey{slot_id}_warning", None)
            if warn is not None:
                warn.setVisible(conflict)
        if hasattr(self, "_nav_row_of"):
            self._refresh_nav_trailing()
        if hasattr(self, "_prompt_slot_captions"):
            self._refresh_prompt_slot_rows()
        if hasattr(self, "_translate_warning_label"):
            self._refresh_translate_warnings()

    def _sync_format_toggle(self, slot_id: int, on: bool) -> None:
        """録音キーページの整形トグルとプロンプトページの同じトグルをそろえる（正本は 1 つ）。"""
        # シグナルは止めない（ToggleSwitch は toggled で自分のつまみを動かすため、止めると見た目がずれる）。
        # 代わりに状態が同じなら何もしないことで、相互に呼び合っても 1 往復で止まる
        for name in (f"_format{slot_id}_check", f"_prompt_slot{slot_id}_check"):
            toggle = getattr(self, name, None)
            if toggle is not None and toggle.isChecked() != on:
                toggle.setChecked(on)
        row = getattr(self, f"_prompt_row{slot_id}", None)
        if row is not None:
            row.setVisible(on)
        if hasattr(self, "_prompt_slot_captions"):
            self._refresh_prompt_slot_rows()

    # ------------------------------------------------------------------
    # プロンプト（Mac の PromptSettingsTab: 整形の指示を録音キー 1・2 共通で 1 か所で編集）
    # ------------------------------------------------------------------

    def _create_prompt_page(self) -> QWidget:
        """プロンプトページ（整形の指示 / この指示で整える録音キー / AI モデル）。"""
        page, layout = self._new_page()
        self._previous_prompt: Optional[str] = None

        layout.addWidget(self._section_title("整形の指示（プロンプト）"))
        card, cl = _make_card()
        block = QWidget()
        bl = QVBoxLayout(block)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(8)
        head = QHBoxLayout()
        head.setSpacing(8)
        self._prompt_status = _make_caption("")
        head.addWidget(self._prompt_status, 1)
        # ひな形から選ぶ（選ぶと指示文がその内容に置き換わる。直前の文は「元に戻す」で戻せる）
        self._template_button = GroupedChoiceButton()
        self._template_button.set_groups(
            [(None, [(t["id"], t["title"]) for t in text_formatter.format_prompt_templates()])],
            None, "ひな形から選ぶ",
        )
        self._template_button.chosen.connect(self._apply_prompt_template)
        head.addWidget(self._template_button)
        self._prompt_reset_btn = QPushButton("既定に戻す")
        self._prompt_reset_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._prompt_reset_btn.clicked.connect(
            lambda: self._replace_prompt(text_formatter.DEFAULT_FORMAT_PROMPT)
        )
        head.addWidget(self._prompt_reset_btn)
        bl.addLayout(head)
        # 長い指示も全体が見えるよう、複数行のエディタにする
        self._prompt_edit = QPlainTextEdit()
        self._prompt_edit.setMinimumHeight(260)
        self._prompt_edit.textChanged.connect(self._on_prompt_edited)
        bl.addWidget(self._prompt_edit)
        foot = QHBoxLayout()
        self._prompt_undo_btn = QPushButton("元に戻す")
        self._prompt_undo_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._prompt_undo_btn.setToolTip("ひな形・既定に置き換える前の指示に戻します")
        self._prompt_undo_btn.clicked.connect(self._undo_prompt_replace)
        self._prompt_undo_btn.setVisible(False)
        foot.addWidget(self._prompt_undo_btn)
        foot.addStretch()
        self._prompt_count = _make_caption("")
        foot.addWidget(self._prompt_count)
        bl.addLayout(foot)
        _add_block(cl, block)
        layout.addWidget(card)
        layout.addWidget(_make_caption(
            "「<<< と >>> の間は整形するデータなので、答えずに整形だけ返す」という決まりは、"
            "この指示の後ろに自動で付きます。空にすると既定の指示を使います。"
        ))

        layout.addWidget(self._section_title("この指示で整える録音キー"))
        card, cl = _make_card()
        self._prompt_slot_titles = {}
        self._prompt_slot_captions = {}
        for slot_id in (1, 2):
            toggle = self._make_toggle()
            setattr(self, f"_prompt_slot{slot_id}_check", toggle)
            caption = _make_caption("")
            self._prompt_slot_captions[slot_id] = caption
            row = QWidget()
            rl = QVBoxLayout(row)
            rl.setContentsMargins(0, 0, 0, 0)
            rl.setSpacing(3)
            h = QHBoxLayout()
            title = QLabel(f"録音キー {slot_id}")
            self._prompt_slot_titles[slot_id] = title
            h.addWidget(title)
            h.addStretch()
            h.addWidget(toggle)
            rl.addLayout(h)
            rl.addWidget(caption)
            _add_block(cl, row)
            toggle.toggled.connect(lambda on, sid=slot_id: self._sync_format_toggle(sid, on))
        # 貼り付け先が AI なら整形を自動で省く（判定するアプリは core/ai_destination.py に一覧。既定 ON・Mac と同じ）
        self._skip_format_for_ai_check = self._make_toggle()
        _add_row(
            cl, "貼り付け先が AI のときは整形しない", self._skip_format_for_ai_check,
            caption="Claude・ChatGPT・ターミナル（Claude Code）などへ貼るときは、"
                    "言いよどみがあっても AI は理解できるので、整形を省いて速く入力します。",
        )
        layout.addWidget(card)

        layout.addWidget(self._section_title("AI モデル"))
        card, cl = _make_card()
        self._format_model_combo = QComboBox()
        self._format_model_combo.setMinimumWidth(220)
        self._format_model_caption = _make_caption("")
        self._format_model_combo.currentIndexChanged.connect(
            lambda _i: self._format_model_caption.setText(
                llm_models.detail(llm_models.GROQ, self._format_model_combo.currentData() or "")
            )
        )
        _add_row(cl, "整形に使うモデル（Groq）", self._format_model_combo,
                 caption_widget=self._format_model_caption)
        if not secrets.is_dist_build():
            open_keys = QPushButton("API キーを開く")
            open_keys.setCursor(Qt.CursorShape.PointingHandCursor)
            open_keys.clicked.connect(self.select_api_keys)
            _add_row(cl, "Groq の API キー", open_keys, caption="「API キー」ページの Groq の欄に入れます。")
        layout.addWidget(card)
        layout.addWidget(_make_caption(
            "整形には Groq の API キーが要ります。失敗したときは整形せず、文字起こしをそのまま入力します。"
        ))
        layout.addStretch()
        return self._wrap_scroll(page)

    def _fill_format_model_combo(self, current: str) -> None:
        """整形モデルの選択肢（おすすめ → 既知 → 一覧に無い保存値）を入れ直す。"""
        combo = self._format_model_combo
        combo.blockSignals(True)
        combo.clear()
        for model_id, label in llm_models.options(llm_models.GROQ, current):
            combo.addItem(label, model_id)
        index = combo.findData(current)
        combo.setCurrentIndex(max(0, index))
        combo.blockSignals(False)
        self._format_model_caption.setText(llm_models.detail(llm_models.GROQ, combo.currentData() or ""))

    def _on_prompt_edited(self) -> None:
        """指示文が書き換わった: 状態・文字数・ボタンの有効無効を更新する。"""
        text = self._prompt_edit.toPlainText()
        self._prompt_count.setText(f"{len(text)} 文字")
        self._prompt_status.setText(text_formatter.prompt_summary(text))
        is_default = text.strip() == text_formatter.DEFAULT_FORMAT_PROMPT.strip()
        self._prompt_reset_btn.setEnabled(not is_default)
        for slot_id in (1, 2):
            label = getattr(self, f"_prompt_summary{slot_id}", None)
            if label is not None:
                label.setText(text_formatter.prompt_summary(text))

    def _replace_prompt(self, new_text: str) -> None:
        """指示文を置き換える（直前の文を「元に戻す」用に覚える）。"""
        current = self._prompt_edit.toPlainText()
        if current == new_text:
            return
        self._previous_prompt = current
        self._prompt_edit.setPlainText(new_text)
        self._prompt_undo_btn.setVisible(True)

    def _apply_prompt_template(self, template_id: str) -> None:
        """ひな形を選んだ: 指示文をその内容に置き換える。"""
        for template in text_formatter.format_prompt_templates():
            if template["id"] == template_id:
                self._replace_prompt(template["prompt"])
                return

    def _undo_prompt_replace(self) -> None:
        """「元に戻す」: ひな形・既定に置き換える前の指示に戻す。"""
        if self._previous_prompt is not None:
            self._prompt_edit.setPlainText(self._previous_prompt)
        self._previous_prompt = None
        self._prompt_undo_btn.setVisible(False)

    def _refresh_prompt_slot_rows(self) -> None:
        """「この指示で整える録音キー」の名前（割り当て中のキー）と説明（整えるか・エンジン）を更新する。"""
        for slot_id in (1, 2):
            hotkey_input = getattr(self, f"_hotkey{slot_id}_input", None)
            key = key_label(hotkey_input.text()) if hotkey_input is not None else ""
            self._prompt_slot_titles[slot_id].setText(f"録音キー {slot_id}（{key}）")
            choice = getattr(self, "_slot_choice", {}).get(slot_id)
            engine = backends.choice_title(*choice) if choice else ""
            toggle = getattr(self, f"_format{slot_id}_check", None)
            on = toggle.isChecked() if toggle is not None else False
            self._prompt_slot_captions[slot_id].setText(
                f"整える（{engine} で文字起こししたあと）" if on
                else f"整えない（{engine} の文字起こしをそのまま入力）"
            )

    # ------------------------------------------------------------------
    # 翻訳して入力（Mac の TranslateInputTab: 翻訳するとき / 翻訳のしかた）
    # ------------------------------------------------------------------

    def _create_translate_page(self) -> Optional[QWidget]:
        """翻訳して入力ページ（1 回だけ翻訳するキー・いつも翻訳／訳す先の言語・翻訳エンジン）。

        Mac の翻訳エンジンは Apple（端末内）と Groq だが、Windows には Apple 翻訳が無いので
        Groq（既定）と Gemini から選ぶ。
        """
        page, layout = self._new_page()

        layout.addWidget(self._section_title("翻訳するとき"))
        card, cl = _make_card()
        # たまに 1 回だけ訳す使い方が主なので、キーを先頭に置く（常時 ON は下のトグル）
        self._translate_once_input = HotkeyInput(platform_adapter=self._platform)
        key_block = QWidget()
        kb = QVBoxLayout(key_block)
        kb.setContentsMargins(0, 0, 0, 0)
        kb.setSpacing(4)
        kb.addWidget(self._hotkey_row(self._translate_once_input))
        # 録音キー・ハンズフリーキー・再貼り付けキーと重なると黙って効かなくなるので、ここで知らせる
        self._translate_warning_label = _make_caption("")
        self._translate_warning_label.setStyleSheet(MacTheme.status_warn_style(self._is_dark_mode))
        self._translate_warning_label.setVisible(False)
        kb.addWidget(self._translate_warning_label)
        _add_row(
            cl, "この 1 回だけ翻訳するキー", key_block,
            caption="録音中にこのキーを押すと（録音キーと同時でも、話している途中でも）、その回だけ翻訳して貼り付けます。"
            "翻訳される回は録音中の表示に「→EN」のような印が出ます。録音中にもう一度押すと取り消せます。",
        )
        for widget in (self._translate_once_input, self._handsfree_input, self._repaste_input):
            widget.textChanged.connect(lambda _t: self._refresh_translate_warnings())
        self._translate_always_check = self._make_toggle()
        _add_row(
            cl, "いつも翻訳して入力する", self._translate_always_check,
            caption="話した内容を毎回翻訳してから貼り付けます（例: 日本語で話す → 英語が入力される）。",
        )
        layout.addWidget(card)

        layout.addWidget(self._section_title("翻訳のしかた"))
        card, cl = _make_card()
        self._translate_target_combo = QComboBox()
        from ..core import dictation_translator

        for code, label in dictation_translator.TARGET_LANGUAGES:
            self._translate_target_combo.addItem(label, code)
        self._translate_target_combo.setMinimumWidth(160)
        _add_row(cl, "訳す先の言語", self._translate_target_combo)
        # エンジンとモデルを 1 つのポップアップで決める（録音キーの文字起こしエンジンと同じ形）
        self._translate_engine_button = GroupedChoiceButton()
        self._translate_engine_button.setMinimumWidth(240)
        self._translate_engine_caption = _make_caption("")
        self._translate_choice = ("groq", llm_models.RECOMMENDED_GROQ, llm_models.RECOMMENDED_GEMINI)
        self._translate_engine_button.chosen.connect(self._set_translate_choice)
        _add_row(cl, "翻訳エンジン", self._translate_engine_button, caption_widget=self._translate_engine_caption)
        layout.addWidget(card)
        layout.addWidget(_make_caption(
            "「文章を自動で整える」と同時に使うと、整形と翻訳で 2 回 LLM を呼びます。"
            "速さを優先するなら、録音キーの設定で整形をオフにしてください。"
            "翻訳に失敗したときは原文がそのまま入力されます（文章を失いません）。"
        ))
        layout.addStretch()
        return self._wrap_scroll(page)

    def _set_translate_choice(self, value) -> None:
        """翻訳エンジン＋モデルを選んだ（Groq / Gemini それぞれのモデルは別々に覚える）。"""
        engine, model = value
        _, groq_model, gemini_model = self._translate_choice
        if engine == "gemini":
            gemini_model = model
        else:
            groq_model = model
        self._translate_choice = (engine, groq_model, gemini_model)
        self._render_translate_engine()

    def _render_translate_engine(self) -> None:
        """翻訳エンジンのポップアップと説明を、いまの選択で描き直す。"""
        engine, groq_model, gemini_model = self._translate_choice
        groups = [
            ("Groq", [(("groq", mid), label) for mid, label in llm_models.options(llm_models.GROQ, groq_model)]),
            ("Gemini（有料の場合あり）",
             [(("gemini", mid), label) for mid, label in llm_models.options(llm_models.GEMINI, gemini_model)]),
        ]
        current_model = gemini_model if engine == "gemini" else groq_model
        provider = llm_models.GEMINI if engine == "gemini" else llm_models.GROQ
        name = next((info.name for info in llm_models.known(provider) if info.id == current_model), current_model)
        self._translate_engine_button.set_groups(
            groups, (engine, current_model), f"{'Gemini' if engine == 'gemini' else 'Groq'} · {name}"
        )
        if engine == "gemini":
            detail = (
                "Google の Gemini で翻訳します。無料枠を超えると課金されます（「API キー」ページの Gemini の欄にキーが要ります）。\n"
                + llm_models.detail(llm_models.GEMINI, current_model)
            )
        else:
            detail = (
                "Groq の LLM で翻訳します。話し言葉のニュアンスに強い代わりに、通信と API キーが必要です。\n"
                + llm_models.detail(llm_models.GROQ, current_model)
            )
        self._translate_engine_caption.setText(detail)

    def _refresh_translate_warnings(self) -> None:
        """1 回翻訳キーが他のキー設定と重なっていたら警告を出す（Mac と同じ文言）。"""
        if not hasattr(self, "_translate_warning_label"):
            return
        warnings = translate_once_key_warnings(
            self._translate_once_input.text(),
            [self._hotkey1_input.text(), self._hotkey2_input.text()],
            self._handsfree_input.text(),
            self._repaste_input.text(),
        )
        self._translate_warning_label.setText("\n".join(warnings))
        self._translate_warning_label.setVisible(bool(warnings))

    # ------------------------------------------------------------------
    # ホームページ（Mac 版 HomeView のダッシュボードと同構成の 3 カード + 最近の履歴）
    # ------------------------------------------------------------------

    @staticmethod
    def _home_card() -> tuple:
        """ダッシュボードのカード（角丸の面）と縦レイアウトを作る。"""
        frame = QFrame()
        frame.setObjectName("card")
        lay = QVBoxLayout(frame)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(8)
        return frame, lay

    def _create_home_page(self) -> QWidget:
        """ホーム（ダッシュボード）を作成する。

        Mac 版 HomeView の主役 3 カード（累計入力 / 節約できた時間 / この期間）を再現し、
        下に最近の履歴（直近 8 件・クリックでコピー）を置く。データ源は StatsStore / HistoryStore
        （Mac と計算式・しきい値を一致させる）。その下に API の利用料金（今日 / 今月 / 累計・内訳・
        直近 30 日）を置く。アプリ別使用状況は Windows では未対応（履歴にはアプリ名が残る）。
        """
        # 期間カードの選択（1=今日 / 7=今週）。既定は今日
        self._home_period_days = 1

        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(24, 12, 24, 24)
        layout.setSpacing(16)

        # --- ヘッダ（見出し + 今日の一言） ---
        header = QVBoxLayout()
        header.setSpacing(2)
        title = QLabel("ダッシュボード")
        title.setStyleSheet("font-size: 18px; font-weight: 700;")
        header.addWidget(title)
        self._home_greeting = _make_caption("今日はまだ入力していません")
        header.addWidget(self._home_greeting)
        layout.addLayout(header)

        # --- 上段: 累計入力 + 節約できた時間（横並び） ---
        top_row = QHBoxLayout()
        top_row.setSpacing(12)
        top_row.addWidget(self._build_cumulative_card(), 1)
        top_row.addWidget(self._build_saved_card(), 1)
        layout.addLayout(top_row)

        # --- 下段: この期間（全幅・今日/今週トグル） ---
        layout.addWidget(self._build_period_card())

        # --- API の利用料金（今日 / 今月 / 累計・内訳・直近 30 日・Mac のホームと同じ） ---
        layout.addLayout(self._build_api_cost_section())

        # --- 最近の履歴（直近 8 件・クリックでコピー） ---
        recent_label = QLabel("最近の履歴")
        recent_label.setStyleSheet("font-size: 14px; font-weight: 600;")
        layout.addWidget(recent_label)
        self._home_history_list = QListWidget()
        self._home_history_list.setObjectName("historyList")
        self._home_history_list.setWordWrap(True)
        self._home_history_list.setMinimumHeight(140)
        self._home_history_list.itemClicked.connect(self._copy_history_item)
        layout.addWidget(self._home_history_list)

        layout.addStretch()
        return self._wrap_scroll(page)

    def _build_cumulative_card(self) -> QFrame:
        """カード「累計入力」（累計文字数 + 回数/録音時間 + レベル進捗）。"""
        card, cl = self._home_card()
        cl.addWidget(_make_caption("累計入力"))
        # 主役: 累計文字数
        big_row = QHBoxLayout()
        big_row.setSpacing(4)
        self._home_total_chars = QLabel("0")
        self._home_total_chars.setStyleSheet("font-size: 26px; font-weight: 700;")
        big_row.addWidget(self._home_total_chars)
        unit = QLabel("文字")
        unit.setStyleSheet("font-size: 13px;")
        big_row.addWidget(unit, 0, Qt.AlignmentFlag.AlignBottom)
        big_row.addStretch()
        cl.addLayout(big_row)
        # 補足: 回数・録音時間
        self._home_total_sub = _make_caption("0 回 · 録音 0秒")
        cl.addWidget(self._home_total_sub)
        # レベル進捗（累計文字数を経験値としたレベル制）
        level_row = QHBoxLayout()
        self._home_level_label = QLabel("レベル 1")
        self._home_level_label.setStyleSheet("font-size: 13px; font-weight: 600;")
        level_row.addWidget(self._home_level_label)
        level_row.addStretch()
        self._home_next_label = _make_caption("あと 0 文字で Lv.2")
        level_row.addWidget(self._home_next_label)
        cl.addLayout(level_row)
        self._home_level_progress = QProgressBar()
        self._home_level_progress.setRange(0, 100)
        self._home_level_progress.setValue(0)
        self._home_level_progress.setTextVisible(False)
        self._home_level_progress.setFixedHeight(6)
        cl.addWidget(self._home_level_progress)
        return card

    def _build_saved_card(self) -> QFrame:
        """カード「節約できた時間」（推定節約時間 + 身近なものへの換算）。"""
        card, cl = self._home_card()
        cl.addWidget(_make_caption("節約できた時間"))
        self._home_saved_value = QLabel("0 秒")
        self._home_saved_value.setStyleSheet("font-size: 24px; font-weight: 700;")
        cl.addWidget(self._home_saved_value)
        self._home_saved_comparison = _make_caption("これから時間が積み上がっていきます")
        self._home_saved_comparison.setWordWrap(True)
        cl.addWidget(self._home_saved_comparison)
        cl.addStretch()
        return card

    def _build_period_card(self) -> QFrame:
        """カード「この期間」（今日/今週トグル + 期間内の文字数/録音/回数）。"""
        card, cl = self._home_card()
        head = QHBoxLayout()
        head.addWidget(_make_caption("この期間"))
        head.addStretch()
        # 今日=直近1日 / 今週=直近7日（既存 daily_series の日数と対応）
        self._home_today_btn = QPushButton("今日")
        self._home_week_btn = QPushButton("今週")
        for btn, days in ((self._home_today_btn, 1), (self._home_week_btn, 7)):
            btn.setCheckable(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            btn.clicked.connect(lambda _=False, d=days: self._set_home_period(d))
            head.addWidget(btn)
        self._home_today_btn.setChecked(True)
        cl.addLayout(head)
        # 主役: 期間内の文字数
        big_row = QHBoxLayout()
        big_row.setSpacing(4)
        self._home_period_chars = QLabel("0")
        self._home_period_chars.setStyleSheet("font-size: 26px; font-weight: 700;")
        big_row.addWidget(self._home_period_chars)
        unit = QLabel("文字")
        unit.setStyleSheet("font-size: 13px;")
        big_row.addWidget(unit, 0, Qt.AlignmentFlag.AlignBottom)
        big_row.addStretch()
        cl.addLayout(big_row)
        self._home_period_sub = _make_caption("録音 0秒 · 0 回")
        cl.addWidget(self._home_period_sub)
        return card

    # ------------------------------------------------------------------
    # API の利用料金（Mac の HomeView.apiCostSection）
    # ------------------------------------------------------------------

    def _build_api_cost_section(self) -> QVBoxLayout:
        """API の利用料金（今日 / 今月 / 累計の合計・内訳・直近 30 日の推移）。

        右上で「定価（無料枠も含む）」と「実際に払った分」を切り替える（設定 api_cost_mode にすぐ保存）。
        料金は使用量 × 公開単価の推定なので、その旨を小さく添える。
        """
        from ..core import api_usage

        self._api_cost_period = "month"
        box = QVBoxLayout()
        box.setSpacing(8)
        head = QHBoxLayout()
        title = QLabel("API の利用料金")
        title.setStyleSheet("font-size: 14px; font-weight: 600;")
        head.addWidget(title)
        head.addStretch()
        self._api_cost_mode_buttons = {}
        for mode, label in ((api_usage.MODE_LIST, "定価（無料枠も含む）"), (api_usage.MODE_PAID, "実際に払った分")):
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _=False, m=mode: self._set_api_cost_mode(m))
            self._api_cost_mode_buttons[mode] = btn
            head.addWidget(btn)
        box.addLayout(head)

        totals = QHBoxLayout()
        totals.setSpacing(12)
        self._api_cost_totals = {}
        for key, label in (("today", "今日"), ("month", "今月"), ("all", "累計")):
            card, cl = self._home_card()
            cl.addWidget(_make_caption(label))
            value = QLabel("$0.00")
            value.setStyleSheet("font-size: 24px; font-weight: 700;")
            cl.addWidget(value)
            note = _make_caption("")
            cl.addWidget(note)
            self._api_cost_totals[key] = (value, note)
            totals.addWidget(card, 1)
        box.addLayout(totals)

        # 内訳（プロバイダー × モデル × 用途。期間は 今日 / 今月 / 累計）
        card, cl = self._home_card()
        bh = QHBoxLayout()
        bh.addWidget(_make_caption("内訳"))
        bh.addStretch()
        self._api_cost_period_buttons = {}
        for key, label in (("today", "今日"), ("month", "今月"), ("all", "累計")):
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _=False, k=key: self._set_api_cost_period(k))
            self._api_cost_period_buttons[key] = btn
            bh.addWidget(btn)
        cl.addLayout(bh)
        self._api_cost_rows = QVBoxLayout()
        self._api_cost_rows.setSpacing(4)
        cl.addLayout(self._api_cost_rows)
        box.addWidget(card)

        # 直近 30 日の推移
        card, cl = self._home_card()
        th = QHBoxLayout()
        th.addWidget(_make_caption("直近 30 日"))
        th.addStretch()
        self._api_cost_trend_total = _make_caption("")
        th.addWidget(self._api_cost_trend_total)
        cl.addLayout(th)
        self._api_cost_trend = CostTrendBars()
        cl.addWidget(self._api_cost_trend)
        box.addWidget(card)

        self._api_cost_footnote = _make_caption("")
        box.addWidget(self._api_cost_footnote)
        return box

    def _api_cost_mode(self) -> str:
        """保存されている料金の表示モード（無い・壊れた値は定価）。"""
        from ..core import api_usage

        mode = self._config_manager.get("api_cost_mode", api_usage.MODE_LIST)
        return mode if mode in (api_usage.MODE_LIST, api_usage.MODE_PAID) else api_usage.MODE_LIST

    def _set_api_cost_mode(self, mode: str) -> None:
        """料金の表示モードを切り替える（表示の設定なので、保存ボタンを待たずにすぐ保存する）。"""
        self._config_manager.save({"api_cost_mode": mode})
        self._refresh_api_cost()

    def _set_api_cost_period(self, period: str) -> None:
        """内訳の期間（今日 / 今月 / 累計）を切り替える。"""
        self._api_cost_period = period
        self._refresh_api_cost()

    def _refresh_api_cost(self) -> None:
        """API の利用料金の表示を、記録の最新値で描き直す。"""
        from ..core import api_usage

        if not hasattr(self, "_api_cost_totals"):
            return
        store = api_usage.shared()
        mode = self._api_cost_mode()
        for m, btn in self._api_cost_mode_buttons.items():
            btn.setChecked(m == mode)
        for key, summary in (("today", store.today(mode)), ("month", store.month(mode)), ("all", store.all_time(mode))):
            value, note = self._api_cost_totals[key]
            value.setText(api_usage.formatted_usd(summary.usd))
            # 単価が分からない分を 0 円に見せて隠さない
            note.setText("単価未確認を含む" if summary.has_unpriced else "")
        for k, btn in self._api_cost_period_buttons.items():
            btn.setChecked(k == self._api_cost_period)
        breakdown = {"today": store.today, "month": store.month}.get(self._api_cost_period, None)
        rows = (breakdown(mode) if breakdown else store.all_time(mode)).rows
        while self._api_cost_rows.count():
            item = self._api_cost_rows.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        if not rows:
            self._api_cost_rows.addWidget(_make_caption("この期間に API の利用はありません。"))
        for row in rows:
            line = QWidget()
            ll = QHBoxLayout(line)
            ll.setContentsMargins(0, 2, 0, 2)
            left = QVBoxLayout()
            left.setSpacing(0)
            left.addWidget(QLabel(f"{api_usage.model_label(row.provider)} · {row.model}"))
            if row.audioSeconds > 0:
                quantity = self._format_minutes(row.audioSeconds)
                if row.billedAudioSeconds >= row.audioSeconds + 1:
                    quantity += f"（課金対象 {self._format_minutes(row.billedAudioSeconds)}）"
            else:
                quantity = f"入力 {self._grouped(row.inputTokens)} / 出力 {self._grouped(row.outputTokens)} トークン"
            purpose = api_usage.PURPOSE_LABELS.get(row.purpose, row.purpose)
            left.addWidget(_make_caption(f"{purpose} · {quantity} · {self._grouped(row.requests)} 回"))
            ll.addLayout(left, 1)
            if api_usage.is_free_tier(mode, row.provider):
                price_text = "無料枠"
            else:
                c = row.cost_usd(mode)
                price_text = api_usage.formatted_usd(c) if c is not None else "単価未確認"
            ll.addWidget(QLabel(price_text))
            self._api_cost_rows.addWidget(line)
        series = store.daily_series(30, mode)
        self._api_cost_trend.set_values([usd for _, usd in series])
        self._api_cost_trend_total.setText(f"合計 {api_usage.formatted_usd(sum(usd for _, usd in series))}")
        self._api_cost_footnote.setText(
            "使用量 × 各社の公開単価から計算した推定で、実際の請求額とは異なることがあります。"
            f"単価は {api_usage.CHECKED_ON} 時点（Gemini は有料枠の単価で計算）。Windows は 2026-10-19 以降の分だけを数えます。"
            + ("「実際に払った分」は OpenAI・Soniox・Microsoft・Google（Gemini）だけを計上し、ほかは無料枠として 0 ドルにしています。"
               if mode == api_usage.MODE_PAID else "")
        )

    def _set_home_period(self, days: int) -> None:
        """期間カードの今日/今週を切り替える。"""
        self._home_period_days = days
        self._home_today_btn.setChecked(days == 1)
        self._home_week_btn.setChecked(days == 7)
        self._refresh_home_period()

    @staticmethod
    def _saved_comparison(seconds: float) -> str:
        """節約時間を身近なものへ換算した一言（Mac 版 savedComparison と同じしきい値）。"""
        s = float(seconds)
        if s >= 86400:      # まる 1 日（24 時間）
            return f"まるっと {int(s // 86400)} 日ぶんの時間"
        if s >= 28800:      # ぐっすり睡眠（8 時間）
            return f"ぐっすり睡眠 {int(s // 28800)} 回ぶん"
        if s >= 7200:       # 映画（2 時間）
            return f"映画 {int(s // 7200)} 本ぶんの尺"
        if s >= 1800:       # 通勤（片道 30 分）
            return f"通勤 {int(s // 1800)} 回ぶんの移動時間"
        if s >= 180:        # カップ麺（3 分）
            return f"カップ麺 {int(s // 180)} 個ぶんの待ち時間"
        return "これから時間が積み上がっていきます"

    @staticmethod
    def _grouped(n: int) -> str:
        """3 桁区切り（カンマ）の数値文字列。"""
        return f"{int(n):,}"

    def _refresh_home(self) -> None:
        """ホームの 3 カードと最近の履歴をストアの現在値で更新する。"""
        if self._stats is not None:
            d = self._stats.snapshot()
            today_chars = self._stats.daily_series(1)[-1]["characters"]
            self._home_greeting.setText(
                f"今日はここまで {self._grouped(today_chars)} 文字を入力しました"
                if today_chars > 0
                else "今日はまだ入力していません"
            )
            self._home_total_chars.setText(self._grouped(d["total_characters"]))
            self._home_total_sub.setText(
                f"{self._grouped(d['total_sessions'])} 回 · "
                f"録音 {self._format_minutes(d['total_recording_seconds'])}"
            )
            self._home_level_label.setText(f"レベル {d['level']}")
            self._home_next_label.setText(
                f"あと {self._grouped(d['xp_to_next_level'])} 文字で Lv.{d['level'] + 1}"
            )
            self._home_level_progress.setValue(int(round(d["level_progress"] * 100)))
            self._home_saved_value.setText(self._format_saved(d["saved_seconds"]))
            self._home_saved_comparison.setText(self._saved_comparison(d["saved_seconds"]))
            self._refresh_home_period()
        self._refresh_home_history()
        self._refresh_api_cost()

    def _refresh_home_period(self) -> None:
        """「この期間」カードを現在の今日/今週選択で更新する。"""
        if self._stats is None:
            return
        series = self._stats.daily_series(self._home_period_days)
        chars = sum(b["characters"] for b in series)
        rec = sum(b["recording_seconds"] for b in series)
        sessions = sum(b["sessions"] for b in series)
        self._home_period_chars.setText(self._grouped(chars))
        self._home_period_sub.setText(
            f"録音 {self._format_minutes(rec)} · {self._grouped(sessions)} 回"
        )

    def _refresh_home_history(self) -> None:
        """最近の履歴（直近 8 件）を更新する。行クリックでコピー。"""
        self._home_history_list.clear()
        items = self._history_entries()
        if not items:
            placeholder = QListWidgetItem(
                "音声入力すると、ここに最近の履歴が残ります（クリックでコピーできます）。"
            )
            placeholder.setFlags(Qt.ItemFlag.NoItemFlags)
            self._home_history_list.addItem(placeholder)
            return
        for entry in items[:8]:
            self._home_history_list.addItem(self._make_history_item(entry))

    def _history_entries(self) -> list:
        """ローカル履歴に、同期がオンなら Mac 側の履歴もマージして返す（新しい順）。"""
        local = self._history.items() if self._history is not None else []
        if self._history_sync is not None and self._history_sync.enabled:
            return self._history_sync.merged_items(local)
        return local

    def _make_history_item(self, entry: dict) -> QListWidgetItem:
        """履歴 1 件ぶんの QListWidgetItem を作る（windows 以外の device は "[Mac] " 等のラベルを付ける）。"""
        text = entry["text"]
        preview = text if len(text) <= 80 else text[:80] + "…"
        device = entry.get("device", "windows")
        if device != "windows":
            preview = ("[Mac] " if device == "mac" else f"[{device}] ") + preview
        date = _format_history_date(entry["date"])
        # 貼り付け先アプリ名があれば日時の横に添える（Mac の履歴と同じく「どこに入れたか」を見せる）
        app_name = entry.get("app_name")
        meta = f"{date}  ·  {app_name}" if app_name else date
        item = QListWidgetItem(f"{preview}\n{meta}")
        item.setData(Qt.ItemDataRole.UserRole, text)
        item.setToolTip(text)
        return item

    # ------------------------------------------------------------------
    # 履歴ページ（Mac 版 HistoryTab と同構成。クリックでクリップボードにコピー）
    # ------------------------------------------------------------------

    def _create_stats_page(self) -> QWidget:
        """実績ページを作成する（レベル・推定節約時間・連続日数。Mac 版 StatsTab と同項目）。"""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(10)
        layout.setContentsMargins(24, 8, 24, 24)

        # --- レベルカード（レベル + 経験値 + 次レベルまでの進捗バー） ---
        level_card, level_layout = _make_card()
        head = QWidget()
        head_h = QHBoxLayout(head)
        head_h.setContentsMargins(_CARD_PAD_X, 9, _CARD_PAD_X, 4)
        self._stats_level_label = QLabel("レベル 1")
        # レベルだけ少し大きく見せる（色はテーマの QSS を継承）
        self._stats_level_label.setStyleSheet("font-size: 20px; font-weight: 600;")
        self._stats_xp_label = QLabel("経験値 0")
        self._stats_xp_label.setObjectName("caption")
        head_h.addWidget(self._stats_level_label)
        head_h.addStretch()
        head_h.addWidget(self._stats_xp_label)
        level_layout.addWidget(head)

        bar_wrap = QWidget()
        bar_v = QVBoxLayout(bar_wrap)
        bar_v.setContentsMargins(_CARD_PAD_X, 0, _CARD_PAD_X, 10)
        bar_v.setSpacing(5)
        self._stats_progress = QProgressBar()
        self._stats_progress.setRange(0, 100)
        self._stats_progress.setTextVisible(False)
        self._stats_progress.setFixedHeight(8)
        self._stats_next_label = _make_caption("")
        bar_v.addWidget(self._stats_progress)
        bar_v.addWidget(self._stats_next_label)
        level_layout.addWidget(bar_wrap)
        layout.addWidget(level_card)

        # --- サマリーカード（今日 / 今週 / 累計。大きな数字をカウントアップして達成感を出す） ---
        summary_card, summary_layout = _make_card()
        tiles = QWidget()
        tiles_h = QHBoxLayout(tiles)
        tiles_h.setContentsMargins(_CARD_PAD_X, 12, _CARD_PAD_X, 12)
        tiles_h.setSpacing(0)
        today_tile, self._tile_today_value, self._tile_today_sub = self._make_summary_tile("今日")
        week_tile, self._tile_week_value, self._tile_week_sub = self._make_summary_tile("今週")
        total_tile, self._tile_total_value, self._tile_total_sub = self._make_summary_tile("累計")
        tiles_h.addWidget(today_tile, 1)
        tiles_h.addWidget(self._make_vline())
        tiles_h.addWidget(week_tile, 1)
        tiles_h.addWidget(self._make_vline())
        tiles_h.addWidget(total_tile, 1)
        summary_layout.addWidget(tiles)
        layout.addWidget(summary_card)

        # --- 入力量チャートカード（期間切替・棒グラフ・伸びるアニメーション） ---
        chart_card, chart_layout = _make_card()
        chart_inner = QWidget()
        chart_v = QVBoxLayout(chart_inner)
        chart_v.setContentsMargins(_CARD_PAD_X, 10, _CARD_PAD_X, 12)
        chart_v.setSpacing(10)

        chart_head = QHBoxLayout()
        chart_head.setContentsMargins(0, 0, 0, 0)
        chart_title = QLabel("入力量（文字数）")
        chart_title.setStyleSheet("font-weight: 600;")
        chart_head.addWidget(chart_title)
        chart_head.addStretch()
        # 期間切替（週 / 月 / 年）。排他チェックのセグメント風ボタン
        self._period_group = QButtonGroup(self)
        self._period_group.setExclusive(True)
        for idx, (key, label) in enumerate([("week", "週"), ("month", "月"), ("year", "年")]):
            btn = QPushButton(label)
            btn.setObjectName("segButton")
            btn.setProperty("class", "seg")
            btn.setCheckable(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setFixedHeight(26)
            btn.setMinimumWidth(40)
            if key == "week":
                btn.setChecked(True)
            btn.clicked.connect(lambda _checked, k=key: self._set_stats_period(k))
            self._period_group.addButton(btn, idx)
            chart_head.addWidget(btn)
        chart_v.addLayout(chart_head)

        self._stats_period = "week"
        self._usage_chart = UsageBarChart()
        chart_v.addWidget(self._usage_chart)
        chart_layout.addWidget(chart_inner)
        layout.addWidget(chart_card)

        # --- 累計カード ---
        stats_card, stats_layout = _make_card()
        self._stats_saved_value = QLabel("0 秒")
        self._stats_chars_value = QLabel("0 文字")
        self._stats_count_value = QLabel("0 回")
        self._stats_streak_value = QLabel("0 日")
        _add_row(stats_layout, "推定節約時間", self._stats_saved_value)
        _add_row(stats_layout, "累計文字数", self._stats_chars_value)
        _add_row(stats_layout, "音声入力した回数", self._stats_count_value)
        _add_row(stats_layout, "連続利用日数", self._stats_streak_value)
        layout.addWidget(stats_card)

        layout.addWidget(_make_caption(
            "「推定節約時間」は、同じ文章をキーボードで打つ場合と比べて短縮できた時間の目安です"
            "（タイピングより遅くなる短い入力は 0 として数えます）。実績はリセットできません。"
        ))
        layout.addStretch()

        self._refresh_stats()
        return self._wrap_scroll(page)

    def _make_summary_tile(self, title: str) -> tuple[QWidget, QLabel, QLabel]:
        """サマリー 1 枚（小タイトル + 大きな数字「N 文字」+ 補足）を作って返す。"""
        tile = QWidget()
        v = QVBoxLayout(tile)
        v.setContentsMargins(6, 0, 6, 0)
        v.setSpacing(2)
        title_label = QLabel(title)
        title_label.setObjectName("caption")
        value_label = QLabel("0 文字")
        # 大きな数字で達成感を出す（色はテーマ QSS を継承）
        value_label.setStyleSheet("font-size: 22px; font-weight: 700;")
        sub_label = QLabel("")
        sub_label.setObjectName("caption")
        v.addWidget(title_label)
        v.addWidget(value_label)
        v.addWidget(sub_label)
        return tile, value_label, sub_label

    @staticmethod
    def _make_vline() -> QFrame:
        """サマリータイル間の縦区切り線。"""
        line = QFrame()
        line.setObjectName("hairline")
        line.setFrameShape(QFrame.Shape.VLine)
        line.setFixedWidth(1)
        return line

    def _refresh_stats(self) -> None:
        """実績表示をストアの現在値で更新する。"""
        if self._stats is None:
            return
        d = self._stats.snapshot()
        self._stats_level_label.setText(f"レベル {d['level']}")
        self._stats_xp_label.setText(f"経験値 {d['xp']}")
        self._stats_progress.setValue(int(round(d["level_progress"] * 100)))
        self._stats_next_label.setText(
            f"あと {d['xp_to_next_level']} 文字でレベル {d['level'] + 1}"
        )
        self._stats_saved_value.setText(self._format_saved(d["saved_seconds"]))
        self._stats_chars_value.setText(f"{d['total_characters']} 文字")
        self._stats_count_value.setText(f"{d['total_sessions']} 回")
        self._stats_streak_value.setText(
            f"{d['current_streak']} 日（最長 {d['longest_streak']} 日）"
        )

        # サマリー（今日 / 今週 / 累計）。数字は 0→値 にカウントアップ
        today = self._stats.daily_series(1)[-1]
        week = self._stats.daily_series(7)
        week_chars = sum(b["characters"] for b in week)
        week_rec = sum(b["recording_seconds"] for b in week)
        self._animate_number(self._tile_today_value, today["characters"], " 文字")
        self._tile_today_sub.setText(f"{today['sessions']} 回")
        self._animate_number(self._tile_week_value, week_chars, " 文字")
        self._tile_week_sub.setText(f"録音 {self._format_minutes(week_rec)}")
        self._animate_number(self._tile_total_value, d["total_characters"], " 文字")
        self._tile_total_sub.setText(f"Lv.{d['level']}")

        self._update_chart()

    def _animate_number(self, label: QLabel, target: int, suffix: str) -> None:
        """ラベルの数字を 0→target にカウントアップする（達成感の演出）。"""
        anim = QVariantAnimation(self)
        anim.setStartValue(0)
        anim.setEndValue(int(target))
        anim.setDuration(700)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim.valueChanged.connect(lambda v: label.setText(f"{int(v)}{suffix}"))
        # 末尾を確実に正しい値で締める（端数で止まらないように）
        anim.finished.connect(lambda: label.setText(f"{int(target)}{suffix}"))
        anim.start()
        # GC されると停止するのでラベルに持たせて延命する
        label._countup_anim = anim

    def _chart_bars(self) -> list:
        """現在の期間設定に応じた棒データ [(ラベル, 文字数)] を返す。"""
        if self._stats is None:
            return []
        if self._stats_period == "week":
            wd = ["月", "火", "水", "木", "金", "土", "日"]
            out = []
            for b in self._stats.daily_series(7):
                dt = datetime.strptime(b["day"], "%Y-%m-%d")
                out.append((wd[dt.weekday()], b["characters"]))
            return out
        if self._stats_period == "month":
            return [
                (str(int(b["day"][8:10])), b["characters"])
                for b in self._stats.daily_series(30)
            ]
        # year（直近 12 ヶ月）
        return [
            (f"{int(b['month'][5:7])}月", b["characters"])
            for b in self._stats.monthly_series(12)
        ]

    def _update_chart(self) -> None:
        """現在の期間でチャートのデータを差し替える（0 から伸びるアニメーション付き）。"""
        if getattr(self, "_usage_chart", None) is not None:
            self._usage_chart.set_data(self._chart_bars())

    def _set_stats_period(self, period: str) -> None:
        """チャートの集計期間（week / month / year）を切り替える。"""
        if period == self._stats_period:
            return
        self._stats_period = period
        self._update_chart()

    @staticmethod
    def _format_saved(seconds: float) -> str:
        """累計節約秒数を「X 時間 Y 分」等に整形する（Mac 版 formattedSaved と同じ規則）。"""
        total = int(round(seconds))
        if total >= 3600:
            return f"{total // 3600} 時間 {(total % 3600) // 60} 分"
        if total >= 60:
            return f"{total // 60} 分 {total % 60} 秒"
        return f"{total} 秒"

    @staticmethod
    def _format_minutes(seconds: float) -> str:
        """録音秒数を短く整形する（サマリー補足用。Mac 版 formattedMinutes と同じ規則）。"""
        total = int(round(seconds))
        if total >= 3600:
            return f"{total // 3600}時間{(total % 3600) // 60}分"
        if total >= 60:
            return f"{total // 60}分"
        return f"{total}秒"

    def _create_history_page(self) -> QWidget:
        """履歴ページを作成する（直近の音声入力をクリックで再コピー）。"""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(10)
        layout.setContentsMargins(24, 8, 24, 24)

        # 「Mac と履歴を共有」の設定は 一般 › 履歴 へ移した（Mac の設定画面の作り直しに合わせる）

        # QListWidget 自体がスクロールするため _wrap_scroll は不要
        self._history_list = QListWidget()
        self._history_list.setWordWrap(True)
        # 長文は折り返して表示するため横スクロールは出さない
        self._history_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._history_list.itemClicked.connect(self._copy_history_item)
        layout.addWidget(self._history_list, 1)

        footer = QHBoxLayout()
        footer.setSpacing(8)
        # コピー成功の一時フィードバック（Mac 版の「コピーしました」と同等）
        self._history_feedback = QLabel("")
        self._history_feedback.setStyleSheet(MacTheme.status_ok_style(self._is_dark_mode))
        clear_btn = QPushButton("履歴を消去")
        clear_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        clear_btn.clicked.connect(self._clear_history)
        footer.addWidget(self._history_feedback)
        footer.addStretch()
        footer.addWidget(clear_btn)
        layout.addLayout(footer)

        layout.addWidget(_make_caption(
            f"音声入力の直近 {HISTORY_MAX_ITEMS} 件（同期がオンのときは Mac の履歴も含めて"
            "最大 200 件）です。行をクリックするとクリップボードにコピーします。"
        ))

        self._refresh_history()
        return page

    def _create_history_sync_card(self) -> QWidget:
        """「Mac と履歴を共有」カードを作る（トグル・同期先 URL・共有トークン・同期状況）。

        URL・トークン欄は共有がオンのときだけ出す（Mac の一般タブと同じ）。
        """
        box = QWidget()
        box_layout = QVBoxLayout(box)
        box_layout.setContentsMargins(0, 0, 0, 0)
        box_layout.setSpacing(8)
        card, cl = _make_card()
        self._history_sync_enabled_toggle = self._make_toggle()
        _add_row(
            cl, "Mac と履歴を共有", self._history_sync_enabled_toggle,
            caption=(
                "Cloudflare 上の自分専用 Worker に履歴を送り、"
                "Mac で入力した履歴もここに表示します。"
            ),
        )
        box_layout.addWidget(card)
        card, cl = _make_card()
        self._history_sync_detail_card = card
        box_layout.addWidget(card)
        self._history_sync_enabled_toggle.toggled.connect(card.setVisible)
        card.setVisible(False)

        self._history_sync_url_input = QLineEdit()
        self._history_sync_url_input.setPlaceholderText(
            "https://voicekey-history-sync.<subdomain>.workers.dev"
        )
        _add_row(cl, "同期サーバー URL", self._history_sync_url_input)

        # 共有トークン欄（API キー欄と同じパターン: 入力中は伏字、保存時のみ資格情報ストアへ書き込む）
        token_block = QWidget()
        token_layout = QVBoxLayout(token_block)
        token_layout.setContentsMargins(0, 0, 0, 0)
        token_layout.setSpacing(6)

        token_head = QHBoxLayout()
        token_head.setSpacing(8)
        token_name = QLabel("共有トークン")
        token_name.setStyleSheet("font-weight: 600;")
        self._history_sync_token_status = QLabel("未設定")
        self._history_sync_token_status.setStyleSheet(MacTheme.status_muted_style())
        token_head.addWidget(token_name)
        token_head.addWidget(self._history_sync_token_status)
        token_head.addStretch()

        self._history_sync_token_input = QLineEdit()
        self._history_sync_token_input.setEchoMode(QLineEdit.EchoMode.Password)
        self._history_sync_token_input.setPlaceholderText("トークンを入力")

        token_save_btn = QPushButton("保存")
        token_save_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        token_save_btn.clicked.connect(self._save_history_sync_token)

        token_delete_btn = QPushButton("削除")
        token_delete_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        token_delete_btn.clicked.connect(self._delete_history_sync_token)

        token_body = QHBoxLayout()
        token_body.setSpacing(8)
        token_body.addWidget(self._history_sync_token_input, 1)
        token_body.addWidget(token_save_btn)
        token_body.addWidget(token_delete_btn)

        token_layout.addLayout(token_head)
        token_layout.addLayout(token_body)
        _add_block(cl, token_block)

        self._history_sync_status_label = QLabel("")
        self._history_sync_status_label.setWordWrap(True)
        self._history_sync_status_label.setStyleSheet(MacTheme.status_muted_style())
        _add_block(cl, self._history_sync_status_label)

        return box

    def _refresh_history_sync_token_status(self) -> None:
        """共有トークンの保存状況ラベルを更新する（_refresh_api_key_status と同じ規則）。"""
        if not secrets.is_keyring_available():
            # 同期トークンは API キーと違って環境変数のフォールバックが無いので、代替手段は案内しない
            self._history_sync_token_status.setText("資格情報ストア利用不可")
            self._history_sync_token_status.setStyleSheet(
                MacTheme.status_warn_style(self._is_dark_mode)
            )
            return
        if secrets.get_api_key(secrets.SERVICE_SYNC_TOKEN):
            self._history_sync_token_status.setText("保存済み")
            self._history_sync_token_status.setStyleSheet(
                MacTheme.status_ok_style(self._is_dark_mode)
            )
        else:
            self._history_sync_token_status.setText("未設定")
            self._history_sync_token_status.setStyleSheet(MacTheme.status_muted_style())

    def _save_history_sync_token(self) -> None:
        """入力欄の共有トークンを資格情報ストアに保存する（保存後は再表示しないため入力欄をクリア）。"""
        new_token = self._history_sync_token_input.text().strip()
        if not new_token:
            QMessageBox.warning(self, "共有トークン", "トークンを入力してください。")
            return

        if not secrets.set_api_key(secrets.SERVICE_SYNC_TOKEN, new_token):
            QMessageBox.critical(
                self,
                "共有トークン",
                "資格情報ストアへの保存に失敗しました。"
            )
            return

        self._history_sync_token_input.clear()
        self._refresh_history_sync_token_status()
        if self._history_sync is not None:
            self._history_sync.apply_config()

    def _delete_history_sync_token(self) -> None:
        """資格情報ストアに保存された共有トークンを削除する。"""
        secrets.delete_api_key(secrets.SERVICE_SYNC_TOKEN)
        self._history_sync_token_input.clear()
        self._refresh_history_sync_token_status()

    def _refresh_history_sync_status(self) -> None:
        """Mac との同期状況ラベルを history_sync.status() の内容で更新する。"""
        if self._history_sync is None:
            self._history_sync_status_label.setText("")
            return

        status = self._history_sync.status()
        if not status.get("enabled"):
            self._history_sync_status_label.setText("同期はオフです")
            self._history_sync_status_label.setStyleSheet(MacTheme.status_muted_style())
            return
        if not status.get("configured"):
            self._history_sync_status_label.setText(
                "URL とトークンを設定すると同期が始まります"
            )
            self._history_sync_status_label.setStyleSheet(MacTheme.status_muted_style())
            return
        if status.get("token_invalid"):
            self._history_sync_status_label.setText(
                "トークンが無効です（保存し直してください）"
            )
            self._history_sync_status_label.setStyleSheet(
                MacTheme.status_warn_style(self._is_dark_mode)
            )
            return

        last_sync = status.get("last_sync")
        last_sync_text = None
        if last_sync and parse_iso_date is not None:
            try:
                last_sync_text = parse_iso_date(last_sync).astimezone().strftime("%H:%M")
            except Exception:
                last_sync_text = None
        if last_sync_text is None:
            self._history_sync_status_label.setText("まだ同期していません")
        else:
            pending = status.get("pending", 0)
            self._history_sync_status_label.setText(
                f"送信待ち {pending} 件・最終同期 {last_sync_text}"
            )
        self._history_sync_status_label.setStyleSheet(MacTheme.status_muted_style())

    def _on_nav_changed(self, row: int) -> None:
        """サイドバー選択でページを切り替え、見出しを更新する（見出し行は選べない）。"""
        if row < 0:
            return
        item = self._nav.item(row)
        page_index = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        if page_index is None:
            return
        self._pages.setCurrentIndex(int(page_index))
        self._set_page_header(self._nav_titles[row])
        if self._nav_titles[row] == "API キー":
            self._refresh_all_api_key_status()
        # ウィンドウを開いたまま音声入力しても、各ページを開いた時点で最新になるように
        if row == self._home_page_index:
            self._refresh_home()
        elif row == self._history_page_index:
            self._refresh_history()
            if self._history_sync is not None:
                self._history_sync.request_fetch()
            self._refresh_history_sync_status()
        elif row == self._stats_page_index:
            self._refresh_stats()

    def select_home(self) -> None:
        """ホーム（ダッシュボード）ページを選択する（サイドノッチの「ホームを開く」から呼ぶ）。"""
        self._nav.setCurrentRow(self._home_page_index)

    def select_page(self, title: str) -> None:
        """ページ名でページを選択する（「プロンプトを編集」「API キーを開く」などの導線）。"""
        row = self._nav_row_of.get(title)
        if row is not None:
            self._nav.setCurrentRow(row)

    def select_api_keys(self) -> None:
        """「API キー」ページを選択する（キー未設定・無効で文字起こしに失敗したときにアプリが呼ぶ）。"""
        self.select_page("API キー")

    def _refresh_history(self) -> None:
        """履歴一覧をストアの現在の内容で作り直す（同期がオンなら Mac の履歴も含む）。"""
        self._history_list.clear()
        items = self._history_entries()

        if not items:
            placeholder = QListWidgetItem(
                f"まだ履歴がありません。音声入力すると、ここに直近 {HISTORY_MAX_ITEMS} 件が残ります。"
            )
            # 選択・クリック不可の案内行にする
            placeholder.setFlags(Qt.ItemFlag.NoItemFlags)
            self._history_list.addItem(placeholder)
            return

        # 一覧は 2 行プレビュー + 日時。全文はクリック時のコピーとツールチップで確認できる
        for entry in items:
            self._history_list.addItem(self._make_history_item(entry))

    def _copy_history_item(self, item: QListWidgetItem) -> None:
        """クリックされた履歴の全文をクリップボードにコピーする。"""
        text = item.data(Qt.ItemDataRole.UserRole)
        if not text:
            return
        QApplication.clipboard().setText(text)
        self._history_feedback.setText(f"コピーしました（{len(text)} 文字）")
        QTimer.singleShot(1500, lambda: self._history_feedback.setText(""))

    def _clear_history(self) -> None:
        """履歴をすべて消去して一覧を更新する。"""
        if self._history is not None:
            self._history.clear()
        self._refresh_history()

    def refresh_history_views(self) -> None:
        """同期状態の変化（history_sync.on_changed 等）を受けて履歴表示一式を更新する。

        本体側から QTimer.singleShot(0, ...) 経由で呼ばれる想定（別スレッドの通知を
        Qt イベントループ上で安全に UI へ反映するため）。
        """
        self._refresh_history()
        self._refresh_home_history()
        self._refresh_history_sync_status()

    def showEvent(self, event) -> None:
        """ウィンドウを開くたびに永続設定から UI を再ロードし、履歴・実績を最新化する。

        ウィンドウは破棄されず使い回されるため、前回の未保存編集やテーマプレビューが
        残ったまま再表示・保存されるのを防ぐ。キャンセル（保存せず閉じる）した変更は
        次に開いた時点でここで破棄される。
        """
        super().showEvent(event)
        self._load_current_settings()
        self._refresh_home()
        self._refresh_history()
        self._refresh_stats()

    def _create_api_keys_page(self) -> QWidget:
        """API キーページ（全エンジン・翻訳のキー。各行に「どこから読めるか」の状態を出す・Mac と同じ）。

        値そのもの（末尾も含む）は画面に一切出さない。保存先はアプリ専用の項目
        （資格情報マネージャーの voicekey.*）で、削除できるのはアプリに保存した値だけ。
        読む順は アプリに保存した値 → 環境変数 → 資格情報マネージャーの共有項目（サービス名＝変数名・shared）。
        """
        page, layout = self._new_page()
        layout.addWidget(_make_caption(
            "使う文字起こしエンジンのキーだけ入れれば動きます。キーはこの PC の Windows 資格情報マネージャー"
            "（アプリ専用の項目）に保存され、画面やログには表示しません。"
        ))

        # 項目 → 入力欄・状態ラベル・削除ボタン・エラー表示
        self._api_key_inputs = {}
        self._api_key_status = {}
        self._api_key_delete = {}
        self._api_key_error = {}
        self._api_key_sources = {}

        sections = list(_API_KEY_SECTIONS)
        # 選択肢から外した Deepgram は、いまの設定で使っている人にだけ出す
        uses_deepgram = any(
            (self._config_manager.get(f"hotkey{i}", {}) or {}).get("backend") == backends.DEEPGRAM
            for i in (1, 2)
        )
        if uses_deepgram:
            sections.append(("旧エンジン", [_LEGACY_API_KEY_ROW]))
        for section_title, rows in sections:
            layout.addWidget(self._section_title(section_title))
            card, cl = _make_card()
            for item_id, title, note, secret, placeholder in rows:
                _add_block(cl, self._api_key_block(item_id, title, note, secret, placeholder))
            layout.addWidget(card)
        layout.addStretch()
        return self._wrap_scroll(page)

    def _api_key_block(self, item_id: str, title: str, note: str, secret: bool, placeholder: str) -> QWidget:
        """API キー 1 項目分の行（名前・状態・補足・入力欄・保存・削除）。"""
        block = QWidget()
        bl = QVBoxLayout(block)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(6)
        head = QHBoxLayout()
        head.setSpacing(8)
        name_label = QLabel(title)
        name_label.setStyleSheet("font-weight: 600;")
        status_label = QLabel("未設定")
        status_label.setStyleSheet(MacTheme.status_muted_style())
        head.addWidget(name_label)
        head.addStretch()
        head.addWidget(status_label)
        bl.addLayout(head)
        if note:
            bl.addWidget(_make_caption(note))
        key_input = QLineEdit()
        if secret:
            # 入力中は伏字表示。保存時のみ資格情報マネージャーに書き込み、値は再表示しない
            key_input.setEchoMode(QLineEdit.EchoMode.Password)
        key_input.setPlaceholderText(placeholder)
        save_btn = QPushButton("保存")
        save_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        save_btn.clicked.connect(lambda _=False, i=item_id: self._save_api_key(i))
        delete_btn = QPushButton("削除")
        delete_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        delete_btn.clicked.connect(lambda _=False, i=item_id: self._delete_api_key(i))
        delete_btn.setEnabled(False)  # 消せるのはアプリに保存した値だけ
        body = QHBoxLayout()
        body.setSpacing(8)
        body.addWidget(key_input, 1)
        body.addWidget(save_btn)
        body.addWidget(delete_btn)
        bl.addLayout(body)
        error = _make_caption("保存できませんでした（資格情報マネージャーに書き込めませんでした）。もう一度お試しください")
        error.setStyleSheet(MacTheme.status_warn_style(self._is_dark_mode))
        error.setVisible(False)
        bl.addWidget(error)
        self._api_key_inputs[item_id] = key_input
        self._api_key_status[item_id] = status_label
        self._api_key_delete[item_id] = delete_btn
        self._api_key_error[item_id] = error
        return block

    def _refresh_all_api_key_status(self) -> None:
        """全項目の状態を読み直す（ページを開いたとき・ウィンドウを開いたとき）。"""
        for item_id in list(self._api_key_status):
            self._refresh_api_key_status(item_id)

    def _refresh_api_key_status(self, item_id: str) -> None:
        """
        項目の状態ラベル（アプリに保存済み／環境変数から読込／資格情報マネージャー（共有）から読込／未設定）を更新する。

        値は返さない・表示しない（どこから読めるかだけ）。
        """
        status_label = self._api_key_status.get(item_id)
        if status_label is None:
            return
        if secrets.is_keyring_available():
            source = secrets.api_key_source(item_id)
        else:
            # 資格情報ストアが使えない環境では環境変数だけを見る
            item = secrets.API_KEY_ITEMS.get(item_id)
            source = (
                secrets.SOURCE_ENVIRONMENT
                if item is not None and os.environ.get(item.variable_name) else None
            )
        self._api_key_sources[item_id] = source
        if source is not None:
            status_label.setText("✓ " + secrets.SOURCE_LABELS[source])
            status_label.setStyleSheet(MacTheme.status_ok_style(self._is_dark_mode))
        elif not secrets.is_keyring_available():
            status_label.setText("資格情報ストア利用不可（環境変数で設定してください）")
            status_label.setStyleSheet(MacTheme.status_warn_style(self._is_dark_mode))
        else:
            status_label.setText("未設定")
            status_label.setStyleSheet(MacTheme.status_muted_style())
        self._api_key_delete[item_id].setEnabled(source == secrets.SOURCE_APP)

    def _save_api_key(self, item_id: str) -> None:
        """
        入力欄の値をアプリ専用の保存先へ保存する（`SONIOX_API_KEY=...` のような貼り付けも正規化する）。

        保存できたら入力欄を空にして状態を更新する。失敗したら入力を残してエラーを出す
        （黙って空にすると「保存できた」と誤解されるため・Mac と同じ）。
        """
        key_input = self._api_key_inputs[item_id]
        value = secrets.sanitize_key(key_input.text())
        if not value:
            return
        if not secrets.save_item(item_id, value):
            self._api_key_error[item_id].setVisible(True)
            return
        self._api_key_error[item_id].setVisible(False)
        key_input.clear()
        self._refresh_api_key_status(item_id)

    def _delete_api_key(self, item_id: str) -> None:
        """アプリに保存した値だけを消す（環境変数・共有の項目には触らない）。"""
        secrets.delete_item(item_id)
        self._api_key_inputs[item_id].clear()
        self._api_key_error[item_id].setVisible(False)
        self._refresh_api_key_status(item_id)

    # ------------------------------------------------------------------
    # ユーザー辞書（確定置換）
    # ------------------------------------------------------------------

    def _create_dictionary_page(self) -> QWidget:
        """辞書と数字ページ（置き換えを追加 / 登録済みの置き換え / 数字の書き方・Mac と同じ構成）。"""
        page, layout = self._new_page()

        # --- 置き換えを追加（常に出ている入力欄） ---
        layout.addWidget(self._section_title("置き換えを追加"))
        card, cl = _make_card()
        add_row = QWidget()
        ar = QHBoxLayout(add_row)
        ar.setContentsMargins(0, 0, 0, 0)
        ar.setSpacing(8)
        self._new_rule_from = QLineEdit()
        self._new_rule_from.setPlaceholderText("聞き取られた表記（例: ボイスキー）")
        self._new_rule_to = QLineEdit()
        self._new_rule_to.setPlaceholderText("直したい表記（例: voicekey）")
        add_btn = QPushButton("追加")
        add_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        add_btn.clicked.connect(self._add_rule_from_inputs)
        self._new_rule_to.returnPressed.connect(self._add_rule_from_inputs)
        ar.addWidget(self._new_rule_from, 1)
        ar.addWidget(QLabel("→"))
        ar.addWidget(self._new_rule_to, 1)
        ar.addWidget(add_btn)
        _add_block(cl, add_row)
        layout.addWidget(card)
        layout.addWidget(_make_caption(
            "文字起こし・整形が終わった文に、貼り付ける直前で当てます（部分一致・上から順に）。"
            "API を通さないので待ち時間は増えません。"
        ))

        # --- 登録済みの置き換え（N 件） ---
        self._rules_title = self._section_title("登録済みの置き換え（0 件）")
        layout.addWidget(self._rules_title)
        # ルール行を縦に積むコンテナ（追加・削除で動的に増減する）
        self._rules_container = QWidget()
        self._rules_layout = QVBoxLayout(self._rules_container)
        self._rules_layout.setContentsMargins(0, 0, 0, 0)
        self._rules_layout.setSpacing(6)
        layout.addWidget(self._rules_container)

        # 既存ルールを読み込んで行を作る
        self._replacement_rows = []
        for rule in self._config_manager.get("replacements", []) or []:
            if isinstance(rule, dict):
                self._add_replacement_row(rule)

        # 空状態の案内（ルールが無いとき表示）
        self._dict_empty_label = _make_caption(
            "置き換えはまだありません。上の欄で、よく聞き間違えられる語の直し方を登録できます。"
        )
        layout.addWidget(self._dict_empty_label)
        self._update_dict_empty_label()

        # --- 数字の書き方（旧 一般 の数字入力） ---
        layout.addWidget(self._section_title("数字の書き方"))
        card, cl = _make_card()
        self._numeral_enabled_check = self._make_toggle()
        _add_row(
            cl, "数字を半角で入力", self._numeral_enabled_check,
            caption="「十二人→12人」「千二百三十四→1234」のように、漢数字や全角の数字を半角にします。",
        )
        self._numeral_counter_check = self._make_toggle()
        _add_row(
            cl, "助数詞つきの漢数字も変換（三時→3時）", self._numeral_counter_check,
            caption="「三時」「十時」「百人」のような、単位の付いた漢数字も半角数字にします。",
        )
        # マスターが OFF のときは助数詞トグルを無効化する（機能全体が止まるため）
        self._numeral_enabled_check.toggled.connect(self._numeral_counter_check.setEnabled)

        # 数字にしない語（保護リスト）。数字漢字で始まる語をラン先頭にアンカー照合して守る
        protect_block = QWidget()
        pbl = QVBoxLayout(protect_block)
        pbl.setContentsMargins(0, 0, 0, 0)
        pbl.setSpacing(6)
        pbl.addWidget(QLabel("数字にしない語"))
        pbl.addWidget(_make_caption("「一人」「十分」など、数字に読める普通の言葉を守ります。"))
        self._numeral_protect_list = QListWidget()
        self._numeral_protect_list.setMaximumHeight(140)
        pbl.addWidget(self._numeral_protect_list)
        protect_add_row = QHBoxLayout()
        self._numeral_protect_input = QLineEdit()
        self._numeral_protect_input.setPlaceholderText("語を追加")
        self._numeral_protect_input.returnPressed.connect(self._add_protect_word)
        protect_add_btn = QPushButton("追加")
        protect_add_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        protect_add_btn.clicked.connect(self._add_protect_word)
        protect_del_btn = QPushButton("削除")
        protect_del_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        protect_del_btn.clicked.connect(self._remove_selected_protect_word)
        protect_add_row.addWidget(self._numeral_protect_input, 1)
        protect_add_row.addWidget(protect_add_btn)
        protect_add_row.addWidget(protect_del_btn)
        pbl.addLayout(protect_add_row)
        _add_block(cl, protect_block)
        layout.addWidget(card)

        layout.addStretch()
        return self._wrap_scroll(page)

    def _add_rule_from_inputs(self) -> None:
        """「置き換えを追加」の入力欄から 1 件追加する（変換元が空なら何もしない）。"""
        src = self._new_rule_from.text().strip()
        if not src:
            return
        self._add_replacement_row({"from": src, "to": self._new_rule_to.text(), "enabled": True})
        self._new_rule_from.clear()
        self._new_rule_to.clear()
        self._new_rule_from.setFocus()

    def _add_replacement_row(self, rule: Optional[dict] = None) -> None:
        """確定置換ルール 1 行ぶんのウィジェットを作って一覧に追加する。"""
        rule = rule or {}
        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(8)

        enabled = QCheckBox()
        enabled.setChecked(bool(rule.get("enabled", True)))
        enabled.setToolTip("この行を有効/無効にする")
        from_input = QLineEdit(str(rule.get("from", "")))
        from_input.setPlaceholderText("変換元")
        arrow = QLabel("→")
        to_input = QLineEdit(str(rule.get("to", "")))
        to_input.setPlaceholderText("変換先")
        delete_btn = QPushButton("削除")
        delete_btn.setCursor(Qt.CursorShape.PointingHandCursor)

        row_layout.addWidget(enabled)
        row_layout.addWidget(from_input, 1)
        row_layout.addWidget(arrow)
        row_layout.addWidget(to_input, 1)
        row_layout.addWidget(delete_btn)

        entry = {"enabled": enabled, "from": from_input, "to": to_input, "widget": row}
        self._replacement_rows.append(entry)
        self._rules_layout.addWidget(row)

        def _remove() -> None:
            # 行ウィジェットと管理リストの両方から取り除く
            if entry in self._replacement_rows:
                self._replacement_rows.remove(entry)
            row.setParent(None)
            row.deleteLater()
            self._update_dict_empty_label()

        delete_btn.clicked.connect(_remove)
        self._update_dict_empty_label()

    def _update_dict_empty_label(self) -> None:
        """ルールが無いときだけ空状態の案内を表示し、見出しの件数を更新する。"""
        count = len(getattr(self, "_replacement_rows", []))
        if hasattr(self, "_dict_empty_label"):
            self._dict_empty_label.setVisible(count == 0)
        if hasattr(self, "_rules_title"):
            self._rules_title.setText(f"登録済みの置き換え（{count} 件）")

    def _collect_replacements(self) -> list:
        """UI のルール行を settings.yaml 用のリストにまとめる（変換元が空の行は捨てる）。"""
        rules = []
        for entry in getattr(self, "_replacement_rows", []):
            src = entry["from"].text().strip()
            if not src:
                continue
            rules.append({
                "from": src,
                "to": entry["to"].text(),
                "enabled": entry["enabled"].isChecked(),
            })
        return rules

    # ------------------------------------------------------------------
    # 数字入力の保護リスト（変換しない語）
    # ------------------------------------------------------------------

    def _add_protect_word(self) -> None:
        """入力欄の語を保護リストへ追加する（前後空白除去・重複はスキップ）。"""
        word = self._numeral_protect_input.text().strip()
        if not word:
            return
        existing = {
            self._numeral_protect_list.item(i).text()
            for i in range(self._numeral_protect_list.count())
        }
        if word not in existing:
            self._numeral_protect_list.addItem(word)
        self._numeral_protect_input.clear()

    def _remove_selected_protect_word(self) -> None:
        """保護リストで選択中の語を削除する。"""
        for item in self._numeral_protect_list.selectedItems():
            self._numeral_protect_list.takeItem(self._numeral_protect_list.row(item))

    def _collect_protect_words(self) -> list:
        """保護リストの語を settings.yaml 用のリストにまとめる（登録順を保持）。"""
        return [
            self._numeral_protect_list.item(i).text()
            for i in range(self._numeral_protect_list.count())
        ]

    # ------------------------------------------------------------------
    # アカウント（ブラウザ経由ログイン・製品版 段階4）
    # ------------------------------------------------------------------

    def _create_account_page(self) -> QWidget:
        """アカウントページを作成する（ログイン状態の表示・ログイン／ログアウト）。"""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(12)
        layout.setContentsMargins(24, 8, 24, 24)

        card, cl = _make_card()
        block = QWidget()
        bl = QVBoxLayout(block)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(8)

        self._account_status = QLabel("")
        self._account_status.setWordWrap(True)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        self._login_btn = QPushButton("ログイン")
        self._login_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._login_btn.clicked.connect(self._on_login_clicked)
        self._logout_btn = QPushButton("ログアウト")
        self._logout_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._logout_btn.clicked.connect(self._on_logout_clicked)
        btn_row.addWidget(self._login_btn)
        btn_row.addWidget(self._logout_btn)
        btn_row.addStretch()

        bl.addWidget(self._account_status)
        bl.addLayout(btn_row)
        _add_block(cl, block)

        layout.addWidget(card)

        # ライセンス（アクティベーションキー）カード。ログイン済みのときだけ表示する。
        lic_card, lcl = _make_card()
        lic_block = QWidget()
        lic_l = QVBoxLayout(lic_block)
        lic_l.setContentsMargins(0, 0, 0, 0)
        lic_l.setSpacing(8)

        lic_title = QLabel("ライセンス（アクティベーションキー）")
        lic_title.setStyleSheet("font-weight: 600;")
        lic_l.addWidget(lic_title)

        # 利用権の状態（有効／期限／未登録／確認中）
        self._entitlement_status = QLabel("")
        self._entitlement_status.setWordWrap(True)
        lic_l.addWidget(self._entitlement_status)

        # キー入力 + 登録ボタン
        key_row = QHBoxLayout()
        key_row.setSpacing(8)
        self._activation_key_input = QLineEdit()
        self._activation_key_input.setPlaceholderText("アクティベーションキーを入力")
        self._activation_key_input.returnPressed.connect(self._on_redeem_clicked)
        self._redeem_btn = QPushButton("登録")
        self._redeem_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._redeem_btn.clicked.connect(self._on_redeem_clicked)
        key_row.addWidget(self._activation_key_input, 1)
        key_row.addWidget(self._redeem_btn)
        lic_l.addLayout(key_row)

        # 登録結果メッセージ（成功＝緑 / 失敗＝赤）
        self._redeem_msg = QLabel("")
        self._redeem_msg.setWordWrap(True)
        self._redeem_msg.setVisible(False)
        lic_l.addWidget(self._redeem_msg)

        _add_block(lcl, lic_block)
        self._license_card = lic_card
        layout.addWidget(lic_card)

        layout.addWidget(_make_caption(
            "ログインすると無料体験でサーバー経由の文字起こし（即時入力／正確性）が"
            "使えます。無料体験を使い切ったら、アクティベーションキーを登録すると続けて使えます。"
            "キーはこのアカウントに紐付き、別の端末でログインしても同じライセンスで使えます。"
            "ログインはブラウザで行います。"
        ))
        layout.addStretch()

        # ログイン待ち/交換中はステータスをポーリングして表示を最新化する
        # （deep link はブラウザ→OS→アプリと非同期に戻ってくるため）
        self._account_poll = QTimer(self)
        self._account_poll.setInterval(800)
        self._account_poll.timeout.connect(self.refresh_account_status)

        self.refresh_account_status()
        return self._wrap_scroll(page)

    def _on_login_clicked(self) -> None:
        """ログイン開始: state を生成し、既定ブラウザでログインページを開く。"""
        from ..core import login_coordinator

        url = login_coordinator.shared().begin_login()
        QDesktopServices.openUrl(QUrl(url))
        self.refresh_account_status()
        self._account_poll.start()

    def _on_logout_clicked(self) -> None:
        """ログアウト（セッション破棄）。"""
        from ..core import login_coordinator

        login_coordinator.shared().logout()
        # 登録欄・結果表示をリセット
        if hasattr(self, "_activation_key_input"):
            self._activation_key_input.clear()
        if hasattr(self, "_redeem_msg"):
            self._redeem_msg.setVisible(False)
        self.refresh_account_status()

    # ------------------------------------------------------------------
    # ライセンス（アクティベーションキー）
    # ------------------------------------------------------------------

    def _ensure_entitlement_refresh(self) -> None:
        """利用権が未確認なら、バックグラウンドで /api/v1/me を叩いて取得する。

        ポーリングや再描画で何度も呼ばれるため、確認中・確認済みのときは何もしない
        （UNKNOWN/ERROR のときだけ取りに行く）。
        """
        from ..core import login_coordinator

        coord = login_coordinator.shared()
        LC = login_coordinator.LoginCoordinator
        if coord.entitlement not in (LC.ENT_UNKNOWN, LC.ENT_ERROR):
            return
        if getattr(self, "_entitlement_refreshing", False):
            return
        self._entitlement_refreshing = True

        def _work():
            try:
                coord.refresh_entitlement()
            finally:
                self._entitlement_done.emit()

        threading.Thread(target=_work, daemon=True).start()

    def _on_entitlement_done(self) -> None:
        """利用権確認の完了（メインスレッド）。表示を更新する。"""
        self._entitlement_refreshing = False
        self._render_entitlement()
        # メール表示を含むログイン行も更新したいので、ログイン中なら上段も描き直す
        if hasattr(self, "_account_status"):
            self.refresh_account_status()

    def _render_entitlement(self) -> None:
        """現在の利用権の状態をライセンスカードに反映する。"""
        if not hasattr(self, "_entitlement_status"):
            return
        from ..core import login_coordinator

        coord = login_coordinator.shared()
        LC = login_coordinator.LoginCoordinator
        ent = coord.entitlement

        if ent == LC.ENT_ACTIVE:
            until = coord.entitlement_active_until
            text = "ライセンス: 有効"
            if until:
                text += f"（期限: {self._format_until(until)}）"
            self._entitlement_status.setText(text)
            self._entitlement_status.setStyleSheet(MacTheme.status_ok_style(self._is_dark_mode))
            # 有効になったら入力欄は不要
            self._activation_key_input.setVisible(False)
            self._redeem_btn.setVisible(False)
        elif ent == LC.ENT_FREE:
            remaining = coord.entitlement_free_remaining
            quota = coord.entitlement_free_quota
            self._entitlement_status.setText(
                f"無料体験中（残り {remaining} / {quota} 回）。"
                "使い切ったらアクティベーションキーの登録が必要です。"
            )
            self._entitlement_status.setStyleSheet(MacTheme.status_ok_style(self._is_dark_mode))
            # 無料体験中でも先にキーを登録できるよう入力欄は出す
            self._activation_key_input.setVisible(True)
            self._redeem_btn.setVisible(True)
        elif ent == LC.ENT_CHECKING:
            self._entitlement_status.setText("ライセンスを確認中…")
            self._entitlement_status.setStyleSheet(MacTheme.status_muted_style())
            self._activation_key_input.setVisible(True)
            self._redeem_btn.setVisible(True)
        elif ent == LC.ENT_ERROR:
            self._entitlement_status.setText(
                coord.entitlement_error or "ライセンスの状態を確認できませんでした"
            )
            self._entitlement_status.setStyleSheet(MacTheme.status_warn_style(self._is_dark_mode))
            self._activation_key_input.setVisible(True)
            self._redeem_btn.setVisible(True)
        elif ent == LC.ENT_NONE:
            self._entitlement_status.setText(
                "無料体験を使い切りました。続けて使うにはアクティベーションキーを入力してください。"
            )
            self._entitlement_status.setStyleSheet(MacTheme.status_warn_style(self._is_dark_mode))
            self._activation_key_input.setVisible(True)
            self._redeem_btn.setVisible(True)
        else:  # ENT_UNKNOWN
            self._entitlement_status.setText(
                "ログインするとライセンスの状態を確認できます。"
            )
            self._entitlement_status.setStyleSheet(MacTheme.status_muted_style())
            self._activation_key_input.setVisible(True)
            self._redeem_btn.setVisible(True)

    @staticmethod
    def _format_until(iso: str) -> str:
        """ISO8601 の期限文字列を「YYYY-MM-DD HH:MM」に整形する（失敗時は原文）。"""
        try:
            s = iso.replace("Z", "+00:00")
            return datetime.fromisoformat(s).astimezone().strftime("%Y-%m-%d %H:%M")
        except Exception:
            return iso

    def _on_redeem_clicked(self) -> None:
        """アクティベーションキーを登録する（ネットワーク I/O はワーカースレッド）。"""
        from ..core import login_coordinator

        code = self._activation_key_input.text().strip()
        if not code:
            self._show_redeem_msg(False, "キーを入力してください")
            return
        self._redeem_btn.setEnabled(False)
        self._activation_key_input.setEnabled(False)
        self._redeem_msg.setVisible(False)
        coord = login_coordinator.shared()

        def _work():
            ok, msg = coord.redeem(code)
            self._redeem_done.emit(ok, msg)

        threading.Thread(target=_work, daemon=True).start()

    def _on_redeem_done(self, ok: bool, msg: str) -> None:
        """キー登録の完了（メインスレッド）。結果表示と状態更新を行う。"""
        self._redeem_btn.setEnabled(True)
        self._activation_key_input.setEnabled(True)
        self._show_redeem_msg(ok, msg)
        if ok:
            self._activation_key_input.clear()
        # 成功でも失敗でも最新の利用権状態を描き直す
        self._render_entitlement()

    def _show_redeem_msg(self, ok: bool, msg: str) -> None:
        """キー登録の結果メッセージを色付きで表示する。"""
        self._redeem_msg.setText(msg)
        style = (
            MacTheme.status_ok_style(self._is_dark_mode)
            if ok
            else MacTheme.status_warn_style(self._is_dark_mode)
        )
        self._redeem_msg.setStyleSheet(style)
        self._redeem_msg.setVisible(True)

    def refresh_account_status(self) -> None:
        """ログイン状態に合わせてアカウントページの表示を更新する。

        deep link 完了後（app.py のワーカー）や、ポーリングタイマーから呼ばれる。
        アカウントページが未生成のうちは何もしない。
        """
        if not hasattr(self, "_account_status"):
            return
        from ..core import login_coordinator

        coord = login_coordinator.shared()
        status = coord.status
        LC = login_coordinator.LoginCoordinator

        # ライセンスカードはログイン済みのときだけ見せる
        logged_in = (status == LC.LOGGED_IN)
        if hasattr(self, "_license_card"):
            self._license_card.setVisible(logged_in)

        if status == LC.LOGGED_IN:
            email = coord.account_email
            self._account_status.setText(f"ログイン済み（{email}）" if email else "ログイン済み")
            self._account_status.setStyleSheet(MacTheme.status_ok_style(self._is_dark_mode))
            self._login_btn.setVisible(False)
            self._logout_btn.setVisible(True)
            self._account_poll.stop()
            # 利用権をまだ確認していなければ（復元ログイン直後など）バックグラウンドで取得
            self._ensure_entitlement_refresh()
            self._render_entitlement()
        elif status == LC.WAITING:
            self._account_status.setText("ブラウザでログインを完了してください…")
            self._account_status.setStyleSheet(MacTheme.status_muted_style())
            self._login_btn.setVisible(True)
            self._login_btn.setEnabled(True)
            self._logout_btn.setVisible(False)
        elif status == LC.EXCHANGING:
            self._account_status.setText("ログイン処理中…")
            self._account_status.setStyleSheet(MacTheme.status_muted_style())
            self._login_btn.setEnabled(False)
            self._logout_btn.setVisible(False)
        elif status == LC.FAILED:
            self._account_status.setText(coord.error or "ログインに失敗しました。")
            self._account_status.setStyleSheet(MacTheme.status_warn_style(self._is_dark_mode))
            self._login_btn.setVisible(True)
            self._login_btn.setEnabled(True)
            self._logout_btn.setVisible(False)
            self._account_poll.stop()
        else:  # IDLE（未ログイン）
            self._account_status.setText("未ログイン")
            self._account_status.setStyleSheet(MacTheme.status_muted_style())
            self._login_btn.setVisible(True)
            self._login_btn.setEnabled(True)
            self._logout_btn.setVisible(False)
            self._account_poll.stop()

    # ------------------------------------------------------------------
    # バージョン情報（自動アップデート）
    # ------------------------------------------------------------------

    def _create_version_page(self) -> QWidget:
        """バージョン情報ページを作成する（現在版・更新確認・更新検知時の更新ボタン）。"""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(12)
        layout.setContentsMargins(24, 8, 24, 24)

        card, cl = _make_card()
        _add_row(cl, "現在のバージョン", QLabel(APP_VERSION))
        # 更新状態（最新です / 確認中… / 新バージョン利用可能 / 失敗）
        self._version_status = QLabel("「アップデートを確認」を押すと最新版を確認します")
        self._version_status.setObjectName("caption")
        _add_row(cl, "更新状態", self._version_status)
        layout.addWidget(card)

        # ボタン行（手動確認 + 検知時のみ出る「今すぐ更新する」）
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        self._check_update_btn = QPushButton("アップデートを確認")
        self._check_update_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._check_update_btn.clicked.connect(self._on_check_update_clicked)
        self._update_now_btn = QPushButton("今すぐ更新する")
        self._update_now_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._update_now_btn.setProperty("class", "primary")
        self._update_now_btn.clicked.connect(self._on_update_now_clicked)
        self._update_now_btn.setVisible(False)  # 新バージョン検知時のみ表示
        btn_row.addWidget(self._check_update_btn)
        btn_row.addWidget(self._update_now_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        updates_enabled = self._updater is not None and self._updater.is_enabled()
        layout.addWidget(_make_caption(
            "新しいバージョンが見つかると「今すぐ更新する」ボタンが表示されます。"
            "更新は起動時と 1 日ごとに自動で確認されます。"
            if updates_enabled else
            # Windows 版は配布物が無い（ソースから使う）ので、更新は git pull で行う
            "Windows 版は配布していません。ソースのフォルダで git pull して起動し直すと最新になります。"
        ))
        layout.addStretch()

        # updater のチェック結果を表示へ反映（ウィンドウを開いたまま検知しても更新される）
        if self._updater is not None and not updates_enabled:
            # 更新フィードが無い（Windows 版は配布していない）。押しても確認先が無いのでボタンを止める
            self._check_update_btn.setEnabled(False)
            from ..utils.updater import DISABLED_MESSAGE
            self._version_status.setText(DISABLED_MESSAGE)
        elif self._updater is not None:
            self._updater.update_available.connect(self._on_update_available)
            self._updater.up_to_date.connect(self._on_up_to_date)
            self._updater.update_failed.connect(self._on_update_failed)
        else:
            # 開発実行などで updater が無い場合は確認ボタンを無効化する
            self._check_update_btn.setEnabled(False)
            self._version_status.setText("このビルドでは自動アップデートは利用できません")

        return self._wrap_scroll(page)

    def _on_check_update_clicked(self) -> None:
        """「アップデートを確認」: 手動チェックを開始し、待機中であることを表示する。"""
        if self._updater is None:
            return
        self._version_status.setText("確認中…")
        self._version_status.setStyleSheet(MacTheme.status_muted_style())
        self._updater.check_now(manual=True)

    def _on_update_available(self, version: str) -> None:
        """新バージョン検知: 状態表示と「今すぐ更新する」ボタンを出す。"""
        if not hasattr(self, "_version_status"):
            return
        self._version_status.setText(f"新しいバージョン {version} が利用可能です")
        self._version_status.setStyleSheet(MacTheme.status_ok_style(self._is_dark_mode))
        self._update_now_btn.setVisible(True)
        self._update_now_btn.setEnabled(True)
        self._update_now_btn.setText("今すぐ更新する")

    def _on_up_to_date(self) -> None:
        """最新版だった場合の表示。"""
        if not hasattr(self, "_version_status"):
            return
        self._version_status.setText("最新です")
        self._version_status.setStyleSheet(MacTheme.status_ok_style(self._is_dark_mode))
        self._update_now_btn.setVisible(False)

    def _on_update_failed(self, message: str) -> None:
        """更新確認/ダウンロードの失敗表示。"""
        if not hasattr(self, "_version_status"):
            return
        self._version_status.setText(message or "更新に失敗しました")
        self._version_status.setStyleSheet(MacTheme.status_warn_style(self._is_dark_mode))
        # 失敗したら再試行できるよう更新ボタンを戻す
        self._update_now_btn.setEnabled(True)
        self._update_now_btn.setText("今すぐ更新する")

    def _on_update_now_clicked(self) -> None:
        """「今すぐ更新する」: インストーラの DL とサイレント実行を開始する。"""
        if self._updater is None:
            return
        self._update_now_btn.setEnabled(False)
        self._update_now_btn.setText("更新を準備中…")
        self._version_status.setText("更新を準備中… 完了するとアプリが再起動します")
        self._version_status.setStyleSheet(MacTheme.status_muted_style())
        self._updater.download_and_install()

    # ------------------------------------------------------------------
    # 入力デバイス
    # ------------------------------------------------------------------

    def _populate_input_devices(self) -> None:
        """入力デバイス一覧をコンボボックスへ読み込む。"""
        current_value = "default"
        if hasattr(self, "_input_device_combo") and self._input_device_combo.count() > 0:
            current_data = self._input_device_combo.currentData()
            if current_data is not None:
                current_value = current_data

        self._input_device_combo.clear()
        self._input_device_combo.addItem("システム既定", "default")

        devices = AudioRecorder.list_input_devices()
        for device in devices:
            device_id = int(device["id"])
            label = f"{device_id}: {device['label']}"
            self._input_device_combo.addItem(label, device_id)

        self._set_input_device_selection(current_value)

    def _set_input_device_selection(self, value) -> None:
        """入力デバイス選択を設定値に合わせる。"""
        normalized = AudioRecorder.normalize_device_setting(value)
        target_value = "default" if normalized is None else normalized

        for index in range(self._input_device_combo.count()):
            if self._input_device_combo.itemData(index) == target_value:
                self._input_device_combo.setCurrentIndex(index)
                return

        self._input_device_combo.setCurrentIndex(0)

    def _start_mic_auto_detect(self) -> None:
        """マイク自動検出を開始する（検出処理は使い捨てスレッドで実行）。"""
        from ..core.mic_auto_detect import detect_async

        self._mic_detect_button.setEnabled(False)
        self._mic_detect_status.setText("自動検出中… マイクに向かって喋り続けてください（数秒）")
        self._mic_detect_status.setVisible(True)
        # コールバックはワーカースレッドから呼ばれるため、Signal でメインスレッドへ渡す
        detect_async(self._mic_detect_done.emit)

    def _on_mic_detect_done(self, result) -> None:
        """マイク自動検出の完了処理（メインスレッド）。"""
        self._mic_detect_button.setEnabled(True)
        if result:
            # 一覧を最新化してから検出デバイスを選択する（保存は「保存」ボタンで確定）
            self._populate_input_devices()
            self._set_input_device_selection(int(result["id"]))
            self._mic_detect_status.setText(f"「{result['name']}」を選択しました")
            display_ms = 2000
        else:
            self._mic_detect_status.setText(
                "音声を検出できませんでした。喋りながらもう一度お試しください"
            )
            display_ms = 4000
        # 結果表示は数秒で消す（再実行中に消さないようガード）
        QTimer.singleShot(
            display_ms,
            lambda: self._mic_detect_button.isEnabled() and self._mic_detect_status.setVisible(False),
        )

    # ------------------------------------------------------------------
    # マイクテスト（録音せずレベルだけ確認・無料枠を消費しない）
    # ------------------------------------------------------------------

    def _toggle_mic_test(self) -> None:
        """マイクテストの開始/停止をトグルする（選択中のマイクでレベルを表示）。"""
        if self._mic_monitor.is_running:
            self._stop_mic_test()
            return
        device = self._input_device_combo.currentData() or "default"
        if self._mic_monitor.start(device):
            self._mic_test_timer.start()
            self._mic_test_button.setText("停止")
        else:
            # 開始できないときは無言にせずボタン文言で知らせる
            self._mic_test_button.setText("開けません")
            QTimer.singleShot(1500, lambda: self._mic_test_button.setText("テスト"))

    def _stop_mic_test(self) -> None:
        """マイクテストを停止する（ウィンドウを隠すときにも呼ぶ）。"""
        self._mic_test_timer.stop()
        self._mic_monitor.stop()
        self._mic_test_bar.setValue(0)
        self._mic_test_button.setText("テスト")

    def _poll_mic_test_level(self) -> None:
        self._mic_test_bar.setValue(int(self._mic_monitor.read_level() * 100))

    def hideEvent(self, event) -> None:
        """設定を閉じる/隠すときはマイクテストのストリームを必ず閉じる。"""
        self._stop_mic_test()
        super().hideEvent(event)

    # ------------------------------------------------------------------
    # 設定の読み込み・保存
    # ------------------------------------------------------------------

    def _load_current_settings(self) -> None:
        """設定ファイルから現在の値をUIに読み込む。

        ウィンドウは使い回されるため、このメソッドは「永続 config を UI に流し込んで
        未保存の編集・テーマプレビューを破棄する」役目も持つ（showEvent から毎回呼ぶ）。
        """
        config = self._config_manager.config

        # テーマも永続 config に合わせて戻す（未保存のプレビューを破棄）
        self._is_dark_mode = config.get("dark_mode", False)
        self._theme_toggle.set_dark(self._is_dark_mode)
        self._apply_theme(self._is_dark_mode)

        # 一般 - 言語（保存値が選択肢に無くても選択を保持して表示する）
        language = config.get("language", "ja") or ""
        lang_index = self._lang_combo.findData(language)
        if lang_index < 0:
            self._lang_combo.addItem(language, language)
            lang_index = self._lang_combo.count() - 1
        self._lang_combo.setCurrentIndex(lang_index)

        # 録音キー 1 / 2（既定は DEFAULT_CONFIG と同じ: 1 = Soniox / 2 = Groq）
        slot_defaults = {1: ("<f2>", backends.SONIOX), 2: ("<f3>", backends.GROQ)}
        for slot_id, (default_hotkey, default_backend) in slot_defaults.items():
            hotkey_config = config.get(f"hotkey{slot_id}", {}) or {}

            getattr(self, f"_hotkey{slot_id}_input").setText(
                hotkey_config.get("hotkey", default_hotkey)
            )

            mode_combo = getattr(self, f"_mode{slot_id}_combo")
            mode_index = mode_combo.findData(
                hotkey_config.get("hotkey_mode", HotkeyMode.TOGGLE.value)
            )
            if mode_index < 0:
                mode_index = mode_combo.findData(HotkeyMode.TOGGLE.value)
            mode_combo.setCurrentIndex(mode_index)
            getattr(self, f"_mode{slot_id}_caption").setText(_mode_caption(mode_combo.currentData()))

            # エンジン＋モデル（保存値をそのまま選択状態にする。一覧外のモデル・旧 Deepgram もそのまま見せる）
            backend = str(hotkey_config.get("backend", default_backend) or default_backend)
            if not backends.is_supported(backend):
                backend = backends.GROQ
            model = backends.resolve_model(backend, hotkey_config.get("api_model", ""))
            self._slot_choice[slot_id] = (backend, model)
            self._render_engine_button(slot_id)

            # プロンプト（api_prompt）入力欄は UI に無い（保存値は _collect_slot_config が維持する）

            # テキスト整形（録音キーページとプロンプトページの両方に同じトグルがある）
            format_on = bool(hotkey_config.get("format_enabled", False))
            getattr(self, f"_format{slot_id}_check").setChecked(format_on)
            self._sync_format_toggle(slot_id, format_on)

        # 整形の指示（空＝既定文。既定文をそのまま編集できるよう実テキストを出す）と整形モデル
        stored_prompt = str(config.get("format_auto_prompt", "") or "")
        self._prompt_edit.setPlainText(
            stored_prompt if stored_prompt.strip() else text_formatter.DEFAULT_FORMAT_PROMPT
        )
        self._previous_prompt = None
        self._prompt_undo_btn.setVisible(False)
        self._on_prompt_edited()
        self._skip_format_for_ai_check.setChecked(bool(config.get("skip_format_for_ai", True)))
        self._fill_format_model_combo(
            str(config.get("format_model", text_formatter.DEFAULT_FORMAT_MODEL) or text_formatter.DEFAULT_FORMAT_MODEL)
        )
        self._repaste_input.setText(config.get("repaste_key", "") or "")
        # 翻訳して入力
        self._translate_once_input.setText(config.get("translate_once_key", "") or "")
        self._translate_always_check.setChecked(bool(config.get("translate_input_enabled", False)))
        target_index = self._translate_target_combo.findData(config.get("translate_input_target", "en"))
        self._translate_target_combo.setCurrentIndex(max(0, target_index))
        engine = config.get("translate_input_engine", "groq")
        self._translate_choice = (
            "gemini" if engine == "gemini" else "groq",
            str(config.get("translate_input_groq_model", "") or llm_models.RECOMMENDED_GROQ),
            str(config.get("translate_input_gemini_model", "") or llm_models.RECOMMENDED_GEMINI),
        )
        self._render_translate_engine()
        self._history_enabled_check.setChecked(bool(config.get("history_enabled", True)))
        self._on_hotkey_changed()

        # 一般 - その他（VAD/分割/HUD/ストリーミング/音量正規化は常時ON固定のため UI 無し）
        # 自動起動はレジストリの実状態を反映（settings.yaml には持たない）
        self._autostart_check.setChecked(autostart.is_enabled())
        self._hud_always_visible_check.setChecked(
            bool(config.get("hud_always_visible", False))
        )
        self._side_notch_check.setChecked(
            bool(config.get("side_notch_enabled", True))
        )
        self._sound_effects_check.setChecked(
            bool(config.get("sound_effects_enabled", True))
        )
        self._duck_media_check.setChecked(
            bool(config.get("duck_media_enabled", True))
        )
        # 数字入力（マスター / 助数詞 / 保護リスト）
        self._numeral_enabled_check.setChecked(
            bool(config.get("numeral_normalize_enabled", True))
        )
        self._numeral_counter_check.setChecked(
            bool(config.get("numeral_convert_counter", True))
        )
        # マスター OFF のときは助数詞トグルを無効表示にする
        self._numeral_counter_check.setEnabled(self._numeral_enabled_check.isChecked())
        self._numeral_protect_list.clear()
        for word in config.get("numeral_protect_words", []) or []:
            if isinstance(word, str) and word:
                self._numeral_protect_list.addItem(word)
        self._handsfree_input.setText(config.get("handsfree_key", ""))
        self._auto_enter_delay_slider.setValue(config.get("auto_enter_delay_ms", 50))
        self._populate_input_devices()
        self._set_input_device_selection(config.get("audio_input_device", "default"))

        # 製品版は文字起こしモデル・整形モデル/指示が固定（UI 非公開）のため読み込み不要。

        # API キーの状態（どこから読めるか）
        self._refresh_all_api_key_status()

        # Mac との履歴共有
        hs = config.get("history_sync", {}) or {}
        self._history_sync_enabled_toggle.setChecked(bool(hs.get("enabled", False)))
        self._history_sync_detail_card.setVisible(self._history_sync_enabled_toggle.isChecked())
        self._history_sync_url_input.setText(str(hs.get("url", "")))
        self._refresh_history_sync_token_status()
        self._refresh_history_sync_status()

    def _toggle_theme(self) -> None:
        """ダーク/ライトモードを切り替える。"""
        self._is_dark_mode = not self._is_dark_mode
        self._apply_theme(self._is_dark_mode)

    def _apply_theme(self, is_dark: bool) -> None:
        """
        テーマモードに基づいてグローバルスタイルシートを適用する。

        Args:
            is_dark: ダークモードの場合True
        """
        stylesheet = MacTheme.get_stylesheet(is_dark)
        self.setStyleSheet(stylesheet)
        # トグルスイッチは QSS ではなく自前描画のため、テーマを個別に伝える
        for toggle in self._toggles:
            toggle.set_dark(is_dark)
        # サイドバーアイコンは色がテーマ依存なので作り直す
        self._apply_nav_icons()
        # 実績チャートも QSS ではなく自前描画のため、テーマ色を個別に伝える
        if getattr(self, "_usage_chart", None) is not None:
            c = MacTheme.Colors(is_dark)
            # HAIRLINE は rgba() 文字列で QColor が解釈できないため、グリッド色は明示生成する
            grid = QColor(255, 255, 255, 28) if is_dark else QColor(0, 0, 0, 28)
            self._usage_chart.set_colors(c.ACCENT, c.SECONDARY_TEXT, grid)

    def _save_settings(self) -> None:
        """設定をファイルに保存する。"""
        # 既存のdev_mode, llm_postprocess設定を保持
        existing_dev_mode = self._config_manager.get("dev_mode", False)
        existing_llm_postprocess = self._config_manager.get("llm_postprocess", {})
        selected_input_device = self._input_device_combo.currentData()
        if selected_input_device is None:
            selected_input_device = "default"

        new_config = {
            # グローバル設定
            "language": self._lang_combo.currentData() or "",
            "audio_input_device": selected_input_device,
            "auto_enter_delay_ms": self._auto_enter_delay_slider.value(),
            # 待機中も小型ピルを常時表示するか（Mac 版 hudAlwaysVisible と同義）
            "hud_always_visible": self._hud_always_visible_check.isChecked(),
            # 画面左端のサイドノッチ（履歴スリット）を表示するか（Mac 版 sideNotchEnabled と同義）
            "side_notch_enabled": self._side_notch_check.isChecked(),
            # 操作音（録音開始/停止の効果音）を鳴らすか（Mac 版 soundEffectsEnabled と同義）
            "sound_effects_enabled": self._sound_effects_check.isChecked(),
            # 録音中は他アプリ（メディア）の音量を下げるか（Mac 版 duckMediaEnabled と同義）
            "duck_media_enabled": self._duck_media_check.isChecked(),

            # ハンズフリー切替キー
            "handsfree_key": self._handsfree_input.text(),
            # ユーザー辞書（確定置換）。貼り付け直前に from→to を機械置換する
            "replacements": self._collect_replacements(),
            # 数字入力の正規化（Mac 版 ConfigStore と同義）
            "numeral_normalize_enabled": self._numeral_enabled_check.isChecked(),
            "numeral_convert_counter": self._numeral_counter_check.isChecked(),
            "numeral_protect_words": self._collect_protect_words(),
            # vad_filter / vad_min_silence_duration_ms / split_parallel_enabled /
            # streaming_enabled / hud_enabled / audio_preprocess.volume_normalize は
            # 常時 ON 固定（UI 撤去）。ここでは保存せず config_manager 側で True を強制する。

            # 整形の指示（既定文と同じなら空で保存＝実行時に最新の既定文を使う・Mac と同じ）と整形モデル
            "format_auto_prompt": text_formatter.stored_prompt(self._prompt_edit.toPlainText()),
            # 貼り付け先が AI のときは整形しない（Mac の skipFormatForAI と同義）
            "skip_format_for_ai": self._skip_format_for_ai_check.isChecked(),
            "format_model": self._format_model_combo.currentData() or text_formatter.DEFAULT_FORMAT_MODEL,
            # 最後の文字起こしをもう一度貼り付けるキー（空＝無効）
            "repaste_key": self._repaste_input.text(),
            # 音声入力の履歴を保存するか
            "history_enabled": self._history_enabled_check.isChecked(),
            # 翻訳して入力（Mac の DictationTranslation と同じ項目）
            "translate_once_key": self._translate_once_input.text(),
            "translate_input_enabled": self._translate_always_check.isChecked(),
            "translate_input_target": self._translate_target_combo.currentData() or "en",
            "translate_input_engine": self._translate_choice[0],
            "translate_input_groq_model": self._translate_choice[1],
            "translate_input_gemini_model": self._translate_choice[2],

            # ホットキー1 設定
            "hotkey1": self._collect_slot_config(1),

            # ホットキー2 設定
            "hotkey2": self._collect_slot_config(2),

            # Mac との履歴共有
            "history_sync": {
                "enabled": self._history_sync_enabled_toggle.isChecked(),
                "url": self._history_sync_url_input.text().strip().rstrip("/"),
            },

            # その他の設定
            "dark_mode": self._is_dark_mode,
            "dev_mode": existing_dev_mode,
            "llm_postprocess": existing_llm_postprocess,
        }

        if self._config_manager.save(new_config):
            # 自動起動は保存成功と同じ境界でのみ反映する。先に反映すると保存失敗時に
            # 「自動起動だけ変わって設定は元のまま」という不整合が残るため（item 15）。
            # 自動起動は settings.yaml ではなくレジストリで管理するので別途反映する。
            if autostart.is_supported():
                if not autostart.set_enabled(self._autostart_check.isChecked()):
                    QMessageBox.warning(
                        self, "注意",
                        "設定は保存しましたが、ログイン時起動の更新に失敗しました。",
                    )
            # 本体へ設定変更を即時適用させる（共有マネージャなので mtime ポーリングを待たない）
            self.settings_saved.emit()
            self.close()
        else:
            QMessageBox.critical(self, "エラー", "設定の保存に失敗しました。")

    def _collect_slot_config(self, slot_id: int) -> dict:
        """
        スロットの UI 状態を settings.yaml 用の辞書にまとめる。

        Args:
            slot_id: スロットID（1または2）

        Returns:
            hotkey1 / hotkey2 セクションに保存する設定辞書
        """
        # api_prompt の入力欄は UI に無いため、既存の保存値をそのまま維持する
        # （保存フィールド自体は互換のため残す。空文字で上書きしない）。
        existing_slot = self._config_manager.get(f"hotkey{slot_id}", {}) or {}
        backend, model = self._slot_choice[slot_id]
        return {
            "hotkey": getattr(self, f"_hotkey{slot_id}_input").text(),
            "hotkey_mode": getattr(self, f"_mode{slot_id}_combo").currentData(),
            "backend": backend,
            # 既定モデルなら空で保存する（既定モデルが将来変わったとき自動で追従させるため。
            # 一覧外の自由入力・既定以外を選んだときはその名前を保存する）
            "api_model": "" if model == backends.default_model(backend) else model,
            "api_prompt": existing_slot.get("api_prompt", ""),
            "format_enabled": getattr(self, f"_format{slot_id}_check").isChecked(),
        }
