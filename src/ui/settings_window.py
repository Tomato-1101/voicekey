"""
設定ウィンドウモジュール

Mac 版 SettingsView.swift と同じ 5 タブ構成（一般 / ホットキー 1 / ホットキー 2 / 履歴 / API キー）
・同じ日本語文言で設定を管理する。
ダーク/ライトテーマ切り替えに対応（Qt は OS テーマに自動追従しないため Windows 版のみ）。
"""

from typing import Optional

from PySide6.QtCore import Qt, QPropertyAnimation, QEasingCurve, QPointF, QTimer, Signal
from PySide6.QtGui import QKeyEvent, QPainter, QPainterPath, QColor, QPen, QBrush
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..config import ConfigManager, HotkeyMode, TranscriptionBackend
from ..core.audio_recorder import AudioRecorder
from ..core.history import MAX_ITEMS as HISTORY_MAX_ITEMS, HistoryStore
from ..core.text_formatter import DEFAULT_FORMAT_PROMPT, KNOWN_FORMAT_MODELS
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

# backend の表示順と UI ラベル（Mac 版 Backend.label / allCases と完全一致させる）
_BACKEND_LABELS = [
    (TranscriptionBackend.OPENAI.value, "OpenAI"),
    (TranscriptionBackend.GROQ.value, "Groq"),
    (TranscriptionBackend.ELEVENLABS.value, "ElevenLabs"),
    (TranscriptionBackend.DEEPGRAM.value, "Deepgram"),
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

# backend 選択値 → 選択可能モデル（先頭がベンチ実測 2026-06-10 に基づく既定）
_BACKEND_MODELS = {
    TranscriptionBackend.OPENAI.value: [
        "gpt-4o-mini-transcribe",
        "gpt-4o-transcribe",
    ],
    TranscriptionBackend.GROQ.value: [
        "whisper-large-v3-turbo",
        "whisper-large-v3",
        "distil-whisper-large-v3-en",
    ],
    TranscriptionBackend.ELEVENLABS.value: [
        "scribe_v1",                # 日本語 REST 最高精度（既定）
        "scribe_v1_experimental",
        "scribe_v2",                # 短文0%だが長文は後退
    ],
    TranscriptionBackend.DEEPGRAM.value: [
        "nova-3",                   # ストリーミング/REST とも最良（既定）
        "nova-2",
    ],
}

# 各 backend の既定モデル（保存済みモデルが当該 backend のものでない時のフォールバック）
_BACKEND_DEFAULT_MODEL = {b: models[0] for b, models in _BACKEND_MODELS.items()}


def _model_label(model: str, recommended: str) -> str:
    """モデル選択 Combo の表示ラベルを作る（推奨モデルに「（推奨）」を付ける）。

    表示にだけ使い、保存値は userData のモデル識別子のまま（API へはラベルを送らない）。
    """
    return f"{model}（推奨）" if model == recommended else model


def _make_caption(text: str) -> QLabel:
    """設定項目の補足説明ラベルを作る（Mac 版の .caption 相当）。"""
    label = QLabel(text)
    label.setWordWrap(True)
    label.setStyleSheet(MacTheme.caption_style())
    return label



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

    angle = property(get_angle, set_angle)

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
        """フォーカス取得時に録音状態をリセット。"""
        super().focusInEvent(event)
        self._pressed_keys = []
        self._is_recording = True

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

    def __init__(
        self,
        platform_adapter: Optional[PlatformAdapter] = None,
        history: Optional[HistoryStore] = None,
    ) -> None:
        """
        設定ウィンドウを初期化する。

        Args:
            platform_adapter: プラットフォーム依存処理のアダプタ
            history: 音声入力履歴ストア（「履歴」タブで表示・再コピーする）
        """
        super().__init__()
        self._platform = platform_adapter or get_platform_adapter()
        self._history = history

        # DIST ビルドでは API キータブを作らないため、タブ生成前に空で初期化しておく
        # （_load_current_settings が無条件に _refresh_api_key_status を呼ぶ）
        self._api_key_inputs = {}
        self._api_key_status = {}

        self._config_manager = ConfigManager()

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
        self.resize(560, 640)

    def _setup_ui(self) -> None:
        """UIコンポーネントを設定する。"""
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(20, 16, 20, 16)
        main_layout.setSpacing(12)

        # ヘッダー（タイトル + テーマ切替）
        header_layout = QHBoxLayout()
        header_layout.setSpacing(10)

        title = QLabel("voicekey 設定")
        title.setStyleSheet(MacTheme.title_style())

        # テーマ切替ボタン（Qt は OS テーマに追従しないため Windows 版独自）
        self._theme_toggle = ThemeToggleButton(is_dark=self._is_dark_mode)
        self._theme_toggle.clicked.connect(self._toggle_theme)

        header_layout.addWidget(title)
        header_layout.addStretch()
        header_layout.addWidget(self._theme_toggle)
        main_layout.addLayout(header_layout)

        # 5 タブ（Mac 版 SettingsView の TabView と同じ構成・順序）
        self._tabs = QTabWidget()
        self._tabs.addTab(self._create_general_tab(), "一般")
        self._tabs.addTab(self._create_slot_tab(1), "ホットキー 1")
        self._tabs.addTab(self._create_slot_tab(2), "ホットキー 2")
        self._history_tab_index = self._tabs.addTab(self._create_history_tab(), "履歴")
        # 配布ビルドは埋め込みキーで動くため、API キータブは出さない（テスターの混乱防止）
        if not secrets.is_dist_build():
            self._tabs.addTab(self._create_api_keys_tab(), "API キー")
        # ウィンドウを開いたまま音声入力しても、履歴タブを開いた時点で最新になるように
        self._tabs.currentChanged.connect(self._on_tab_changed)
        main_layout.addWidget(self._tabs, 1)

        # ボタンエリア（保存/キャンセル）。Mac 版は即時反映だが、Windows 版は
        # settings.yaml + ホットリロードのため明示保存とする
        button_layout = QHBoxLayout()
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
        main_layout.addLayout(button_layout)

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
    # 一般タブ（Mac 版 GeneralSettingsTab と同項目 + Windows 版固有項目）
    # ------------------------------------------------------------------

    def _create_general_tab(self) -> QWidget:
        """一般タブを作成する。"""
        page = QWidget()
        layout = QFormLayout(page)
        layout.setSpacing(10)
        layout.setContentsMargins(16, 16, 16, 16)

        # 言語（Mac 版と同じ 3 択。空文字 = API 側の自動判定）
        self._lang_combo = QComboBox()
        for label, value in _LANGUAGE_OPTIONS:
            self._lang_combo.addItem(label, value)
        layout.addRow("言語:", self._lang_combo)

        # 入力デバイス
        self._input_device_combo = QComboBox()
        # 280px 固定だと「更新」「自動検出」ボタンと合わせて窓幅 560px を超え
        # 横スクロールバーが出るため、最小 200px + 伸縮（余白があれば広がる）にする
        self._input_device_combo.setMinimumWidth(200)

        refresh_button = QPushButton("更新")
        refresh_button.setCursor(Qt.CursorShape.PointingHandCursor)
        refresh_button.setToolTip("デバイス一覧を更新")
        refresh_button.clicked.connect(self._populate_input_devices)

        # マイク自動検出（Mac 版と同機能）: 押した後に喋ると声が入ったマイクを自動選択する
        self._mic_detect_button = QPushButton("自動検出")
        self._mic_detect_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._mic_detect_button.setToolTip("全マイクを監視し、喋った声が入ったマイクを自動選択します")
        self._mic_detect_button.clicked.connect(self._start_mic_auto_detect)

        device_row = QWidget()
        device_row_layout = QHBoxLayout(device_row)
        device_row_layout.setContentsMargins(0, 0, 0, 0)
        device_row_layout.setSpacing(8)
        device_row_layout.addWidget(self._input_device_combo, 1)
        device_row_layout.addWidget(refresh_button)
        device_row_layout.addWidget(self._mic_detect_button)
        layout.addRow("入力デバイス:", device_row)
        # 検出中・検出結果の表示（待ち時間を可視化する。無表示の待ちはバグと区別できない）
        self._mic_detect_status = _make_caption("")
        self._mic_detect_status.setVisible(False)
        layout.addRow("", self._mic_detect_status)
        layout.addRow("", _make_caption("録音に使うマイク。「システム既定」は OS の設定に従います。"))
        self._mic_detect_done.connect(self._on_mic_detect_done)

        # VAD（無音スキップ）
        self._vad_check = QCheckBox("無音を自動スキップ（VAD）")
        layout.addRow("", self._vad_check)
        layout.addRow("", _make_caption("発話が検出されない録音を API に送らず、幻覚と無駄なコストを防ぎます。"))

        # VAD 最小無音時間（Windows 版固有の調整項目）
        self._vad_silence_spin = QSpinBox()
        self._vad_silence_spin.setRange(100, 5000)
        self._vad_silence_spin.setSingleStep(50)
        self._vad_silence_spin.setSuffix(" ms")
        layout.addRow("VAD 最小無音時間:", self._vad_silence_spin)

        # 録音中 HUD（画面下部中央の小型ピル）
        self._hud_check = QCheckBox("録音中に HUD を表示")
        layout.addRow("", self._hud_check)

        # リアルタイムストリーミング（Deepgram のホットキーで有効）
        self._streaming_check = QCheckBox("リアルタイムストリーミング（Deepgram）")
        layout.addRow("", self._streaming_check)
        layout.addRow("", _make_caption(
            "バックエンドが Deepgram のホットキーで、話しながら HUD に文字を表示し、"
            "離した瞬間に確定します。オフにすると従来どおり録音後にまとめて変換します。"
        ))

        # Auto Enter 遅延（ダブルタップ時、テキスト挿入後のEnter押下までの待機時間）
        # 一部アプリが即時Enterに反応しないため、ユーザー側で調整可能にする
        self._auto_enter_delay_slider = QSlider(Qt.Orientation.Horizontal)
        self._auto_enter_delay_slider.setRange(0, 500)
        self._auto_enter_delay_slider.setSingleStep(10)
        self._auto_enter_delay_slider.setPageStep(50)
        self._auto_enter_delay_slider.setMinimumWidth(220)

        self._auto_enter_delay_label = QLabel("50 ms")
        self._auto_enter_delay_label.setMinimumWidth(56)
        self._auto_enter_delay_slider.valueChanged.connect(
            lambda v: self._auto_enter_delay_label.setText(f"{v} ms")
        )

        delay_row = QWidget()
        delay_row_layout = QHBoxLayout(delay_row)
        delay_row_layout.setContentsMargins(0, 0, 0, 0)
        delay_row_layout.setSpacing(8)
        delay_row_layout.addWidget(self._auto_enter_delay_slider)
        delay_row_layout.addWidget(self._auto_enter_delay_label)
        layout.addRow("自動 Enter の遅延:", delay_row)
        layout.addRow("", _make_caption(
            "ホットキーを素早く 2 回押すと、貼り付け後に Enter を自動送信します（チャット送信用）。"
        ))

        # 音声前処理（API送信前）：Peak+RMS ハイブリッド音量正規化（Windows 版固有）
        # ノイズ対策は API モデル側に任せ、ここでは小音量を持ち上げて音割れを防ぐのみ
        self._volume_normalize_check = QCheckBox("音量正規化（Peak+RMS）")
        layout.addRow("", self._volume_normalize_check)
        layout.addRow("", _make_caption("小さい声を底上げし、音割れしない範囲でゲイン調整します。"))

        # LLM テキスト整形に使う Groq モデル（両ホットキー共通・リストから選択）
        # 表示は「（推奨）」付きラベル、値は userData のモデル識別子
        self._format_model_combo = QComboBox()
        for model in KNOWN_FORMAT_MODELS:
            self._format_model_combo.addItem(
                _model_label(model, KNOWN_FORMAT_MODELS[0]), model
            )
        layout.addRow("整形モデル:", self._format_model_combo)
        layout.addRow("", _make_caption(
            "テキスト整形に使う Groq のモデル。速度重視なら既定（llama-3.1-8b-instant）を推奨。"
        ))

        # テキスト整形で LLM に渡すプロンプト（編集可・空欄なら既定）
        auto_prompt_header = QWidget()
        auto_prompt_header_layout = QHBoxLayout(auto_prompt_header)
        auto_prompt_header_layout.setContentsMargins(0, 0, 0, 0)
        auto_prompt_header_layout.addWidget(QLabel("整形の指示:"))
        auto_prompt_header_layout.addStretch()

        auto_prompt_reset = QPushButton("既定に戻す")
        auto_prompt_reset.setCursor(Qt.CursorShape.PointingHandCursor)
        auto_prompt_reset.clicked.connect(
            lambda: self._format_auto_prompt_edit.setPlainText(DEFAULT_FORMAT_PROMPT)
        )
        auto_prompt_header_layout.addWidget(auto_prompt_reset)
        layout.addRow(auto_prompt_header)

        self._format_auto_prompt_edit = QPlainTextEdit()
        self._format_auto_prompt_edit.setFixedHeight(110)
        layout.addRow(self._format_auto_prompt_edit)
        layout.addRow("", _make_caption(
            "テキスト整形で LLM に渡す指示。"
            "自由に編集できます（空欄なら既定の指示を使用）。"
        ))

        # ログイン時に自動起動（Windows のみ。Mac ネイティブ版は SMAppService で対応）。
        # 状態はレジストリ側が真実なので settings.yaml には保存しない
        self._autostart_check = QCheckBox("ログイン時に起動")
        self._autostart_check.setEnabled(autostart.is_supported())
        layout.addRow("", self._autostart_check)
        if not autostart.is_supported():
            layout.addRow("", _make_caption("この機能は Windows でのみ利用できます。"))

        return self._wrap_scroll(page)

    # ------------------------------------------------------------------
    # ホットキータブ（Mac 版 SlotSettingsTab と同項目・同文言）
    # ------------------------------------------------------------------

    def _create_slot_tab(self, slot_id: int) -> QWidget:
        """
        ホットキースロットのタブページを作成する。

        Args:
            slot_id: スロットID（1または2）

        Returns:
            ホットキー設定のタブページ
        """
        page = QWidget()
        layout = QFormLayout(page)
        layout.setSpacing(10)
        layout.setContentsMargins(16, 16, 16, 16)

        # ホットキー入力
        hotkey_input = HotkeyInput(platform_adapter=self._platform)
        setattr(self, f"_hotkey{slot_id}_input", hotkey_input)
        layout.addRow("ホットキー:", hotkey_input)

        # 動作モード（表示は日本語ラベル、保存値は userData の識別子）
        mode_combo = QComboBox()
        for value, label in _MODE_LABELS:
            mode_combo.addItem(label, value)
        setattr(self, f"_mode{slot_id}_combo", mode_combo)
        layout.addRow("動作:", mode_combo)

        # バックエンド選択（REST + ストリーミングの全 4 種）
        backend_combo = QComboBox()
        for value, label in _BACKEND_LABELS:
            backend_combo.addItem(label, value)
        backend_combo.currentIndexChanged.connect(
            lambda _=0, sid=slot_id: self._on_slot_backend_changed(sid)
        )
        setattr(self, f"_backend{slot_id}_combo", backend_combo)
        layout.addRow("バックエンド:", backend_combo)

        # モデル選択（バックエンド変更で候補を差し替える）
        model_combo = QComboBox()
        setattr(self, f"_api{slot_id}_model_combo", model_combo)
        layout.addRow("モデル:", model_combo)

        # プロンプト入力（文字起こしのヒント）
        prompt_input = QLineEdit()
        prompt_input.setPlaceholderText("専門用語や固有名詞のヒントを入力")
        setattr(self, f"_api{slot_id}_prompt_input", prompt_input)
        layout.addRow("プロンプト（任意）:", prompt_input)
        layout.addRow("", _make_caption(
            "文字起こしのヒント。よく使う固有名詞を書いておくと精度が上がります。"
        ))

        # テキスト整形（LLM）: 貼り付け前に Groq の高速 LLM で 1 回整形する
        # （整形内容は LLM が自動判断。指示は「一般」タブで編集可。Mac 版と文言を一致させる）
        format_check = QCheckBox("テキスト整形（LLM）")
        setattr(self, f"_format{slot_id}_check", format_check)
        layout.addRow("", format_check)
        layout.addRow("", _make_caption(
            "文字起こし後に Groq の高速 LLM で整形してから貼り付けます。"
            "内容に応じた整形を LLM が自動判断します（指示は「一般」タブで編集可）。"
            "オフなら文字起こしをそのまま貼り付けます。"
        ))

        return self._wrap_scroll(page)

    # ------------------------------------------------------------------
    # API キータブ（Mac 版 ApiKeysTab と同構成）
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # 履歴タブ（Mac 版 HistoryTab と同構成。クリックでクリップボードにコピー）
    # ------------------------------------------------------------------

    def _create_history_tab(self) -> QWidget:
        """履歴タブを作成する（直近の音声入力をクリックで再コピー）。"""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(10)
        layout.setContentsMargins(16, 16, 16, 16)

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

    def _on_tab_changed(self, index: int) -> None:
        """履歴タブが選択されたら一覧を最新の内容に更新する。"""
        if index == self._history_tab_index:
            self._refresh_history()

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
        """ウィンドウを開くたびに履歴一覧を最新化する。"""
        super().showEvent(event)
        self._refresh_history()

    def _create_api_keys_tab(self) -> QWidget:
        """API キータブを作成する（4 バックエンド分の保存・削除）。"""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(16)
        layout.setContentsMargins(16, 16, 16, 16)

        # service 識別子 → (入力欄, 状態ラベル)
        self._api_key_inputs = {}
        self._api_key_status = {}

        for backend_value, label in _BACKEND_LABELS:
            service = _BACKEND_TO_SERVICE[backend_value]

            head = QHBoxLayout()
            head.setSpacing(8)
            name_label = QLabel(label)
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

            row = QVBoxLayout()
            row.setSpacing(6)
            row.addLayout(head)
            row.addLayout(body)
            layout.addLayout(row)

            self._api_key_inputs[service] = key_input
            self._api_key_status[service] = status_label

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
        self._mic_detect_status.setText("自動検出中… マイクに向かって喋ってください")
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

        # ホットキー 1 / 2
        slot_defaults = {1: ("<f2>", "openai"), 2: ("<f3>", "groq")}
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

        # 一般 - その他
        self._streaming_check.setChecked(config.get("streaming_enabled", True))
        self._hud_check.setChecked(config.get("hud_enabled", True))
        # 自動起動はレジストリの実状態を反映（settings.yaml には持たない）
        self._autostart_check.setChecked(autostart.is_enabled())
        self._vad_check.setChecked(config.get("vad_filter", True))
        self._vad_silence_spin.setValue(config.get("vad_min_silence_duration_ms", 500))
        self._auto_enter_delay_slider.setValue(config.get("auto_enter_delay_ms", 50))
        self._populate_input_devices()
        self._set_input_device_selection(config.get("audio_input_device", "default"))

        # 音声前処理（音量正規化のみ）
        preprocess_cfg = config.get("audio_preprocess", {}) or {}
        self._volume_normalize_check.setChecked(bool(preprocess_cfg.get("volume_normalize", True)))

        # LLM テキスト整形モデル（両ホットキー共通）。保存値がリスト外でも選択を保持して表示する
        saved_model = config.get("format_model", "llama-3.1-8b-instant")
        if self._format_model_combo.findData(saved_model) < 0:
            self._format_model_combo.addItem(saved_model, saved_model)
        self._format_model_combo.setCurrentIndex(self._format_model_combo.findData(saved_model))

        # 整形プロンプト（空 = 既定。編集できるよう既定の実テキストを表示する）
        self._format_auto_prompt_edit.setPlainText(
            config.get("format_auto_prompt", "") or DEFAULT_FORMAT_PROMPT
        )

        # APIモデル候補を初期化（setCurrentIndex がシグナルを発しないケースに備えて明示実行）
        for slot_id in (1, 2):
            self._on_slot_backend_changed(slot_id)

        # API キー保存状況
        for service in _BACKEND_TO_SERVICE.values():
            self._refresh_api_key_status(service)

    def _on_slot_backend_changed(self, slot_id: int) -> None:
        """
        スロットのバックエンド選択変更を処理する。

        Args:
            slot_id: スロットID（1または2）
        """
        backend_combo = getattr(self, f"_backend{slot_id}_combo")
        backend = backend_combo.currentData()

        # モデル候補を差し替える。保存済みモデルが当該 backend のものなら復元、
        # そうでなければ先頭（既定）を選ぶ
        # 表示は「（推奨）」付きラベル、値は userData のモデル識別子
        model_combo = getattr(self, f"_api{slot_id}_model_combo")
        model_combo.clear()
        models = _BACKEND_MODELS.get(backend, [])
        recommended = _BACKEND_DEFAULT_MODEL.get(backend, "")
        for model in models:
            model_combo.addItem(_model_label(model, recommended), model)

        config = self._config_manager.config
        hotkey_config = config.get(f"hotkey{slot_id}", {}) or {}
        saved_model = hotkey_config.get("api_model", "")
        if saved_model in models:
            model_combo.setCurrentIndex(models.index(saved_model))
        elif models:
            model_combo.setCurrentIndex(0)

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
            "vad_filter": self._vad_check.isChecked(),
            "vad_min_silence_duration_ms": self._vad_silence_spin.value(),
            "audio_input_device": selected_input_device,
            "auto_enter_delay_ms": self._auto_enter_delay_slider.value(),

            # リアルタイムストリーミング / HUD 表示
            "streaming_enabled": self._streaming_check.isChecked(),
            "hud_enabled": self._hud_check.isChecked(),

            # 音声前処理（音量正規化のみ）
            "audio_preprocess": {
                "volume_normalize": self._volume_normalize_check.isChecked(),
            },

            # LLM テキスト整形に使う Groq モデル（両ホットキー共通。値は userData の識別子）
            "format_model": self._format_model_combo.currentData() or "llama-3.1-8b-instant",
            # 整形プロンプト（既定文と同一なら空で保存し、既定文の将来更新に追従する）
            "format_auto_prompt": (
                "" if self._format_auto_prompt_edit.toPlainText().strip() == DEFAULT_FORMAT_PROMPT.strip()
                else self._format_auto_prompt_edit.toPlainText()
            ),

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
            "api_model": getattr(self, f"_api{slot_id}_model_combo").currentData() or "",
            "api_prompt": getattr(self, f"_api{slot_id}_prompt_input").text(),
            "format_enabled": getattr(self, f"_format{slot_id}_check").isChecked(),
        }
