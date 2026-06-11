"""
設定ウィンドウモジュール

macOSシステム設定風のUIでアプリケーション設定を管理する。
ホットキーとAPI設定を提供。
ダーク/ライトテーマ切り替えに対応。
"""

from typing import Optional

from PySide6.QtCore import Qt, QPropertyAnimation, QEasingCurve, QPointF
from PySide6.QtGui import QKeyEvent, QPainter, QPainterPath, QColor, QPen, QBrush
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSlider,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ..config import ConfigManager, HotkeyMode, TranscriptionBackend
from ..core.audio_recorder import AudioRecorder
from ..core.text_formatter import DEFAULT_AUTO_PROMPT, KNOWN_FORMAT_MODELS
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

# LLM テキスト整形モード（識別子, UI 日本語ラベル）。Mac 版と文言を完全一致させる
_FORMAT_MODES = [
    ("auto", "おまかせ（自動判断）"),
    ("clean", "自動クリーン"),
    ("bullets", "箇条書き"),
    ("polite", "丁寧（敬語）"),
    ("casual", "カジュアル"),
    ("email", "メール調"),
    ("custom", "カスタム"),
]


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
        self.setPlaceholderText("Click to record shortcut...")
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
    
    macOSシステム設定風のサイドバーナビゲーションUIを提供し、
    General/Advancedの2つのページで設定を管理する。
    """

    def __init__(self, platform_adapter: Optional[PlatformAdapter] = None) -> None:
        """設定ウィンドウを初期化する。"""
        super().__init__()
        self._platform = platform_adapter or get_platform_adapter()

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
        self.setWindowTitle("Settings")
        self.resize(720, 480)

    def _setup_ui(self) -> None:
        """UIコンポーネントを設定する。"""
        main_layout = QHBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # --- サイドバー ---
        self._sidebar = QListWidget()
        self._sidebar.setFixedWidth(200)
        self._sidebar.setAttribute(Qt.WidgetAttribute.WA_MacShowFocusRect, False)
        self._sidebar.setFrameShape(QFrame.Shape.NoFrame)
        self._sidebar.currentRowChanged.connect(self._change_page)
        main_layout.addWidget(self._sidebar)

        # --- コンテンツエリア ---
        content_container = QWidget()
        content_layout = QVBoxLayout(content_container)
        content_layout.setContentsMargins(40, 30, 40, 30)
        content_layout.setSpacing(20)

        # ヘッダー（タイトル + テーマ切替）
        header_layout = QHBoxLayout()
        header_layout.setSpacing(10)
        
        self._page_title = QLabel("General")
        self._page_title.setStyleSheet("font-size: 24px; font-weight: bold; margin-bottom: 5px;")
        
        # テーマ切替ボタン
        self._theme_toggle = ThemeToggleButton(is_dark=self._is_dark_mode)
        self._theme_toggle.clicked.connect(self._toggle_theme)
        
        header_layout.addWidget(self._page_title)
        header_layout.addStretch()
        header_layout.addWidget(self._theme_toggle)
        
        content_layout.addLayout(header_layout)

        # ページスタックウィジェット
        self._pages_stack = QStackedWidget()
        content_layout.addWidget(self._pages_stack)
        
        # ボタンエリア（保存/キャンセル）
        button_layout = QHBoxLayout()
        button_layout.addStretch()
        
        self._cancel_btn = QPushButton("Cancel")
        self._cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._cancel_btn.clicked.connect(self.close)
        
        self._save_btn = QPushButton("Save Settings")
        self._save_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._save_btn.setProperty("class", "primary")  # プライマリボタンスタイル
        self._save_btn.clicked.connect(self._save_settings)
        
        button_layout.addWidget(self._cancel_btn)
        button_layout.addWidget(self._save_btn)
        
        content_layout.addLayout(button_layout)
        
        main_layout.addWidget(content_container)

        # ページを追加
        self._setup_pages()

    def _setup_pages(self) -> None:
        """各設定ページを作成してスタックに追加する。"""
        self._add_page("General", self._create_general_page())
        self._add_page("Advanced", self._create_advanced_page())

        # 最初のページを選択
        self._sidebar.setCurrentRow(0)

    def _add_page(self, name: str, widget: QWidget) -> None:
        """
        サイドバーとスタックにページを追加する。
        
        Args:
            name: ページ名
            widget: ページウィジェット
        """
        item = QListWidgetItem(name)
        self._sidebar.addItem(item)
        self._pages_stack.addWidget(widget)

    def _change_page(self, index: int) -> None:
        """
        ページ切替を処理する。
        
        Args:
            index: 選択されたページのインデックス
        """
        self._pages_stack.setCurrentIndex(index)
        item = self._sidebar.item(index)
        if item:
            self._page_title.setText(item.text())

    def _create_general_page(self) -> QWidget:
        """Generalページを作成する（2ホットキー対応）。"""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(20)

        # 2つのホットキー設定を横並びで配置
        hotkeys_layout = QHBoxLayout()
        hotkeys_layout.setSpacing(30)

        # ホットキー1
        hotkey1_group = self._create_hotkey_group(1)
        hotkeys_layout.addWidget(hotkey1_group)

        # ホットキー2
        hotkey2_group = self._create_hotkey_group(2)
        hotkeys_layout.addWidget(hotkey2_group)

        layout.addLayout(hotkeys_layout)

        # 共通設定
        common_layout = QFormLayout()
        self._lang_input = QLineEdit()
        self._lang_input.setPlaceholderText("e.g. ja, en")
        common_layout.addRow("Language (共通):", self._lang_input)
        layout.addLayout(common_layout)

        layout.addStretch()
        return page

    def _create_hotkey_group(self, slot_id: int) -> QGroupBox:
        """
        ホットキースロットのUIグループを作成する。

        Args:
            slot_id: スロットID（1または2）

        Returns:
            ホットキー設定のグループボックス
        """
        group = QGroupBox(f"Hotkey {slot_id}")
        layout = QFormLayout(group)
        layout.setSpacing(12)

        # ホットキー入力
        hotkey_input = HotkeyInput(platform_adapter=self._platform)
        setattr(self, f"_hotkey{slot_id}_input", hotkey_input)
        layout.addRow("Shortcut:", hotkey_input)

        # モード選択
        mode_combo = QComboBox()
        mode_combo.addItems([m.value for m in HotkeyMode])
        setattr(self, f"_mode{slot_id}_combo", mode_combo)
        layout.addRow("Mode:", mode_combo)

        # バックエンド選択（REST + ストリーミングの全 4 種）
        backend_combo = QComboBox()
        backend_combo.addItems([
            TranscriptionBackend.OPENAI.value,
            TranscriptionBackend.GROQ.value,
            TranscriptionBackend.ELEVENLABS.value,
            TranscriptionBackend.DEEPGRAM.value,
        ])
        backend_combo.currentTextChanged.connect(
            lambda text, sid=slot_id: self._on_slot_backend_changed(sid, text)
        )
        setattr(self, f"_backend{slot_id}_combo", backend_combo)
        layout.addRow("Backend:", backend_combo)

        # API設定（動的表示）
        api_widget = self._create_api_settings_widget(slot_id)
        setattr(self, f"_api{slot_id}_widget", api_widget)
        layout.addRow("", api_widget)

        # テキスト整形（LLM）: 貼り付け前に Groq の高速 LLM で 1 回整形する
        format_check = QCheckBox("テキスト整形（LLM）")
        format_check.toggled.connect(
            lambda _=False, sid=slot_id: self._update_format_controls(sid)
        )
        setattr(self, f"_format{slot_id}_check", format_check)
        layout.addRow("Format:", format_check)

        # 整形モード選択（userData にモード識別子を保持する）
        format_mode_combo = QComboBox()
        for mode, label in _FORMAT_MODES:
            format_mode_combo.addItem(label, mode)
        format_mode_combo.currentIndexChanged.connect(
            lambda _=0, sid=slot_id: self._update_format_controls(sid)
        )
        setattr(self, f"_format{slot_id}_mode_combo", format_mode_combo)
        layout.addRow("", format_mode_combo)

        # カスタムプロンプト（モードが custom のときだけ表示）
        format_custom_input = QLineEdit()
        format_custom_input.setPlaceholderText("カスタムプロンプト")
        format_custom_input.setVisible(False)
        setattr(self, f"_format{slot_id}_custom_input", format_custom_input)
        layout.addRow("", format_custom_input)

        return group

    def _update_format_controls(self, slot_id: int) -> None:
        """
        テキスト整形コントロールの表示・有効状態を更新する。

        チェックボックスの状態でモード選択を有効化し、
        モードが custom のときだけカスタムプロンプト欄を表示する。

        Args:
            slot_id: スロットID（1または2）
        """
        enabled = getattr(self, f"_format{slot_id}_check").isChecked()
        mode_combo = getattr(self, f"_format{slot_id}_mode_combo")
        custom_input = getattr(self, f"_format{slot_id}_custom_input")

        mode_combo.setEnabled(enabled)
        is_custom = mode_combo.currentData() == "custom"
        custom_input.setVisible(is_custom)
        custom_input.setEnabled(enabled and is_custom)

    def _create_api_settings_widget(self, slot_id: int) -> QWidget:
        """
        APIバックエンド用の設定ウィジェットを作成する。

        Args:
            slot_id: スロットID

        Returns:
            API設定ウィジェット
        """
        widget = QWidget()
        layout = QFormLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        # モデル選択（Groq/OpenAI共通）
        model_combo = QComboBox()
        setattr(self, f"_api{slot_id}_model_combo", model_combo)
        layout.addRow("Model:", model_combo)

        # プロンプト入力
        prompt_input = QLineEdit()
        prompt_input.setPlaceholderText("Optional: hint text")
        setattr(self, f"_api{slot_id}_prompt_input", prompt_input)
        layout.addRow("Prompt:", prompt_input)

        # API キー入力（Keychain 保存。settings.yaml には書かない）
        # 入力中は伏字表示。Save 押下時のみ keyring に書き込み、Clear で削除。
        key_input = QLineEdit()
        key_input.setEchoMode(QLineEdit.EchoMode.Password)
        key_input.setPlaceholderText("(Saved in macOS Keychain / Windows Credential Manager)")
        setattr(self, f"_api{slot_id}_key_input", key_input)
        layout.addRow("API Key:", key_input)

        # 保存状況の表示と保存/削除ボタン
        status_label = QLabel("Not set")
        status_label.setStyleSheet("color: #888; font-size: 11px;")
        setattr(self, f"_api{slot_id}_key_status", status_label)

        save_btn = QPushButton("Save Key")
        save_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        save_btn.clicked.connect(lambda _=False, sid=slot_id: self._save_api_key(sid))

        clear_btn = QPushButton("Clear")
        clear_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        clear_btn.clicked.connect(lambda _=False, sid=slot_id: self._clear_api_key(sid))

        key_actions = QWidget()
        key_actions_layout = QHBoxLayout(key_actions)
        key_actions_layout.setContentsMargins(0, 0, 0, 0)
        key_actions_layout.setSpacing(8)
        key_actions_layout.addWidget(status_label)
        key_actions_layout.addStretch()
        key_actions_layout.addWidget(save_btn)
        key_actions_layout.addWidget(clear_btn)
        layout.addRow("", key_actions)

        widget.setVisible(False)  # 初期状態は非表示
        return widget

    def _refresh_api_key_status(self, slot_id: int) -> None:
        """
        指定スロットの API キー保存状況ラベルを更新する。

        Keychain にキーが登録されているかどうかを表示するだけで、
        実際の値は表示しない（パスワード欄は常に空）。
        """
        backend_combo = getattr(self, f"_backend{slot_id}_combo", None)
        status_label = getattr(self, f"_api{slot_id}_key_status", None)
        if backend_combo is None or status_label is None:
            return
        service = _BACKEND_TO_SERVICE.get(backend_combo.currentText())
        if service is None:
            status_label.setText("")
            return
        if not secrets.is_keyring_available():
            status_label.setText("Keychain unavailable (env var fallback)")
            return
        existing = secrets.get_api_key(service)
        status_label.setText("Saved in Keychain" if existing else "Not set")

    def _save_api_key(self, slot_id: int) -> None:
        """
        入力欄の API キーを Keychain に保存する。

        backend がローカル等で対応サービスがない場合は no-op。
        保存後は入力欄をクリアし（再表示しないため）、ステータスを更新する。
        """
        backend_combo = getattr(self, f"_backend{slot_id}_combo")
        key_input = getattr(self, f"_api{slot_id}_key_input")
        service = _BACKEND_TO_SERVICE.get(backend_combo.currentText())
        if service is None:
            return

        new_key = key_input.text().strip()
        if not new_key:
            QMessageBox.warning(self, "API Key", "API キーを入力してください。")
            return

        if not secrets.set_api_key(service, new_key):
            QMessageBox.critical(
                self,
                "API Key",
                "Keychain への保存に失敗しました。環境変数 (.env) で設定してください。"
            )
            return

        key_input.clear()
        # 同じバックエンドを使っている他スロットのステータスも更新する
        for sid in (1, 2):
            self._refresh_api_key_status(sid)

    def _clear_api_key(self, slot_id: int) -> None:
        """Keychain に保存された API キーを削除する。"""
        backend_combo = getattr(self, f"_backend{slot_id}_combo")
        key_input = getattr(self, f"_api{slot_id}_key_input")
        service = _BACKEND_TO_SERVICE.get(backend_combo.currentText())
        if service is None:
            return

        secrets.delete_api_key(service)
        key_input.clear()
        for sid in (1, 2):
            self._refresh_api_key_status(sid)

    def _create_advanced_page(self) -> QWidget:
        """Advancedページを作成する。"""
        page = QWidget()
        layout = QFormLayout(page)
        layout.setSpacing(15)

        # リアルタイムストリーミング（Deepgram のホットキーで有効）
        self._streaming_check = QCheckBox("リアルタイムストリーミング（Deepgram）")
        layout.addRow("Streaming:", self._streaming_check)
        streaming_hint = QLabel(
            "Deepgram のホットキーで、話しながら HUD に字幕を表示し離した瞬間に確定します"
        )
        streaming_hint.setStyleSheet("color: #888; font-size: 11px;")
        streaming_hint.setWordWrap(True)
        layout.addRow("", streaming_hint)

        # 録音中 HUD（画面下部中央の小型ピル）
        self._hud_check = QCheckBox("録音中の HUD を表示")
        layout.addRow("HUD:", self._hud_check)

        # ログイン時に自動起動（Windows のみ。Mac ネイティブ版は SMAppService で対応）。
        # 状態はレジストリ側が真実なので settings.yaml には保存しない
        self._autostart_check = QCheckBox("ログイン時に起動")
        self._autostart_check.setEnabled(autostart.is_supported())
        layout.addRow("Startup:", self._autostart_check)
        if not autostart.is_supported():
            autostart_hint = QLabel("この機能は Windows でのみ利用できます")
            autostart_hint.setStyleSheet("color: #888; font-size: 11px;")
            layout.addRow("", autostart_hint)

        # VAD設定
        self._vad_check = QCheckBox("Enable VAD")
        layout.addRow("", self._vad_check)

        # VAD最小無音時間
        self._vad_silence_spin = QSpinBox()
        self._vad_silence_spin.setRange(100, 5000)
        self._vad_silence_spin.setSingleStep(50)
        self._vad_silence_spin.setSuffix(" ms")
        layout.addRow("VAD Min Silence:", self._vad_silence_spin)

        # Auto Enter 遅延（ダブルタップ時、テキスト挿入後のEnter押下までの待機時間）
        # 一部アプリが即時Enterに反応しないため、ユーザー側で調整可能にする
        self._auto_enter_delay_slider = QSlider(Qt.Orientation.Horizontal)
        self._auto_enter_delay_slider.setRange(0, 500)
        self._auto_enter_delay_slider.setSingleStep(10)
        self._auto_enter_delay_slider.setPageStep(50)
        self._auto_enter_delay_slider.setMinimumWidth(240)

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
        layout.addRow("Auto Enter Delay:", delay_row)

        # 入力デバイス
        self._input_device_combo = QComboBox()
        self._input_device_combo.setMinimumWidth(320)

        refresh_button = QPushButton("Refresh")
        refresh_button.setCursor(Qt.CursorShape.PointingHandCursor)
        refresh_button.clicked.connect(self._populate_input_devices)

        device_row = QWidget()
        device_row_layout = QHBoxLayout(device_row)
        device_row_layout.setContentsMargins(0, 0, 0, 0)
        device_row_layout.setSpacing(8)
        device_row_layout.addWidget(self._input_device_combo)
        device_row_layout.addWidget(refresh_button)

        layout.addRow("Input Device:", device_row)

        # 音声前処理（API送信前）：Peak+RMS ハイブリッド音量正規化
        # ノイズ対策は API モデル側に任せ、ここでは小音量を持ち上げて音割れを防ぐのみ
        self._volume_normalize_check = QCheckBox("Volume Normalization (Peak+RMS)")
        layout.addRow("Audio Preprocess:", self._volume_normalize_check)

        preprocess_hint = QLabel("小さい声を底上げし、音割れしない範囲でゲイン調整します")
        preprocess_hint.setStyleSheet("color: #888; font-size: 11px;")
        layout.addRow("", preprocess_hint)

        # LLM テキスト整形に使う Groq モデル（両ホットキー共通・リストから選択）
        self._format_model_combo = QComboBox()
        for model in KNOWN_FORMAT_MODELS:
            self._format_model_combo.addItem(model)
        layout.addRow("Format Model:", self._format_model_combo)

        # 「おまかせ（自動判断）」モードで LLM に渡すプロンプト（編集可・空欄なら既定）
        self._format_auto_prompt_edit = QPlainTextEdit()
        self._format_auto_prompt_edit.setFixedHeight(110)
        layout.addRow("Auto Format Prompt:", self._format_auto_prompt_edit)

        auto_prompt_reset = QPushButton("既定に戻す")
        auto_prompt_reset.clicked.connect(
            lambda: self._format_auto_prompt_edit.setPlainText(DEFAULT_AUTO_PROMPT)
        )
        layout.addRow("", auto_prompt_reset)

        auto_prompt_hint = QLabel("整形モード「おまかせ（自動判断）」で LLM に渡す指示。自由に編集できます")
        auto_prompt_hint.setStyleSheet("color: #888; font-size: 11px;")
        layout.addRow("", auto_prompt_hint)

        return page

    def _populate_input_devices(self) -> None:
        """入力デバイス一覧をコンボボックスへ読み込む。"""
        current_value = "default"
        if hasattr(self, "_input_device_combo") and self._input_device_combo.count() > 0:
            current_data = self._input_device_combo.currentData()
            if current_data is not None:
                current_value = current_data

        self._input_device_combo.clear()
        self._input_device_combo.addItem("System Default", "default")

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

    def _load_current_settings(self) -> None:
        """設定ファイルから現在の値をUIに読み込む。"""
        config = self._config_manager.config

        # General - 共通設定
        self._lang_input.setText(config.get("language", "ja"))

        # General - ホットキー1
        hotkey1_config = config.get("hotkey1", {})
        self._hotkey1_input.setText(hotkey1_config.get("hotkey", "<f2>"))
        self._mode1_combo.setCurrentText(hotkey1_config.get("hotkey_mode", HotkeyMode.TOGGLE.value))
        backend1 = hotkey1_config.get("backend", "openai")
        if backend1 not in _BACKEND_TO_SERVICE:
            backend1 = TranscriptionBackend.OPENAI.value
        self._backend1_combo.setCurrentText(backend1)
        self._api1_prompt_input.setText(hotkey1_config.get("api_prompt", ""))

        # General - ホットキー2
        hotkey2_config = config.get("hotkey2", {})
        self._hotkey2_input.setText(hotkey2_config.get("hotkey", "<f3>"))
        self._mode2_combo.setCurrentText(hotkey2_config.get("hotkey_mode", HotkeyMode.TOGGLE.value))
        backend2 = hotkey2_config.get("backend", "groq")
        if backend2 not in _BACKEND_TO_SERVICE:
            backend2 = TranscriptionBackend.GROQ.value
        self._backend2_combo.setCurrentText(backend2)
        self._api2_prompt_input.setText(hotkey2_config.get("api_prompt", ""))

        # Advanced
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
        if self._format_model_combo.findText(saved_model) < 0:
            self._format_model_combo.addItem(saved_model)
        self._format_model_combo.setCurrentText(saved_model)

        # おまかせ整形プロンプト（空 = 既定。編集できるよう既定の実テキストを表示する）
        self._format_auto_prompt_edit.setPlainText(
            config.get("format_auto_prompt", "") or DEFAULT_AUTO_PROMPT
        )

        # テキスト整形設定（スロットごと）
        for slot_id in [1, 2]:
            hotkey_config = config.get(f"hotkey{slot_id}", {}) or {}
            getattr(self, f"_format{slot_id}_check").setChecked(
                bool(hotkey_config.get("format_enabled", False))
            )
            mode_combo = getattr(self, f"_format{slot_id}_mode_combo")
            index = mode_combo.findData(hotkey_config.get("format_mode", "auto"))
            mode_combo.setCurrentIndex(index if index >= 0 else 0)
            getattr(self, f"_format{slot_id}_custom_input").setText(
                hotkey_config.get("format_custom_prompt", "")
            )
            self._update_format_controls(slot_id)

        # APIモデルとバックエンド表示状態を初期化
        for slot_id in [1, 2]:
            backend_combo = getattr(self, f"_backend{slot_id}_combo")
            self._on_slot_backend_changed(slot_id, backend_combo.currentText())

    def _on_slot_backend_changed(self, slot_id: int, backend: str) -> None:
        """
        スロットのバックエンド選択変更を処理する。

        Args:
            slot_id: スロットID（1または2）
            backend: 選択されたバックエンド（openai/groq/elevenlabs/deepgram）
        """
        # 4 種すべて API バックエンド。未知値のみウィジェットを隠す
        is_api = backend in _BACKEND_TO_SERVICE
        api_widget = getattr(self, f"_api{slot_id}_widget")
        api_widget.setVisible(is_api)
        if not is_api:
            return

        # モデル候補を差し替える。保存済みモデルが当該 backend のものなら復元、
        # そうでなければ先頭（既定）を選ぶ
        model_combo = getattr(self, f"_api{slot_id}_model_combo")
        model_combo.clear()
        models = _BACKEND_MODELS.get(backend, [])
        model_combo.addItems(models)

        config = self._config_manager.config
        hotkey_config = config.get(f"hotkey{slot_id}", {})
        saved_model = hotkey_config.get("api_model", "")
        if saved_model in models:
            model_combo.setCurrentText(saved_model)
        elif models:
            model_combo.setCurrentIndex(0)

        # 切り替えに伴い Keychain 保存状況の表示も更新
        self._refresh_api_key_status(slot_id)

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
            "language": self._lang_input.text(),
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

            # LLM テキスト整形に使う Groq モデル（両ホットキー共通）
            "format_model": self._format_model_combo.currentText().strip() or "llama-3.1-8b-instant",
            # おまかせ整形プロンプト（既定文と同一なら空で保存し、既定文の将来更新に追従する）
            "format_auto_prompt": (
                "" if self._format_auto_prompt_edit.toPlainText().strip() == DEFAULT_AUTO_PROMPT.strip()
                else self._format_auto_prompt_edit.toPlainText()
            ),

            # ホットキー1 設定
            "hotkey1": {
                "hotkey": self._hotkey1_input.text(),
                "hotkey_mode": self._mode1_combo.currentText(),
                "backend": self._backend1_combo.currentText(),
                "api_model": self._api1_model_combo.currentText(),
                "api_prompt": self._api1_prompt_input.text(),
                "format_enabled": self._format1_check.isChecked(),
                "format_mode": self._format1_mode_combo.currentData(),
                "format_custom_prompt": self._format1_custom_input.text(),
            },

            # ホットキー2 設定
            "hotkey2": {
                "hotkey": self._hotkey2_input.text(),
                "hotkey_mode": self._mode2_combo.currentText(),
                "backend": self._backend2_combo.currentText(),
                "api_model": self._api2_model_combo.currentText(),
                "api_prompt": self._api2_prompt_input.text(),
                "format_enabled": self._format2_check.isChecked(),
                "format_mode": self._format2_mode_combo.currentData(),
                "format_custom_prompt": self._format2_custom_input.text(),
            },

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
            QMessageBox.critical(self, "Error", "Failed to save settings.")
