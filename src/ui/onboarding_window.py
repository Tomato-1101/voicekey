"""
初回起動オンボーディングウィンドウ（Phase 5・Windows）

初回起動でアプリがタスクトレイに黙って常駐すると「何これ？」となるため、
起動時にセットアップウィンドウを自動表示し、概要 → ログイン → 使い方の 3 ステップを
案内する。Windows には OS の権限ゲート（マイク/アクセシビリティ/入力監視）が無いので
権限ステップは持たない（Mac 版は 6 ステップ）。

文言は新語彙（録音キー／文字起こしモード／即時入力／
スタンダード／文章を自動で整える）に合わせる。ログインは settings_window と同じ
login_coordinator を流用し、ブラウザで行う（「あとで」でスキップ可）。

完了/クローズ時に onboarding_finished シグナルを 1 度だけ発火し、
app 側が settings.yaml の did_complete_onboarding を True にする。
"""

from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .styles import MacTheme
from ..utils.logger import get_logger

logger = get_logger(__name__)

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
    """初回セットアップの案内ウィンドウ（3 ステップ）。"""

    # 完了（またはスキップ）時に 1 度だけ発火。app が完了フラグ保存に使う
    onboarding_finished = Signal()

    def __init__(self, config_manager, parent=None) -> None:
        super().__init__(parent)
        self._config = config_manager
        self._finished_emitted = False
        self._step = 0

        # 初回起動時のみ・構築時に dark_mode を 1 回だけ読み、設定画面と同一のテーマを適用する
        # （ライブテーマ切替は載せない）。setObjectName は setStyleSheet より前に付ける必要がある
        self.setObjectName("onboardingRoot")
        dark = bool(config_manager.get("dark_mode", False))
        self._c = MacTheme.Colors(dark)
        self.setStyleSheet(MacTheme.get_stylesheet(dark))

        self.setWindowTitle("VoiceKey へようこそ")
        self.setMinimumWidth(540)
        self.setMinimumHeight(440)

        root = QVBoxLayout(self)
        # 四周に backdrop（対角グラデ）を覗かせ、中身を「中央 1 島」として浮かせる（Mac 版に揃える）
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(0)

        # コンテンツ全体（ヘッダ・ドット・ページ本体・下部ボタン）を 1 つの角丸ガラス島で包む。
        # 島の面・リムは QSS の QFrame#onboardingIsland（sidebarPane と同様式）が描く
        island = QFrame()
        island.setObjectName("onboardingIsland")
        island_v = QVBoxLayout(island)
        island_v.setContentsMargins(20, 20, 20, 20)   # 島の内側 padding（角丸の内に中身を収める）
        island_v.setSpacing(0)
        root.addWidget(island, 1)

        # --- ヘッダー（タイトル + ステップドット） ---
        header = QVBoxLayout()
        header.setContentsMargins(24, 20, 24, 12)
        header.setSpacing(10)
        title = QLabel("VoiceKey へようこそ")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet("font-size: 15px; font-weight: 600;")
        header.addWidget(title)
        self._dots = _StepDots(count=3)
        header.addWidget(self._dots, alignment=Qt.AlignmentFlag.AlignCenter)
        island_v.addLayout(header)
        island_v.addWidget(_hline())

        # --- 本文（3 ステップをスタック） ---
        self._stack = QStackedWidget()
        self._stack.addWidget(self._build_welcome_page())
        self._stack.addWidget(self._build_login_page())
        self._stack.addWidget(self._build_done_page())
        island_v.addWidget(self._stack, 1)

        # --- フッター（戻る / 主ボタン） ---
        island_v.addWidget(_hline())
        footer = QHBoxLayout()
        footer.setContentsMargins(20, 12, 20, 16)
        footer.setSpacing(8)
        self._back_btn = QPushButton("戻る")
        self._back_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._back_btn.clicked.connect(self._on_back)
        footer.addWidget(self._back_btn)
        footer.addStretch(1)
        self._primary_btn = QPushButton("セットアップを始める")
        self._primary_btn.setProperty("class", "primary")   # グローバルのアクセント塗りボタンにする
        self._primary_btn.setDefault(True)
        self._primary_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._primary_btn.clicked.connect(self._on_primary)
        footer.addWidget(self._primary_btn)
        island_v.addLayout(footer)

        # ログイン待ち/交換中はステータスをポーリングして表示を最新化する
        # （deep link はブラウザ→OS→アプリと非同期に戻ってくるため）
        self._login_poll = QTimer(self)
        self._login_poll.setInterval(800)
        self._login_poll.timeout.connect(self._refresh_login_status)

        self._update_nav()

    # ------------------------------------------------------------------
    # ページ生成
    # ------------------------------------------------------------------

    def _build_welcome_page(self) -> QWidget:
        return _make_page(
            heading="録音キーを押して話すだけ",
            body=(
                "録音キーを押して話すだけ。離すとカーソル位置に文字が入ります。\n\n"
                "はじめにログインと使い方を確認します。数十秒で終わります。"
            ),
            secondary=self._c.SECONDARY_TEXT,
        )

    def _build_login_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(32, 28, 32, 28)
        layout.setSpacing(14)

        heading = QLabel("ログイン")
        heading.setStyleSheet("font-size: 20px; font-weight: 700;")
        layout.addWidget(heading)

        desc = QLabel(
            "ログインすると無料体験で文字起こしが使えます。ログインはブラウザで行います。"
            "あとで設定画面からでも可能です。"
        )
        desc.setWordWrap(True)
        desc.setStyleSheet(f"color: {self._c.SECONDARY_TEXT};")
        layout.addWidget(desc)

        # ログイン状態の表示行（未ログイン/待ち/処理中/済み）
        self._login_status = QLabel("")
        self._login_status.setWordWrap(True)
        layout.addWidget(self._login_status)

        self._login_btn = QPushButton("ブラウザでログイン")
        self._login_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._login_btn.clicked.connect(self._on_login_clicked)
        row = QHBoxLayout()
        row.addWidget(self._login_btn)
        row.addStretch(1)
        layout.addLayout(row)

        layout.addStretch(1)
        return page

    def _build_done_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(32, 28, 32, 28)
        layout.setSpacing(14)

        heading = QLabel("準備ができました")
        heading.setStyleSheet("font-size: 20px; font-weight: 700;")
        layout.addWidget(heading)

        desc = QLabel(
            "下の録音キーを押して話すと、その場に文字が入ります。"
            "設定はタスクトレイのアイコンからいつでも変えられます。"
        )
        desc.setWordWrap(True)
        desc.setStyleSheet(f"color: {self._c.SECONDARY_TEXT};")
        layout.addWidget(desc)

        # 実際の既定設定を settings.yaml から読んで表示する（ハードコードしない）
        layout.addWidget(self._usage_card("録音キー 1（メイン）", self._config.get("hotkey1", {})))
        layout.addWidget(self._usage_card("録音キー 2（サブ）", self._config.get("hotkey2", {})))
        layout.addStretch(1)
        return page

    def _usage_card(self, slot_label: str, slot: dict) -> QWidget:
        """録音キー 1 つ分の使い方カード（キー表記・文字起こしモード・録音のしかた）。"""
        hotkey = str(slot.get("hotkey", "") or "未設定")
        backend = str(slot.get("backend", "") or "").lower()
        mode = str(slot.get("hotkey_mode", "") or "").lower()
        backend_label = _BACKEND_LABELS.get(backend, "スタンダード")
        mode_label = _MODE_LABELS.get(mode, mode)

        card = QFrame()
        card.setObjectName("card")   # グローバルの QFrame#card（ガラスカード）スタイルを適用
        cl = QVBoxLayout(card)
        cl.setContentsMargins(14, 12, 14, 12)
        cl.setSpacing(3)
        name = QLabel(slot_label)
        name.setStyleSheet("font-weight: 600;")
        cl.addWidget(name)
        detail = QLabel(f"キー: {hotkey} ／ {backend_label} ／ {mode_label}")
        detail.setStyleSheet(f"color: {self._c.SECONDARY_TEXT};")
        detail.setWordWrap(True)
        cl.addWidget(detail)
        return card

    # ------------------------------------------------------------------
    # ナビゲーション
    # ------------------------------------------------------------------

    def _on_back(self) -> None:
        if self._step > 0:
            self._step -= 1
            self._go_to(self._step)

    def _on_primary(self) -> None:
        if self._step >= 2:
            self._finish()
            return
        self._step += 1
        self._go_to(self._step)

    def _go_to(self, step: int) -> None:
        self._stack.setCurrentIndex(step)
        self._update_nav()
        if step == 1:
            self._refresh_login_status()

    def _update_nav(self) -> None:
        """現在ステップに合わせて戻る/主ボタンの表示・文言を更新する。"""
        self._dots.set_current(self._step)
        self._back_btn.setVisible(self._step > 0)
        if self._step == 0:
            self._primary_btn.setText("セットアップを始める")
        elif self._step == 1:
            # ログイン済みなら「次へ」、未ログインなら「あとで」（スキップ可）
            self._primary_btn.setText("次へ" if self._is_logged_in() else "あとで")
        else:
            self._primary_btn.setText("使い始める")

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
        if self._step == 1:
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
        self.onboarding_finished.emit()

    def reject(self) -> None:
        """Esc/キャンセル。スキップ扱いで完了フラグを立てる（毎回は出さない）。"""
        self._emit_finished_once()
        super().reject()

    def closeEvent(self, event) -> None:
        """× で閉じた場合もスキップ扱いで完了フラグを立てる。"""
        self._emit_finished_once()
        super().closeEvent(event)


class _StepDots(QWidget):
    """ステップ進行を示す小さなドット列（現在＝アクセント／通過＝半透明）。"""

    def __init__(self, count: int, parent=None) -> None:
        super().__init__(parent)
        self._current = 0
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self._dots = []
        for _ in range(count):
            dot = QLabel()
            dot.setFixedSize(8, 8)
            self._dots.append(dot)
            layout.addWidget(dot)
        self.set_current(0)

    def set_current(self, current: int) -> None:
        self._current = current
        for i, dot in enumerate(self._dots):
            if i == current:
                color = "#0A84FF"
            elif i < current:
                color = "rgba(10,132,255,0.5)"
            else:
                color = "rgba(127,127,127,0.35)"
            dot.setStyleSheet(f"background: {color}; border-radius: 4px;")


def _make_page(heading: str, body: str, secondary: str) -> QWidget:
    """見出し + 本文だけの単純ページ（ようこそ用）。secondary は本文の二次テキスト色。"""
    page = QWidget()
    layout = QVBoxLayout(page)
    layout.setContentsMargins(32, 28, 32, 28)
    layout.setSpacing(14)
    h = QLabel(heading)
    h.setStyleSheet("font-size: 20px; font-weight: 700;")
    h.setWordWrap(True)
    layout.addWidget(h)
    b = QLabel(body)
    b.setWordWrap(True)
    b.setStyleSheet(f"color: {secondary};")
    layout.addWidget(b)
    layout.addStretch(1)
    return page


def _hline() -> QFrame:
    """水平の区切り線（グローバルの QFrame#hairline スタイルを使う）。"""
    line = QFrame()
    line.setObjectName("hairline")
    line.setFixedHeight(1)
    return line
