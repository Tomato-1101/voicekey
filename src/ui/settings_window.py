"""
設定ウィンドウモジュール

Mac 版 SettingsView.swift と同じ 5 ページ構成（一般 / ホットキー 1 / ホットキー 2 / 履歴 / API キー）
・同じ日本語文言で設定を管理する。
レイアウトは macOS System Settings 風の「左サイドバーナビ + カード型セクション」。
ダーク/ライトテーマ切り替えに対応（Qt は OS テーマに自動追従しないため Windows 版のみ）。
"""

import importlib.util
import os
from typing import Optional

from PySide6.QtCore import (
    Property,
    Qt,
    QEasingCurve,
    QPointF,
    QPropertyAnimation,
    QRectF,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import (
    QBrush,
    QColor,
    QDesktopServices,
    QKeyEvent,
    QPainter,
    QPainterPath,
    QPen,
)
from PySide6.QtWidgets import (
    QApplication,
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
    QPushButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ..config import ConfigManager, HotkeyMode, TranscriptionBackend
from ..config.constants import APP_VERSION
from ..core.audio_recorder import AudioRecorder
from ..core.history import MAX_ITEMS as HISTORY_MAX_ITEMS, HistoryStore
from ..core.stats import StatsStore
from ..platform import PlatformAdapter, get_platform_adapter
from ..utils import autostart, secrets
from .styles import MacTheme


# 設定ウィンドウ上の backend 選択値と Keychain サービス識別子のマッピング。
# Hotkey1 / Hotkey2 で同じ backend を使う場合は同じ Keychain エントリを共有する。
_BACKEND_TO_SERVICE = {
    TranscriptionBackend.GROQ.value: secrets.SERVICE_GROQ,
    TranscriptionBackend.OPENAI.value: secrets.SERVICE_OPENAI,
    TranscriptionBackend.ELEVENLABS.value: secrets.SERVICE_ELEVENLABS,
    TranscriptionBackend.DEEPGRAM.value: secrets.SERVICE_DEEPGRAM,
}

# 提供元名（API キー欄でどのキーかを示すためだけに使う。配布版では API キータブを
# 隠すので開発時にしか表示されない。Mac 版 Backend.providerName と一致）
_BACKEND_PROVIDER_NAMES = {
    TranscriptionBackend.OPENAI.value: "OpenAI",
    TranscriptionBackend.GROQ.value: "Groq",
    TranscriptionBackend.ELEVENLABS.value: "ElevenLabs",
    TranscriptionBackend.DEEPGRAM.value: "Deepgram",
}

# 製品版（release）で文字起こしに選べる 2 択（表示順）。
# Deepgram=「高速リアルタイム」/ ElevenLabs=「正確性」。モデルは推奨固定で非選択。
# Mac 版 Backend.selectableCases / Backend.label と一致させる。
_TRANSCRIBE_BACKEND_LABELS = [
    (TranscriptionBackend.DEEPGRAM.value, "高速リアルタイム"),
    (TranscriptionBackend.ELEVENLABS.value, "正確性"),
]

# 製品版の API キータブに出すバックエンド（開発ビルドのみ表示）。
# 文字起こし 2 択＋裏のテキスト整形に使う Groq。OpenAI は使わない。
_API_KEY_BACKENDS = [
    TranscriptionBackend.DEEPGRAM.value,
    TranscriptionBackend.ELEVENLABS.value,
    TranscriptionBackend.GROQ.value,
]

# ホットキー動作モードの表示順と UI ラベル（Mac 版 HotkeyMode.label と完全一致させる）
_MODE_LABELS = [
    (HotkeyMode.HOLD.value, "押している間"),
    (HotkeyMode.TOGGLE.value, "トグル"),
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
) -> None:
    """カードへ標準行（左ラベル + 右コントロール、任意で下に補足）を追加する。"""
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

    if caption:
        v.addWidget(_make_caption(caption))
    card_layout.addWidget(row)


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
        on = QColor("#0A84FF") if self._dark else QColor("#007AFF")
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
        platform_adapter: Optional[PlatformAdapter] = None
    ):
        """ホットキー入力ウィジェットを初期化する。"""
        super().__init__(parent)
        self._platform = platform_adapter or get_platform_adapter()
        self.setReadOnly(True)
        self.setPlaceholderText("クリックしてキーを押す…")
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
        modifiers = event.modifiers()

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

    Mac 版と同じ 5 タブ（一般 / ホットキー 1 / ホットキー 2 / 履歴 / API キー）で設定を管理する。
    設定は settings.yaml に保存し、アプリ側のホットリロードで即反映される。
    """

    # マイク自動検出の完了通知（ワーカースレッド → メインスレッドへ結果を渡す）
    _mic_detect_done = Signal(object)
    # 設定保存の完了通知（アプリ本体が設定変更を即座に適用するために購読する）
    settings_saved = Signal()

    def __init__(
        self,
        platform_adapter: Optional[PlatformAdapter] = None,
        history: Optional[HistoryStore] = None,
        stats: Optional[StatsStore] = None,
        config_manager: Optional[ConfigManager] = None,
        updater=None,
    ) -> None:
        """
        設定ウィンドウを初期化する。

        Args:
            platform_adapter: プラットフォーム依存処理のアダプタ
            history: 音声入力履歴ストア（「履歴」タブで表示・再コピーする）
            stats: 使用実績ストア（「実績」タブで表示する）
            config_manager: アプリ本体と共有する設定マネージャ（None なら単体生成）
            updater: 自動アップデータ（「バージョン情報」タブで更新確認/実行に使う。None なら無効表示）
        """
        super().__init__()
        self._platform = platform_adapter or get_platform_adapter()
        self._history = history
        self._stats = stats
        self._updater = updater

        # DIST ビルドでは API キーページを作らないため、ページ生成前に空で初期化しておく
        # （_load_current_settings が無条件に _refresh_api_key_status を呼ぶ）
        self._api_key_inputs = {}
        self._api_key_status = {}

        # テーマ切替時にトラック色を更新するトグルスイッチの一覧
        self._toggles: list[ToggleSwitch] = []

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
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # --- 左サイドバー: ブランド + ナビゲーション ---
        side = QFrame()
        side.setObjectName("sidebarPane")
        side.setFixedWidth(172)
        sv = QVBoxLayout(side)
        sv.setContentsMargins(8, 16, 8, 10)
        sv.setSpacing(2)

        brand = QLabel("voicekey")
        brand.setObjectName("brand")
        brand_caption = QLabel("設定")
        brand_caption.setObjectName("brandCaption")
        sv.addWidget(brand)
        sv.addWidget(brand_caption)
        sv.addSpacing(12)

        self._nav = QListWidget()
        self._nav.setObjectName("sidebar")
        self._nav.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        sv.addWidget(self._nav, 1)
        root.addWidget(side)

        # --- 右コンテンツ: ページタイトル + ページ + 保存/キャンセル ---
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.setSpacing(0)

        header = QWidget()
        hh = QHBoxLayout(header)
        hh.setContentsMargins(24, 18, 20, 8)
        hh.setSpacing(10)
        self._page_title = QLabel("")
        self._page_title.setObjectName("pageTitle")
        # テーマ切替ボタン（Qt は OS テーマに追従しないため Windows 版独自）
        self._theme_toggle = ThemeToggleButton(is_dark=self._is_dark_mode)
        self._theme_toggle.clicked.connect(self._toggle_theme)
        hh.addWidget(self._page_title)
        hh.addStretch()
        hh.addWidget(self._theme_toggle)
        rv.addWidget(header)

        # 6 ページ（Mac 版 SettingsView と同じ構成・順序）
        self._pages = QStackedWidget()
        page_defs = [
            ("一般", self._create_general_page()),
            ("ホットキー 1", self._create_slot_page(1)),
            ("ホットキー 2", self._create_slot_page(2)),
            ("実績", self._create_stats_page()),
            ("履歴", self._create_history_page()),
        ]
        self._stats_page_index = 3
        self._history_page_index = 4
        # アカウント（ブラウザ経由ログイン）。製品版の中核機能なので常時表示する。
        page_defs.append(("アカウント", self._create_account_page()))
        # バージョン情報（現在版表示・更新確認・更新検知時の「今すぐ更新する」）。常時表示。
        page_defs.append(("バージョン情報", self._create_version_page()))
        # 配布ビルドは埋め込みキーで動くため、API キーページは出さない（テスターの混乱防止）
        if not secrets.is_dist_build():
            page_defs.append(("API キー", self._create_api_keys_page()))
        for title, page in page_defs:
            self._nav.addItem(title)
            self._pages.addWidget(page)
        rv.addWidget(self._pages, 1)

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
        self._nav.setCurrentRow(0)

    def _make_toggle(self) -> ToggleSwitch:
        """テーマ追従するトグルスイッチを作って登録する。"""
        toggle = ToggleSwitch()
        toggle.set_dark(self._is_dark_mode)
        self._toggles.append(toggle)
        return toggle

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

    def _create_general_page(self) -> QWidget:
        """一般ページを作成する（基本 / 音声処理 / 表示と動作 / テキスト整形 / 起動）。"""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(24, 8, 24, 24)
        layout.setSpacing(14)

        # --- カード: 基本（言語・入力デバイス） ---
        card, cl = _make_card()

        # 言語（Mac 版と同じ 3 択。空文字 = API 側の自動判定）
        self._lang_combo = QComboBox()
        for label, value in _LANGUAGE_OPTIONS:
            self._lang_combo.addItem(label, value)
        self._lang_combo.setMinimumWidth(160)
        _add_row(cl, "言語", self._lang_combo)

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
        device_head.addWidget(QLabel("入力デバイス"))
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

        layout.addWidget(card)

        # --- カード: 動作（自動 Enter・ハンズフリー） ---
        # VAD / VAD 最小無音時間 / 長文分割 / 音量正規化 / 録音 HUD / リアルタイム
        # ストリーミングは常時 ON に固定したため設定 UI から撤去した
        # （常時 ON の強制は config_manager._load_config 側で行う）
        card, cl = _make_card()

        # Auto Enter 遅延（ダブルタップ時、テキスト挿入後のEnter押下までの待機時間）
        # 一部アプリが即時Enterに反応しないため、ユーザー側で調整可能にする
        self._auto_enter_delay_slider = QSlider(Qt.Orientation.Horizontal)
        self._auto_enter_delay_slider.setRange(0, 500)
        self._auto_enter_delay_slider.setSingleStep(10)
        self._auto_enter_delay_slider.setPageStep(50)
        self._auto_enter_delay_slider.setFixedWidth(180)

        self._auto_enter_delay_label = QLabel("50 ms")
        self._auto_enter_delay_label.setMinimumWidth(50)
        self._auto_enter_delay_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self._auto_enter_delay_slider.valueChanged.connect(
            lambda v: self._auto_enter_delay_label.setText(f"{v} ms")
        )

        delay_row = QWidget()
        delay_row_layout = QHBoxLayout(delay_row)
        delay_row_layout.setContentsMargins(0, 0, 0, 0)
        delay_row_layout.setSpacing(8)
        delay_row_layout.addWidget(self._auto_enter_delay_slider)
        delay_row_layout.addWidget(self._auto_enter_delay_label)
        _add_row(
            cl, "自動 Enter の遅延", delay_row,
            caption="ホットキーを素早く 2 回押すと、貼り付け後に Enter を送信します。",
        )

        # ハンズフリー切替キー（この切替キー＋ホットキーで toggle 録音になる。空＝無効）
        self._handsfree_input = HotkeyInput(platform_adapter=self._platform)
        self._handsfree_input.setFixedWidth(220)
        handsfree_clear = QPushButton("クリア")
        handsfree_clear.setCursor(Qt.CursorShape.PointingHandCursor)
        handsfree_clear.clicked.connect(self._handsfree_input.clear)
        handsfree_row = QWidget()
        handsfree_row_layout = QHBoxLayout(handsfree_row)
        handsfree_row_layout.setContentsMargins(0, 0, 0, 0)
        handsfree_row_layout.setSpacing(8)
        handsfree_row_layout.addWidget(self._handsfree_input)
        handsfree_row_layout.addWidget(handsfree_clear)
        handsfree_row_layout.addStretch()
        _add_row(
            cl, "ハンズフリー切替キー", handsfree_row,
            caption=(
                "切替キー＋ホットキーで、トグル録音（1 回で開始・もう 1 回で停止）になります。"
                "修飾キー（右 Shift など）を推奨。"
            ),
        )

        layout.addWidget(card)

        # 製品版はテキスト整形のモデル・指示文を固定（UI 非公開）。
        # オンオフはホットキー各タブの「テキスト整形（LLM）」トグルで切り替える。

        # --- カード: 起動 ---
        card, cl = _make_card()

        # ログイン時に自動起動（Windows のみ。Mac ネイティブ版は SMAppService で対応）。
        # 状態はレジストリ側が真実なので settings.yaml には保存しない
        self._autostart_check = self._make_toggle()
        self._autostart_check.setEnabled(autostart.is_supported())
        _add_row(
            cl, "ログイン時に起動", self._autostart_check,
            caption="" if autostart.is_supported() else "この機能は Windows でのみ利用できます。",
        )

        layout.addWidget(card)
        layout.addStretch()

        return self._wrap_scroll(page)

    # ------------------------------------------------------------------
    # ホットキーページ（Mac 版 SlotSettingsTab と同項目・同文言）
    # ------------------------------------------------------------------

    def _create_slot_page(self, slot_id: int) -> QWidget:
        """
        ホットキースロットのページを作成する。

        Args:
            slot_id: スロットID（1または2）

        Returns:
            ホットキー設定のページ
        """
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(24, 8, 24, 24)
        layout.setSpacing(14)

        card, cl = _make_card()

        # ホットキー入力
        hotkey_input = HotkeyInput(platform_adapter=self._platform)
        hotkey_input.setFixedWidth(220)
        setattr(self, f"_hotkey{slot_id}_input", hotkey_input)
        _add_row(cl, "ホットキー", hotkey_input)

        # 動作モード（表示は日本語ラベル、保存値は userData の識別子）
        mode_combo = QComboBox()
        for value, label in _MODE_LABELS:
            mode_combo.addItem(label, value)
        mode_combo.setMinimumWidth(160)
        setattr(self, f"_mode{slot_id}_combo", mode_combo)
        _add_row(cl, "動作", mode_combo)

        # バックエンド選択（製品版は 2 択: 高速リアルタイム / 正確性）。
        # モデルは推奨固定で非選択（Deepgram=nova-3 / ElevenLabs=scribe_v1）。
        backend_combo = QComboBox()
        for value, label in _TRANSCRIBE_BACKEND_LABELS:
            backend_combo.addItem(label, value)
        backend_combo.setMinimumWidth(180)
        setattr(self, f"_backend{slot_id}_combo", backend_combo)
        _add_row(cl, "バックエンド", backend_combo)

        # プロンプト入力（文字起こしのヒント）
        prompt_input = QLineEdit()
        prompt_input.setPlaceholderText("専門用語や固有名詞のヒントを入力")
        setattr(self, f"_api{slot_id}_prompt_input", prompt_input)
        prompt_block = QWidget()
        prompt_layout = QVBoxLayout(prompt_block)
        prompt_layout.setContentsMargins(0, 0, 0, 0)
        prompt_layout.setSpacing(6)
        prompt_layout.addWidget(QLabel("プロンプト（任意）"))
        prompt_layout.addWidget(prompt_input)
        _add_block(cl, prompt_block)

        # テキスト整形（LLM）: 貼り付け前に高速 LLM で 1 回整形する
        # （整形内容は LLM が自動判断。指示は「一般」ページで編集可。Mac 版と構成を一致させる）
        format_check = self._make_toggle()
        setattr(self, f"_format{slot_id}_check", format_check)
        _add_row(cl, "テキスト整形（LLM）", format_check)

        layout.addWidget(card)
        layout.addStretch()

        return self._wrap_scroll(page)

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

    @staticmethod
    def _format_saved(seconds: float) -> str:
        """累計節約秒数を「X 時間 Y 分」等に整形する（Mac 版 formattedSaved と同じ規則）。"""
        total = int(round(seconds))
        if total >= 3600:
            return f"{total // 3600} 時間 {(total % 3600) // 60} 分"
        if total >= 60:
            return f"{total // 60} 分 {total % 60} 秒"
        return f"{total} 秒"

    def _create_history_page(self) -> QWidget:
        """履歴ページを作成する（直近の音声入力をクリックで再コピー）。"""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(10)
        layout.setContentsMargins(24, 8, 24, 24)

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
            f"音声入力の直近 {HISTORY_MAX_ITEMS} 件です。行をクリックすると"
            "クリップボードにコピーします。履歴はこの PC の中だけに保存されます。"
        ))

        self._refresh_history()
        return page

    def _on_nav_changed(self, row: int) -> None:
        """サイドバー選択でページを切り替え、タイトルを更新する。"""
        if row < 0:
            return
        self._pages.setCurrentIndex(row)
        self._page_title.setText(self._nav.item(row).text())
        # ウィンドウを開いたまま音声入力しても、各ページを開いた時点で最新になるように
        if row == self._history_page_index:
            self._refresh_history()
        elif row == self._stats_page_index:
            self._refresh_stats()

    def _refresh_history(self) -> None:
        """履歴一覧をストアの現在の内容で作り直す。"""
        self._history_list.clear()
        items = self._history.items() if self._history is not None else []

        if not items:
            placeholder = QListWidgetItem(
                f"まだ履歴がありません。音声入力すると、ここに直近 {HISTORY_MAX_ITEMS} 件が残ります。"
            )
            # 選択・クリック不可の案内行にする
            placeholder.setFlags(Qt.ItemFlag.NoItemFlags)
            self._history_list.addItem(placeholder)
            return

        for entry in items:
            text = entry["text"]
            # 一覧は 2 行プレビュー + 日時。全文はクリック時のコピーとツールチップで確認できる
            preview = text if len(text) <= 80 else text[:80] + "…"
            date = entry["date"].replace("T", " ")[:16]  # 例: 2026-06-12 00:15
            item = QListWidgetItem(f"{preview}\n{date}")
            item.setData(Qt.ItemDataRole.UserRole, text)
            item.setToolTip(text)
            self._history_list.addItem(item)

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

    def showEvent(self, event) -> None:
        """ウィンドウを開くたびに履歴・実績を最新化する。"""
        super().showEvent(event)
        self._refresh_history()
        self._refresh_stats()

    def _create_api_keys_page(self) -> QWidget:
        """API キーページを作成する（製品版で使う 3 バックエンド分の保存・削除を 1 カードに収める）。"""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(12)
        layout.setContentsMargins(24, 8, 24, 24)

        # service 識別子 → (入力欄, 状態ラベル)
        self._api_key_inputs = {}
        self._api_key_status = {}

        card, cl = _make_card()

        for backend_value in _API_KEY_BACKENDS:
            service = _BACKEND_TO_SERVICE[backend_value]
            # API キー欄はどのキーかが分かる必要があるので提供元名で見出しを出す
            # （このタブ自体が配布版では非表示なので提供元名が露出するのは開発時のみ）
            provider_name = _BACKEND_PROVIDER_NAMES[backend_value]

            block = QWidget()
            block_layout = QVBoxLayout(block)
            block_layout.setContentsMargins(0, 0, 0, 0)
            block_layout.setSpacing(6)

            head = QHBoxLayout()
            head.setSpacing(8)
            name_label = QLabel(provider_name)
            name_label.setStyleSheet("font-weight: 600;")
            status_label = QLabel("未設定")
            status_label.setStyleSheet(MacTheme.status_muted_style())
            head.addWidget(name_label)
            head.addWidget(status_label)
            head.addStretch()

            # 入力中は伏字表示。保存時のみ keyring に書き込み、値は再表示しない
            key_input = QLineEdit()
            key_input.setEchoMode(QLineEdit.EchoMode.Password)
            key_input.setPlaceholderText("API キーを入力")

            save_btn = QPushButton("保存")
            save_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            save_btn.clicked.connect(
                lambda _=False, svc=service: self._save_api_key(svc)
            )

            delete_btn = QPushButton("削除")
            delete_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            delete_btn.clicked.connect(
                lambda _=False, svc=service: self._delete_api_key(svc)
            )

            body = QHBoxLayout()
            body.setSpacing(8)
            body.addWidget(key_input, 1)
            body.addWidget(save_btn)
            body.addWidget(delete_btn)

            block_layout.addLayout(head)
            block_layout.addLayout(body)
            _add_block(cl, block)

            self._api_key_inputs[service] = key_input
            self._api_key_status[service] = status_label

        layout.addWidget(card)
        layout.addWidget(_make_caption(
            "API キーは OS の資格情報ストア（Windows 資格情報マネージャー / macOS キーチェーン）に"
            "安全に保存されます。settings.yaml には書き込まれません。"
        ))
        layout.addStretch()
        return self._wrap_scroll(page)

    def _refresh_api_key_status(self, service: str) -> None:
        """
        指定サービスの API キー保存状況ラベルを更新する。

        キーが登録されているかどうかを表示するだけで、
        実際の値は表示しない（パスワード欄は常に空）。
        """
        status_label = self._api_key_status.get(service)
        if status_label is None:
            return
        if not secrets.is_keyring_available():
            status_label.setText("資格情報ストア利用不可（環境変数で設定してください）")
            status_label.setStyleSheet(MacTheme.status_warn_style(self._is_dark_mode))
            return
        if secrets.get_api_key(service):
            status_label.setText("設定済み")
            status_label.setStyleSheet(MacTheme.status_ok_style(self._is_dark_mode))
        else:
            status_label.setText("未設定")
            status_label.setStyleSheet(MacTheme.status_muted_style())

    def _save_api_key(self, service: str) -> None:
        """
        入力欄の API キーを資格情報ストアに保存する。

        保存後は入力欄をクリアし（再表示しないため）、ステータスを更新する。
        """
        key_input = self._api_key_inputs[service]
        new_key = key_input.text().strip()
        if not new_key:
            QMessageBox.warning(self, "API キー", "API キーを入力してください。")
            return

        if not secrets.set_api_key(service, new_key):
            QMessageBox.critical(
                self,
                "API キー",
                "資格情報ストアへの保存に失敗しました。環境変数 (.env) で設定してください。"
            )
            return

        key_input.clear()
        self._refresh_api_key_status(service)

    def _delete_api_key(self, service: str) -> None:
        """資格情報ストアに保存された API キーを削除する。"""
        secrets.delete_api_key(service)
        self._api_key_inputs[service].clear()
        self._refresh_api_key_status(service)

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
        layout.addWidget(_make_caption(
            "ログインすると、サブスクリプションでサーバー経由の文字起こし"
            "（高速リアルタイム／正確性）が使えます。ログインはブラウザで行います。"
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
        self.refresh_account_status()

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

        if status == LC.LOGGED_IN:
            self._account_status.setText("ログイン済み")
            self._account_status.setStyleSheet(MacTheme.status_ok_style(self._is_dark_mode))
            self._login_btn.setVisible(False)
            self._logout_btn.setVisible(True)
            self._account_poll.stop()
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

        layout.addWidget(_make_caption(
            "新しいバージョンが見つかると「今すぐ更新する」ボタンが表示されます。"
            "更新は起動時と 1 日ごとに自動で確認されます。"
        ))
        layout.addStretch()

        # updater のチェック結果を表示へ反映（ウィンドウを開いたまま検知しても更新される）
        if self._updater is not None:
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
    # 設定の読み込み・保存
    # ------------------------------------------------------------------

    def _load_current_settings(self) -> None:
        """設定ファイルから現在の値をUIに読み込む。"""
        config = self._config_manager.config

        # 一般 - 言語（保存値が選択肢に無くても選択を保持して表示する）
        language = config.get("language", "ja") or ""
        lang_index = self._lang_combo.findData(language)
        if lang_index < 0:
            self._lang_combo.addItem(language, language)
            lang_index = self._lang_combo.count() - 1
        self._lang_combo.setCurrentIndex(lang_index)

        # ホットキー 1 / 2（製品版の既定: 高速リアルタイム=deepgram / 正確性=elevenlabs）
        slot_defaults = {1: ("<f2>", "deepgram"), 2: ("<f3>", "elevenlabs")}
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

            backend = hotkey_config.get("backend", default_backend)
            if backend not in _BACKEND_TO_SERVICE:
                backend = default_backend
            backend_combo = getattr(self, f"_backend{slot_id}_combo")
            backend_combo.setCurrentIndex(max(0, backend_combo.findData(backend)))

            getattr(self, f"_api{slot_id}_prompt_input").setText(
                hotkey_config.get("api_prompt", "")
            )

            # テキスト整形設定
            getattr(self, f"_format{slot_id}_check").setChecked(
                bool(hotkey_config.get("format_enabled", False))
            )

        # 一般 - その他（VAD/分割/HUD/ストリーミング/音量正規化は常時ON固定のため UI 無し）
        # 自動起動はレジストリの実状態を反映（settings.yaml には持たない）
        self._autostart_check.setChecked(autostart.is_enabled())
        self._handsfree_input.setText(config.get("handsfree_key", ""))
        self._auto_enter_delay_slider.setValue(config.get("auto_enter_delay_ms", 50))
        self._populate_input_devices()
        self._set_input_device_selection(config.get("audio_input_device", "default"))

        # 製品版は文字起こしモデル・整形モデル/指示が固定（UI 非公開）のため読み込み不要。

        # API キー保存状況
        for service in _BACKEND_TO_SERVICE.values():
            self._refresh_api_key_status(service)

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

            # ハンズフリー切替キー
            "handsfree_key": self._handsfree_input.text(),
            # vad_filter / vad_min_silence_duration_ms / split_parallel_enabled /
            # streaming_enabled / hud_enabled / audio_preprocess.volume_normalize は
            # 常時 ON 固定（UI 撤去）。ここでは保存せず config_manager 側で True を強制する。
            # format_model / format_auto_prompt も製品版は固定（UI 非公開）なので保存しない
            # （DEFAULT_CONFIG の既定 = llama-3.1-8b-instant / 既定プロンプトが使われる）。

            # ホットキー1 設定
            "hotkey1": self._collect_slot_config(1),

            # ホットキー2 設定
            "hotkey2": self._collect_slot_config(2),

            # APIモデルデフォルト値（全 4 バックエンド）
            "default_api_models": self._config_manager.get("default_api_models", {
                "groq": "whisper-large-v3-turbo",
                "openai": "gpt-4o-mini-transcribe",
                "elevenlabs": "scribe_v1",
                "deepgram": "nova-3",
            }),

            # その他の設定
            "dark_mode": self._is_dark_mode,
            "dev_mode": existing_dev_mode,
            "llm_postprocess": existing_llm_postprocess,
        }

        # 自動起動はレジストリで管理するため settings.yaml とは別に反映する
        if autostart.is_supported():
            autostart.set_enabled(self._autostart_check.isChecked())

        if self._config_manager.save(new_config):
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
        return {
            "hotkey": getattr(self, f"_hotkey{slot_id}_input").text(),
            "hotkey_mode": getattr(self, f"_mode{slot_id}_combo").currentData(),
            "backend": getattr(self, f"_backend{slot_id}_combo").currentData(),
            # 製品版はモデル非選択。空にして default_api_models（deepgram=nova-3 等）へフォールバック
            "api_model": "",
            "api_prompt": getattr(self, f"_api{slot_id}_prompt_input").text(),
            "format_enabled": getattr(self, f"_format{slot_id}_check").isChecked(),
        }
