"""
初回起動オンボーディングウィンドウ（Windows・体験型 2 ペイン再デザイン）

初回起動でアプリがタスクトレイに黙って常駐すると「何これ？」となるため、
起動時にセットアップウィンドウを自動表示し、ログイン → 動作確認 → 体験までを
案内する。Windows には OS の権限ゲート（マイク/アクセシビリティ/入力監視）が無いので
権限ステップは持たない（Mac 版は権限 3 ステップ＋動作確認を持つ）。

Mac 版（macos/Sources/Voicekey/UI/OnboardingView.swift）に合わせた **左＝説明／右＝実際の画面
イメージ** の 2 ペイン構成。上部に細いパンくず（ようこそ › 準備 › 体験）。
- ようこそ・まとめは全画面インタースティシャル（中央 1 列）。
- ログイン・マイクテスト・録音キーテスト・体験 3 種は 2 ペイン。
- マイクテスト＝録音キーを押さずにレベルメーターで拾えているか確認（MicLevelMonitor・無料枠を消費しない）。
- 録音キーテスト＝録音キーを押すと右の巨大キーが光る（app.set_hotkey_test で録音は開始しない）。
- 体験＝右のメモ風モックウィンドウに実際に文字を入力してみる。

完了/クローズ時に onboarding_finished シグナルを 1 度だけ発火し、
app 側が settings.yaml の did_complete_onboarding を True にする。
"""

from typing import Optional

from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QPainter
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .styles import MacTheme
from ..core.mic_monitor import MicLevelMonitor
from ..core.audio_recorder import AudioRecorder
from ..utils.logger import get_logger

logger = get_logger(__name__)

# ステップ番号（QStackedWidget のインデックスと一致）。
# 権限ゲートの無い Windows は 権限ステップを持たず、ログインの後ろに動作確認（マイク/録音キーのテスト）→体験を足す。
_STEP_WELCOME = 0
_STEP_LOGIN = 1
_STEP_MIC_TEST = 2             # 動作確認1: マイクテスト（録音せずレベル確認）
_STEP_HOTKEY_TEST = 3          # 動作確認2: 録音キーテスト（押すとキーが光る）
_STEP_PRACTICE_BASIC = 4       # 体験1: 即時入力
_STEP_PRACTICE_HANDSFREE = 5   # 体験2: ハンズフリー（トグル）
_STEP_PRACTICE_FORMAT = 6      # 体験3: 文章整形（フィラーが消える）
_STEP_SUMMARY = 7              # まとめ（完了）

_PRACTICE_STEPS = (_STEP_PRACTICE_BASIC, _STEP_PRACTICE_HANDSFREE, _STEP_PRACTICE_FORMAT)
# 「スキップ」を出すステップ（動作確認・体験は強制しない）
_SKIPPABLE_STEPS = (_STEP_MIC_TEST, _STEP_HOTKEY_TEST) + _PRACTICE_STEPS
# 録音キーテスト／マイクテスト（この間は録音を開始させない＝無料枠を消費しない）
_HOTKEY_TEST_STEPS = (_STEP_MIC_TEST, _STEP_HOTKEY_TEST)


def _has_practice_input(text: str) -> bool:
    """練習欄に「文字が入った」と言えるか（前後の空白を除いて 1 文字以上）。

    録音→文字起こし→貼り付けで練習欄のテキストが変化したら成功とみなす純ロジック。
    Mac 版 OnboardingPractice.hasInput と同じ観点（体験ステップ成功判定・テスト対象）。
    """
    return bool(text.strip())


# 文字起こしモード（バックエンド）→ 新語彙の表示名。
# 即時入力=Deepgram / スタンダード=Groq（既定）。選択肢外（elevenlabs/openai）は
# 内部利用のため「スタンダード」表示にフォールバックする。
_BACKEND_LABELS = {
    "deepgram": "即時入力",
    "groq": "スタンダード",
    "elevenlabs": "スタンダード",
    "openai": "スタンダード",
}
# 録音のしかた（モード）→ 新語彙の表示名。
_MODE_LABELS = {
    "hold": "押している間だけ",
    "toggle": "押すたびに開始・停止",
}


class OnboardingWindow(QDialog):
    """初回セットアップの案内ウィンドウ（体験型 2 ペイン・8 ステップ）。"""

    # 完了（またはスキップ）時に 1 度だけ発火。app が完了フラグ保存に使う
    onboarding_finished = Signal()
    # 録音キーテスト: listener スレッドから押下スロット（無ければ None）を受けてメインスレッドへ渡す
    _hotkey_held_changed = Signal(object)

    def __init__(self, config_manager, app=None, parent=None) -> None:
        super().__init__(parent)
        self._config = config_manager
        # 体験ステップから整形オーバーライド・録音キーテストを操作するための本体参照（テスト時は None）
        self._app = app
        self._finished_emitted = False
        self._step = 0
        # 練習ページの部品（step → {"edit", "success", "login_note"}）。成功演出・自動フォーカスに使う
        self._practice_pages: dict = {}

        # マイクテスト用（録音せずレベルだけ測る軽量モニタ＋描画ポーリング）
        self._mic_monitor = MicLevelMonitor()
        self._mic_timer = QTimer(self)
        self._mic_timer.setInterval(33)   # 約 30fps
        self._mic_timer.timeout.connect(self._poll_mic_level)
        self._mic_meter: Optional[_LevelMeter] = None
        self._mic_device_combo: Optional[QComboBox] = None
        self._giant_key: Optional[_GiantKey] = None
        self._hotkey_held_changed.connect(self._on_hotkey_held)

        # 初回起動時のみ・構築時に dark_mode を 1 回だけ読み、設定画面と同一のテーマを適用する
        # （ライブテーマ切替は載せない）。setObjectName は setStyleSheet より前に付ける必要がある
        self.setObjectName("onboardingRoot")
        dark = bool(config_manager.get("dark_mode", False))
        self._dark = dark
        self._c = MacTheme.Colors(dark)
        self.setStyleSheet(MacTheme.get_stylesheet(dark))

        self.setWindowTitle("VoiceKey へようこそ")
        self.setMinimumWidth(780)
        self.setMinimumHeight(520)
        self.resize(820, 560)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # --- 上部: パンくず（ようこそ › 準備 › 体験） ---
        crumb_bar = QWidget()
        crumb_layout = QHBoxLayout(crumb_bar)
        crumb_layout.setContentsMargins(28, 16, 28, 12)
        crumb_layout.addStretch(1)
        self._breadcrumb = _Breadcrumb(["ようこそ", "準備", "体験"], self._c)
        crumb_layout.addWidget(self._breadcrumb)
        crumb_layout.addStretch(1)
        root.addWidget(crumb_bar)
        root.addWidget(_hline())

        # --- 本文（8 ステップをスタック） ---
        self._stack = QStackedWidget()
        self._stack.addWidget(self._build_welcome_page())               # 0
        self._stack.addWidget(self._build_login_page())                 # 1
        self._stack.addWidget(self._build_mic_test_page())              # 2
        self._stack.addWidget(self._build_hotkey_test_page())           # 3
        self._stack.addWidget(self._build_practice_basic_page())        # 4
        self._stack.addWidget(self._build_practice_handsfree_page())    # 5
        self._stack.addWidget(self._build_practice_format_page())       # 6
        self._stack.addWidget(self._build_summary_page())               # 7
        root.addWidget(self._stack, 1)

        # --- フッター（戻る / スキップ / 主ボタン） ---
        root.addWidget(_hline())
        footer = QHBoxLayout()
        footer.setContentsMargins(28, 14, 28, 18)
        footer.setSpacing(8)
        self._back_btn = QPushButton("戻る")
        self._back_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._back_btn.clicked.connect(self._on_back)
        footer.addWidget(self._back_btn)
        # 動作確認・体験は強制しないので、いつでも完了へ飛べる「スキップ」を常設
        self._skip_btn = QPushButton("スキップ")
        self._skip_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._skip_btn.clicked.connect(self._on_skip)
        footer.addWidget(self._skip_btn)
        footer.addStretch(1)
        self._primary_btn = QPushButton("セットアップを始める")
        self._primary_btn.setProperty("class", "primary")   # グローバルのアクセント塗りボタンにする
        self._primary_btn.setDefault(True)
        self._primary_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._primary_btn.clicked.connect(self._on_primary)
        footer.addWidget(self._primary_btn)
        root.addLayout(footer)

        # ログイン待ち/交換中はステータスをポーリングして表示を最新化する
        # （deep link はブラウザ→OS→アプリと非同期に戻ってくるため）
        self._login_poll = QTimer(self)
        self._login_poll.setInterval(800)
        self._login_poll.timeout.connect(self._refresh_login_status)

        self._update_nav()

    # ------------------------------------------------------------------
    # 2 ペインの共通土台
    # ------------------------------------------------------------------

    def _two_pane(self, left: QWidget, right: QWidget) -> QWidget:
        """左＝説明／右＝画面イメージ の 2 ペインページを組む。"""
        page = QWidget()
        layout = QHBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        left.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        right.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        layout.addWidget(left, 5)
        layout.addWidget(right, 4)
        return page

    def _left_column(self) -> tuple[QWidget, QVBoxLayout]:
        """左ペイン（説明側）の器を返す。"""
        col = QWidget()
        cl = QVBoxLayout(col)
        cl.setContentsMargins(36, 28, 28, 28)
        cl.setSpacing(12)
        return col, cl

    def _wash_pane(self) -> tuple[QWidget, QVBoxLayout]:
        """右ペイン（淡いウォッシュの画面イメージ側）の器を返す。"""
        pane = QWidget()
        # 左との差を出す控えめなウォッシュ（無彩色寄り・紫やネオンは使わない）
        if self._dark:
            wash = "rgba(255, 255, 255, 0.035)"
        else:
            wash = "rgba(10, 122, 255, 0.05)"
        pane.setStyleSheet(f"background: {wash};")
        pl = QVBoxLayout(pane)
        pl.setContentsMargins(28, 28, 36, 28)
        pl.setSpacing(12)
        pl.addStretch(1)
        return pane, pl

    def _heading(self, text: str) -> QLabel:
        h = QLabel(text)
        h.setStyleSheet("font-size: 22px; font-weight: 700;")
        h.setWordWrap(True)
        return h

    def _body(self, text: str) -> QLabel:
        b = QLabel(text)
        b.setWordWrap(True)
        b.setStyleSheet(f"color: {self._c.SECONDARY_TEXT}; font-size: 13px;")
        return b

    # ------------------------------------------------------------------
    # ページ生成（インタースティシャル）
    # ------------------------------------------------------------------

    def _build_welcome_page(self) -> QWidget:
        """ようこそ（全画面インタースティシャル・中央 1 列）。"""
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(48, 40, 48, 40)
        outer.addStretch(1)

        hero = QLabel("‧₊˚ 🎙 ˚₊‧")
        hero.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hero.setStyleSheet(f"font-size: 34px; color: {self._c.ACCENT};")
        outer.addWidget(hero)

        title = QLabel("話すだけで、文字になる。")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet("font-size: 26px; font-weight: 700;")
        outer.addWidget(title)

        sub = QLabel("録音キーを押して話すだけ。離すとカーソル位置に文字が入ります。\n数十秒で使い方まで確認できます。")
        sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sub.setWordWrap(True)
        sub.setStyleSheet(f"color: {self._c.SECONDARY_TEXT}; font-size: 14px;")
        outer.addWidget(sub)

        outer.addStretch(2)
        return page

    def _build_summary_page(self) -> QWidget:
        """まとめ（全画面インタースティシャル・録音キー 2 枚のカード）。"""
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(48, 32, 48, 32)
        outer.addStretch(1)

        title = QLabel("話して、タイプしないで。")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet("font-size: 26px; font-weight: 700;")
        outer.addWidget(title)

        sub = QLabel("これだけ覚えれば、もう準備完了です。")
        sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sub.setStyleSheet(f"color: {self._c.SECONDARY_TEXT}; font-size: 14px;")
        outer.addWidget(sub)

        # 実際の既定設定を settings.yaml から読んで表示する（ハードコードしない）
        cards = QHBoxLayout()
        cards.setSpacing(14)
        cards.addStretch(1)
        cards.addWidget(self._summary_key_card("録音キー 1（メイン）", self._config.get("hotkey1", {})))
        cards.addWidget(self._summary_key_card("録音キー 2（サブ）", self._config.get("hotkey2", {})))
        cards.addStretch(1)
        outer.addSpacing(12)
        outer.addLayout(cards)

        foot = QLabel("設定はいつでも、タスクトレイのアイコンから変えられます。")
        foot.setAlignment(Qt.AlignmentFlag.AlignCenter)
        foot.setStyleSheet(f"color: {self._c.SECONDARY_TEXT}; font-size: 12px;")
        outer.addSpacing(14)
        outer.addWidget(foot)

        outer.addStretch(2)
        return page

    def _summary_key_card(self, slot_label: str, slot: dict) -> QWidget:
        """録音キー 1 つ分のまとめカード（キー表記・文字起こしモード・録音のしかた）。"""
        hotkey = str(slot.get("hotkey", "") or "未設定")
        backend = str(slot.get("backend", "") or "").lower()
        mode = str(slot.get("hotkey_mode", "") or "").lower()
        backend_label = _BACKEND_LABELS.get(backend, "スタンダード")
        mode_label = _MODE_LABELS.get(mode, mode)

        card = QFrame()
        card.setObjectName("card")
        card.setFixedWidth(230)
        cl = QVBoxLayout(card)
        cl.setContentsMargins(16, 14, 16, 14)
        cl.setSpacing(6)
        name = QLabel(slot_label)
        name.setAlignment(Qt.AlignmentFlag.AlignCenter)
        name.setStyleSheet(f"color: {self._c.SECONDARY_TEXT}; font-size: 12px;")
        cl.addWidget(name)
        key = QLabel(hotkey)
        key.setAlignment(Qt.AlignmentFlag.AlignCenter)
        key.setStyleSheet("font-size: 18px; font-weight: 700;")
        cl.addWidget(key)
        detail = QLabel(f"{backend_label} ・ {mode_label}")
        detail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        detail.setWordWrap(True)
        detail.setStyleSheet(f"color: {self._c.SECONDARY_TEXT}; font-size: 12px;")
        cl.addWidget(detail)
        return card

    # ------------------------------------------------------------------
    # ページ生成（2 ペイン）
    # ------------------------------------------------------------------

    def _build_login_page(self) -> QWidget:
        col, cl = self._left_column()
        cl.addWidget(self._heading("ログイン"))
        cl.addWidget(self._body(
            "ログインすると無料体験で文字起こしが使えます。ログインはブラウザで行います。"
            "あとで設定画面からでも可能です。"
        ))

        self._login_status = QLabel("")
        self._login_status.setWordWrap(True)
        cl.addWidget(self._login_status)

        self._login_btn = QPushButton("ブラウザでログイン")
        self._login_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._login_btn.clicked.connect(self._on_login_clicked)
        row = QHBoxLayout()
        row.addWidget(self._login_btn)
        row.addStretch(1)
        cl.addLayout(row)
        cl.addStretch(1)

        pane, pl = self._wash_pane()
        for title, body in (
            ("無料で 200 回ぶん試せます", "ログインするだけで、すぐに文字起こしを体験できます。"),
            ("この端末だけで完結", "履歴はこの Windows の中に保存されます。"),
        ):
            pl.addWidget(self._value_card(title, body))
        pl.addStretch(1)
        return self._two_pane(col, pane)

    def _value_card(self, title: str, body: str) -> QWidget:
        card = QFrame()
        card.setObjectName("card")
        cl = QVBoxLayout(card)
        cl.setContentsMargins(16, 12, 16, 12)
        cl.setSpacing(3)
        name = QLabel(title)
        name.setStyleSheet("font-weight: 600;")
        cl.addWidget(name)
        desc = QLabel(body)
        desc.setWordWrap(True)
        desc.setStyleSheet(f"color: {self._c.SECONDARY_TEXT}; font-size: 12px;")
        cl.addWidget(desc)
        return card

    def _build_mic_test_page(self) -> QWidget:
        col, cl = self._left_column()
        cl.addWidget(self._heading("マイクをテスト"))
        cl.addWidget(self._body(
            "録音キーはまだ押さなくて大丈夫。ためしに少しだけ声を出してみてください。"
            "右のバーが動けば、マイクはちゃんと聞こえています。"
        ))
        prompt = QLabel("話している間に、右のバーは動いていますか？")
        prompt.setWordWrap(True)
        prompt.setStyleSheet("font-weight: 600;")
        cl.addWidget(prompt)

        # 入力デバイス選択（うまく動かないとき用）
        dev_row = QHBoxLayout()
        dev_row.setSpacing(8)
        dev_label = QLabel("マイク")
        dev_label.setStyleSheet(f"color: {self._c.SECONDARY_TEXT};")
        self._mic_device_combo = QComboBox()
        self._mic_device_combo.setMinimumWidth(200)
        self._mic_device_combo.currentIndexChanged.connect(self._on_mic_device_changed)
        dev_row.addWidget(dev_label)
        dev_row.addWidget(self._mic_device_combo, 1)
        cl.addSpacing(4)
        cl.addLayout(dev_row)
        cl.addStretch(1)

        pane, pl = self._wash_pane()
        self._mic_meter = _LevelMeter(self._c)
        self._mic_meter.setFixedHeight(120)
        pl.addWidget(self._mic_meter)
        hint = QLabel("🎙  マイクに向かって話してみてください")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint.setStyleSheet(f"color: {self._c.SECONDARY_TEXT}; font-size: 12px;")
        pl.addWidget(hint)
        pl.addStretch(1)
        return self._two_pane(col, pane)

    def _build_hotkey_test_page(self) -> QWidget:
        col, cl = self._left_column()
        main_key = self._slot_key("hotkey1")
        cl.addWidget(self._heading("録音キーをためす"))
        cl.addWidget(self._body(
            f"下の録音キー {main_key} を、ちょっと押してみてください。押している間、右のキーが光れば、"
            "しっかり効いています。ここでは文字は入りません（練習です）。"
        ))
        prompt = QLabel("押している間、右のキーは光っていますか？")
        prompt.setWordWrap(True)
        prompt.setStyleSheet("font-weight: 600;")
        cl.addWidget(prompt)
        cl.addStretch(1)

        pane, pl = self._wash_pane()
        self._giant_key = _GiantKey(main_key, self._c)
        pl.addWidget(self._giant_key, alignment=Qt.AlignmentFlag.AlignCenter)
        pl.addStretch(1)
        return self._two_pane(col, pane)

    # ------------------------------------------------------------------
    # 体験ステップ（実際に録音キーを押して入力してみる・右はメモ風モック）
    # ------------------------------------------------------------------

    def _slot_key(self, key: str) -> str:
        """設定から録音キーの表記を取り出す（例: '<f2>'）。未設定は「未設定」。"""
        slot = self._config.get(key, {}) or {}
        return str(slot.get("hotkey", "") or "未設定")

    def _handsfree_key(self) -> str:
        """トグル（ハンズフリー）録音に使う録音キー。既定はサブ(hotkey2・toggle)。"""
        for key in ("hotkey2", "hotkey1"):
            slot = self._config.get(key, {}) or {}
            if str(slot.get("hotkey_mode", "") or "").lower() == "toggle":
                return self._slot_key(key)
        return self._slot_key("hotkey2")

    def _build_practice_page(
        self, step: int, heading: str, intro: str,
        example: str, prompt: str, success: str, hint: str = "",
    ) -> QWidget:
        """左＝説明（例文・促し・成功演出）／右＝メモ風モックに練習入力欄。

        録音キーを押して話すと、本体の貼り付けが前面ウィンドウの練習入力欄へテキストを入れる。
        テキストが入った瞬間を textChanged で捕まえ、成功メッセージを出す
        （手入力でも成功扱いにして体験を妨げない）。Mac 版 PracticeStepView と同構成。
        """
        col, cl = self._left_column()
        cl.addWidget(self._heading(heading))
        cl.addWidget(self._body(intro))

        # ログイン必須の注意（未ログイン時のみ表示。文字起こしはサーバー経由のため）
        login_note = QLabel("体験するにはログインが必要です（前のステップでログインできます）")
        login_note.setWordWrap(True)
        login_note.setStyleSheet(f"color: {self._c.WARNING}; font-weight: 600; font-size: 12px;")
        login_note.setVisible(False)
        cl.addWidget(login_note)

        # 読む例文（カードに入れて「これを読む」と分かるように）
        example_card = QFrame()
        example_card.setObjectName("card")
        ecl = QVBoxLayout(example_card)
        ecl.setContentsMargins(16, 12, 16, 12)
        ex = QLabel(f"「{example}」")
        ex.setWordWrap(True)
        ex.setStyleSheet("font-size: 15px; font-weight: 500;")
        ecl.addWidget(ex)
        cl.addWidget(example_card)

        prompt_label = QLabel(prompt)
        prompt_label.setWordWrap(True)
        prompt_label.setStyleSheet("font-weight: 600;")
        cl.addWidget(prompt_label)

        if hint:
            hint_label = QLabel(hint)
            hint_label.setWordWrap(True)
            hint_label.setStyleSheet(f"color: {self._c.SECONDARY_TEXT}; font-size: 12px;")
            cl.addWidget(hint_label)

        success_label = QLabel(f"✓ {success}")
        success_label.setWordWrap(True)
        success_label.setStyleSheet(f"color: {self._c.SUCCESS}; font-weight: 700;")
        success_label.setVisible(False)
        cl.addWidget(success_label)
        cl.addStretch(1)

        # 右: メモ風モックウィンドウの中に練習入力欄（自動フォーカス。前面＝このウィンドウなので貼り付けはここに入る）
        pane, pl = self._wash_pane()
        mock, edit = self._mock_memo_window()
        edit.setPlaceholderText(f"{self._slot_key('hotkey1')} を押しながら、声で入力…")
        pl.addWidget(mock)
        pl.addStretch(1)

        edit.textChanged.connect(
            lambda e=edit, s=success_label: self._on_practice_changed(e, s)
        )
        self._practice_pages[step] = {
            "edit": edit, "success": success_label, "login_note": login_note,
        }
        return self._two_pane(col, pane)

    def _mock_memo_window(self) -> tuple[QWidget, QTextEdit]:
        """信号機ボタン＋「メモ」タイトルバー＋入力欄のミニ・アプリウィンドウ風モック。"""
        frame = QFrame()
        frame.setObjectName("card")
        fl = QVBoxLayout(frame)
        fl.setContentsMargins(0, 0, 0, 0)
        fl.setSpacing(0)

        titlebar = QWidget()
        tl = QHBoxLayout(titlebar)
        tl.setContentsMargins(12, 10, 12, 10)
        tl.setSpacing(6)
        for color in ("#FF5F57", "#FEBC2E", "#28C840"):
            dot = QLabel()
            dot.setFixedSize(11, 11)
            dot.setStyleSheet(f"background: {color}; border-radius: 5px;")
            tl.addWidget(dot)
        tl.addStretch(1)
        memo = QLabel("メモ")
        memo.setStyleSheet(f"color: {self._c.SECONDARY_TEXT}; font-size: 12px;")
        tl.addWidget(memo)
        tl.addStretch(1)
        tl.addSpacing(33)   # 左右対称に見せる余白
        fl.addWidget(titlebar)
        fl.addWidget(_hline())

        edit = QTextEdit()
        edit.setFixedHeight(150)
        edit.setStyleSheet("border: none; background: transparent;")
        fl.addWidget(edit)
        return frame, edit

    def _on_practice_changed(self, edit: QTextEdit, success_label: QLabel) -> None:
        """練習欄にテキストが入ったら成功メッセージを出す（一度出したら消さない）。"""
        if not success_label.isVisible() and _has_practice_input(edit.toPlainText()):
            success_label.setVisible(True)

    def _build_practice_basic_page(self) -> QWidget:
        return self._build_practice_page(
            step=_STEP_PRACTICE_BASIC,
            heading="さっそく入力してみる",
            intro="録音キーを押しながら、下の文を声に出して読んでみてください。"
                  "キーを離すと、右のメモにその場で文字が入ります。",
            example="明日の打ち合わせ、15時に変更でお願いします。",
            prompt=f"⬇ {self._slot_key('hotkey1')} を押しながら、上の文を読んでみてください",
            success="よくできました！ 声がそのまま文字になりました",
        )

    def _build_practice_handsfree_page(self) -> QWidget:
        return self._build_practice_page(
            step=_STEP_PRACTICE_HANDSFREE,
            heading="ハンズフリーで話す",
            intro="キーを押して離すと録音が続きます。手を止めずに長い文章を話せます。"
                  "もう一度押すと確定して入力されます。",
            example="これはハンズフリーのテストです。手を離したまま、このまま何文か続けて話してみます。",
            prompt=f"⬇ {self._handsfree_key()} を一度押して話し、話し終えたらもう一度押して確定してください",
            success="できました！ 押しっぱなしにしなくても入力できます",
            hint="長い議事録のときに便利です。",
        )

    def _build_practice_format_page(self) -> QWidget:
        return self._build_practice_page(
            step=_STEP_PRACTICE_FORMAT,
            heading="話した言葉を、きれいな文章に",
            intro="「文章を自動で整える」をオンにして試します。"
                  "「えーっと」などのフィラーや言い直しはそのまま声に出してみてください。",
            example="えーっと、明日の会議なんですけど、あー、10時から、やっぱり11時からでお願いします",
            prompt=f"⬇ {self._slot_key('hotkey1')} を押しながら、上の文をそのまま読んでみてください",
            success="フィラーや言い直しが自動で消えました",
            hint="整え方は「標準／そのまま／すっきり／箇条書き」の 4 種から選べます"
                 "（設定でいつでも変更できます）。",
        )

    # ------------------------------------------------------------------
    # マイクテスト（録音せずレベルだけ確認）
    # ------------------------------------------------------------------

    def _populate_mic_devices(self) -> None:
        """入力デバイス一覧を選択肢に反映する（現在の設定値を選択状態にする）。"""
        combo = self._mic_device_combo
        if combo is None:
            return
        combo.blockSignals(True)
        combo.clear()
        combo.addItem("システム既定", "default")
        target = str(self._config.get("audio_input_device", "default"))
        for dev in AudioRecorder.list_input_devices():
            combo.addItem(dev.get("label", dev.get("name", "?")), str(dev.get("id")))
        # 現在値を選択
        for i in range(combo.count()):
            if str(combo.itemData(i)) == target:
                combo.setCurrentIndex(i)
                break
        combo.blockSignals(False)

    def _on_mic_device_changed(self, _index: int) -> None:
        """マイクテストの入力デバイスを切り替える（保存もして以後の録音でも使う）。"""
        combo = self._mic_device_combo
        if combo is None:
            return
        value = combo.currentData()
        if value is None:
            return
        self._config.save({"audio_input_device": value})
        if self._step == _STEP_MIC_TEST:
            self._mic_monitor.start(value)

    def _start_mic_test(self) -> None:
        """マイクテストのモニタリングとメーター描画を開始する。"""
        self._populate_mic_devices()
        device = self._config.get("audio_input_device", "default")
        self._mic_monitor.start(device)
        self._mic_timer.start()

    def _stop_mic_test(self) -> None:
        """マイクテストのモニタリングを停止する。"""
        self._mic_timer.stop()
        self._mic_monitor.stop()
        if self._mic_meter is not None:
            self._mic_meter.set_level(0.0)

    def _poll_mic_level(self) -> None:
        if self._mic_meter is not None:
            self._mic_meter.set_level(self._mic_monitor.read_level())

    # ------------------------------------------------------------------
    # 録音キーテスト（押すと光る・録音しない）
    # ------------------------------------------------------------------

    def _start_hotkey_test(self, with_callback: bool) -> None:
        """録音キーテスト（with_callback=True）／マイクテスト（False）用に録音抑止モードへ入る。"""
        if self._app is None:
            return
        callback = self._hotkey_held_changed.emit if with_callback else None
        self._app.set_hotkey_test(True, callback)

    def _stop_hotkey_test(self) -> None:
        if self._app is None:
            return
        self._app.set_hotkey_test(False)

    def _on_hotkey_held(self, slot) -> None:
        """listener スレッド → シグナル経由で受けた「押されている録音キー」を巨大キーへ反映。"""
        if self._giant_key is not None:
            self._giant_key.set_held(slot is not None)

    # ------------------------------------------------------------------
    # ナビゲーション
    # ------------------------------------------------------------------

    def _on_back(self) -> None:
        if self._step > 0:
            self._step -= 1
            self._skip_login_if_personal(direction=-1)
            self._go_to(self._step)

    def _on_primary(self) -> None:
        if self._step >= _STEP_SUMMARY:
            self._finish()
            return
        self._step += 1
        self._skip_login_if_personal(direction=+1)
        self._go_to(self._step)

    def _skip_login_if_personal(self, direction: int) -> None:
        """personal はログインステップを飛ばす（Mac 版 OnboardingView の goNext/goBack と同じ）。"""
        from ..utils import secrets

        if self._step == _STEP_LOGIN and secrets.is_personal_build():
            self._step += direction

    def _on_skip(self) -> None:
        """動作確認・体験をスキップしてまとめへ飛ぶ（強制しない）。"""
        self._step = _STEP_SUMMARY
        self._go_to(self._step)

    def _go_to(self, step: int) -> None:
        self._stack.setCurrentIndex(step)
        self._update_nav()
        if step == _STEP_LOGIN:
            self._refresh_login_status()

        # マイクテスト: モニタ開始 / それ以外: 停止
        if step == _STEP_MIC_TEST:
            self._start_mic_test()
        else:
            self._stop_mic_test()

        # 録音抑止モード: マイクテスト（callback なし）／録音キーテスト（callback あり）で ON、他は OFF
        if step in _HOTKEY_TEST_STEPS:
            self._start_hotkey_test(with_callback=(step == _STEP_HOTKEY_TEST))
        else:
            self._stop_hotkey_test()
        if step == _STEP_HOTKEY_TEST and self._giant_key is not None:
            self._giant_key.set_held(False)

        # 整形体験ステップの間だけ整形を強制 ON（それ以外では必ず解除）
        self._apply_format_override(step == _STEP_PRACTICE_FORMAT)

        # 練習ステップに入ったら入力欄を自動フォーカスし、ログイン必須の注意を最新化する
        if step in self._practice_pages:
            parts = self._practice_pages[step]
            parts["login_note"].setVisible(not self._is_logged_in())
            parts["edit"].setFocus()

    def _apply_format_override(self, enabled: bool) -> None:
        """本体（app）に整形オーバーライドの ON/OFF を伝える（テスト時は app=None で無視）。"""
        if self._app is not None:
            self._app.set_practice_format_override(enabled)

    def _update_nav(self) -> None:
        """現在ステップに合わせてパンくず・戻る/スキップ/主ボタンの表示・文言を更新する。"""
        # パンくずの現在フェーズ（ようこそ=0 / 準備=1 / 体験=2）
        if self._step == _STEP_WELCOME:
            phase = 0
        elif self._step == _STEP_LOGIN:
            phase = 1
        else:
            phase = 2
        self._breadcrumb.set_phase(phase)

        self._back_btn.setVisible(self._step > 0)
        self._skip_btn.setVisible(self._step in _SKIPPABLE_STEPS)
        if self._step == _STEP_WELCOME:
            self._primary_btn.setText("はじめる")
        elif self._step == _STEP_LOGIN:
            # ログイン済みなら「次へ」、未ログインなら「あとで」（スキップ可）
            self._primary_btn.setText("次へ" if self._is_logged_in() else "あとで")
        elif self._step == _STEP_SUMMARY:
            self._primary_btn.setText("VoiceKey をはじめる")
        else:
            # 動作確認・体験は任意なので「次へ」で常に進める（成功しなくても先へ）
            self._primary_btn.setText("次へ")

    # ------------------------------------------------------------------
    # ログイン（settings_window と同じ login_coordinator を流用）
    # ------------------------------------------------------------------

    def _on_login_clicked(self) -> None:
        """ログイン開始: state を生成し、既定ブラウザでログインページを開く。"""
        from ..core import login_coordinator

        url = login_coordinator.shared().begin_login()
        QDesktopServices.openUrl(QUrl(url))
        self._refresh_login_status()
        self._login_poll.start()

    def _is_logged_in(self) -> bool:
        from ..core import login_coordinator

        return login_coordinator.shared().status == login_coordinator.LoginCoordinator.LOGGED_IN

    def _refresh_login_status(self) -> None:
        """ログイン状態を表示へ反映する（ポーリング/表示切替時に呼ぶ）。"""
        from ..core import login_coordinator

        coord = login_coordinator.shared()
        LC = login_coordinator.LoginCoordinator
        status = coord.status
        if status == LC.LOGGED_IN:
            email = coord.account_email
            self._login_status.setText(f"ログイン済み（{email}）" if email else "ログイン済み")
            self._login_status.setStyleSheet(f"color: {self._c.SUCCESS};")
            self._login_btn.setVisible(False)
            self._login_poll.stop()
        elif status == LC.WAITING:
            self._login_status.setText("ブラウザでログインを完了してください…")
            self._login_status.setStyleSheet(f"color: {self._c.SECONDARY_TEXT};")
            self._login_btn.setEnabled(True)
        elif status == LC.EXCHANGING:
            self._login_status.setText("ログイン処理中…")
            self._login_status.setStyleSheet(f"color: {self._c.SECONDARY_TEXT};")
            self._login_btn.setEnabled(False)
        elif status == LC.FAILED:
            self._login_status.setText(coord.error or "ログインに失敗しました。")
            self._login_status.setStyleSheet(f"color: {self._c.WARNING};")
            self._login_btn.setVisible(True)
            self._login_btn.setEnabled(True)
            self._login_poll.stop()
        else:  # IDLE（未ログイン）
            self._login_status.setText("")
            self._login_btn.setVisible(True)
            self._login_btn.setEnabled(True)
        # ログイン済みになったら主ボタンを「次へ」に更新する
        if self._step == _STEP_LOGIN:
            self._primary_btn.setText("次へ" if self._is_logged_in() else "あとで")

    # ------------------------------------------------------------------
    # 完了/クローズ（スキップも完了扱いにして毎回は出さない）
    # ------------------------------------------------------------------

    def _finish(self) -> None:
        self._emit_finished_once()
        self.close()

    def _emit_finished_once(self) -> None:
        if self._finished_emitted:
            return
        self._finished_emitted = True
        self._login_poll.stop()
        # テスト用モード（マイクモニタ・録音キーテスト・整形オーバーライド）を必ず解除
        self._stop_mic_test()
        self._stop_hotkey_test()
        self._apply_format_override(False)
        self.onboarding_finished.emit()

    def reject(self) -> None:
        """Esc/キャンセル。スキップ扱いで完了フラグを立てる（毎回は出さない）。"""
        self._emit_finished_once()
        super().reject()

    def closeEvent(self, event) -> None:
        """× で閉じた場合もスキップ扱いで完了フラグを立てる。"""
        self._emit_finished_once()
        super().closeEvent(event)


class _Breadcrumb(QWidget):
    """上部の細いパンくず（ようこそ › 準備 › 体験・現在フェーズを下線で示す）。"""

    def __init__(self, phases: list, colors, parent=None) -> None:
        super().__init__(parent)
        self._c = colors
        self._current = 0
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self._labels = []
        for i, name in enumerate(phases):
            if i > 0:
                sep = QLabel("›")
                sep.setStyleSheet(f"color: {self._c.SECONDARY_TEXT}; font-size: 12px;")
                layout.addWidget(sep)
            lab = QLabel(name)
            self._labels.append(lab)
            layout.addWidget(lab)
        self.set_phase(0)

    def set_phase(self, current: int) -> None:
        self._current = current
        for i, lab in enumerate(self._labels):
            if i == current:
                lab.setStyleSheet(
                    f"color: {self._c.TEXT}; font-size: 12px; font-weight: 700; "
                    f"border-bottom: 2px solid {self._c.TEXT}; padding-bottom: 2px;"
                )
            else:
                lab.setStyleSheet(f"color: {self._c.SECONDARY_TEXT}; font-size: 12px;")


class _LevelMeter(QWidget):
    """マイク入力レベルの波形バー（中央が高い 13 本・0.0-1.0 を反映）。"""

    _BARS = 13

    def __init__(self, colors, parent=None) -> None:
        super().__init__(parent)
        self._c = colors
        self._level = 0.0
        # 中央が高い形状の係数（Mac 版 MicMeterView と同じ雰囲気）
        self._shape = [0.35, 0.5, 0.62, 0.78, 0.9, 0.97, 1.0, 0.97, 0.9, 0.78, 0.62, 0.5, 0.35]

    def set_level(self, level: float) -> None:
        self._level = max(0.0, min(1.0, float(level)))
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w = self.width()
        h = self.height()
        n = self._BARS
        gap = 6
        bar_w = max(3, (w - gap * (n - 1)) / n)
        accent = QColor(self._c.ACCENT)
        idle = QColor(self._c.SECONDARY_TEXT)
        idle.setAlphaF(0.35)
        for i in range(n):
            factor = self._shape[i]
            # 最低でも小さく見えるベースライン＋レベルに応じて伸びる
            frac = 0.12 + 0.88 * self._level * factor
            bar_h = max(4.0, h * frac)
            x = i * (bar_w + gap)
            y = (h - bar_h) / 2
            painter.setBrush(accent if self._level > 0.02 else idle)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(int(x), int(y), int(bar_w), int(bar_h), 3, 3)
        painter.end()


class _GiantKey(QLabel):
    """録音キーテスト用の巨大キー表示（押している間は光る）。"""

    def __init__(self, key_text: str, colors, parent=None) -> None:
        super().__init__(key_text, parent)
        self._c = colors
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setFixedSize(150, 110)
        self.set_held(False)

    def set_held(self, held: bool) -> None:
        if held:
            self.setStyleSheet(
                f"font-size: 30px; font-weight: 700; color: #FFFFFF; "
                f"background: {self._c.ACCENT}; border-radius: 16px; "
                f"border: 1px solid {self._c.ACCENT};"
            )
        else:
            self.setStyleSheet(
                f"font-size: 30px; font-weight: 700; color: {self._c.SECONDARY_TEXT}; "
                f"background: {self._c.CARD_BG}; border-radius: 16px; "
                f"border: 1px solid {self._c.CARD_BORDER};"
            )


def _hline() -> QFrame:
    """水平の区切り線（グローバルの QFrame#hairline スタイルを使う）。"""
    line = QFrame()
    line.setObjectName("hairline")
    line.setFixedHeight(1)
    return line
