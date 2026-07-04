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
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .styles import MacTheme
from ..utils.logger import get_logger

logger = get_logger(__name__)

# ステップ番号（QStackedWidget のインデックスと一致）。
# 権限ゲートの無い Windows は 権限ステップを持たず、ログインの後ろに体験（練習）を足す。
_STEP_WELCOME = 0
_STEP_LOGIN = 1
_STEP_PRACTICE_BASIC = 2       # 体験1: 即時入力
_STEP_PRACTICE_HANDSFREE = 3   # 体験2: ハンズフリー（トグル）
_STEP_PRACTICE_FORMAT = 4      # 体験3: 文章整形（フィラーが消える）
_STEP_FEATURE_TOUR = 5         # 機能ツアー（紹介のみ）
_STEP_DONE = 6                 # 完了
_PRACTICE_STEPS = (_STEP_PRACTICE_BASIC, _STEP_PRACTICE_HANDSFREE, _STEP_PRACTICE_FORMAT)


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
    """初回セットアップの案内ウィンドウ（3 ステップ）。"""

    # 完了（またはスキップ）時に 1 度だけ発火。app が完了フラグ保存に使う
    onboarding_finished = Signal()

    def __init__(self, config_manager, app=None, parent=None) -> None:
        super().__init__(parent)
        self._config = config_manager
        # 体験ステップから整形オーバーライドを操作するための本体参照（テスト時は None）
        self._app = app
        self._finished_emitted = False
        self._step = 0
        # 練習ページの部品（step → {"edit", "success", "login_note"}）。成功演出・自動フォーカスに使う
        self._practice_pages: dict = {}

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
        self._dots = _StepDots(count=_STEP_DONE + 1)
        header.addWidget(self._dots, alignment=Qt.AlignmentFlag.AlignCenter)
        island_v.addLayout(header)
        island_v.addWidget(_hline())

        # --- 本文（権限なし・ログイン → 体験 → 完了の 7 ステップをスタック） ---
        self._stack = QStackedWidget()
        self._stack.addWidget(self._build_welcome_page())                       # 0
        self._stack.addWidget(self._build_login_page())                         # 1
        self._stack.addWidget(self._build_practice_basic_page())                # 2
        self._stack.addWidget(self._build_practice_handsfree_page())            # 3
        self._stack.addWidget(self._build_practice_format_page())               # 4
        self._stack.addWidget(self._build_feature_tour_page())                  # 5
        self._stack.addWidget(self._build_done_page())                          # 6
        island_v.addWidget(self._stack, 1)

        # --- フッター（戻る / スキップ / 主ボタン） ---
        island_v.addWidget(_hline())
        footer = QHBoxLayout()
        footer.setContentsMargins(20, 12, 20, 16)
        footer.setSpacing(8)
        self._back_btn = QPushButton("戻る")
        self._back_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._back_btn.clicked.connect(self._on_back)
        footer.addWidget(self._back_btn)
        # 体験・機能ツアーは強制しないので、いつでも完了へ飛べる「スキップ」を常設
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
    # 体験ステップ（実際に録音キーを押して入力してみる）
    # ------------------------------------------------------------------

    def _slot_key(self, key: str) -> str:
        """設定から録音キーの表記を取り出す（例: '<cmd_r>'）。未設定は「未設定」。"""
        slot = self._config.get(key, {}) or {}
        return str(slot.get("hotkey", "") or "未設定")

    def _handsfree_key(self) -> str:
        """トグル（ハンズフリー）録音に使う録音キー。既定はサブ(hotkey2・toggle)。

        ユーザーがモードを入れ替えていても toggle のスロットを優先して案内する。
        """
        for key in ("hotkey2", "hotkey1"):
            slot = self._config.get(key, {}) or {}
            if str(slot.get("hotkey_mode", "") or "").lower() == "toggle":
                return self._slot_key(key)
        return self._slot_key("hotkey2")

    def _build_practice_page(
        self, step: int, heading: str, intro: str,
        example: str, prompt: str, success: str, hint: str = "",
    ) -> QWidget:
        """例文カード＋促し文＋自動フォーカスの練習入力欄＋成功演出を 1 枚にまとめた体験ページ。

        録音キーを押して話すと、本体の貼り付けが前面ウィンドウの練習入力欄へテキストを入れる。
        テキストが入った瞬間を textChanged で捕まえ、成功メッセージを出す
        （手入力でも成功扱いにして体験を妨げない）。Mac 版 PracticeCard と同構成。
        """
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(32, 24, 32, 24)
        layout.setSpacing(12)

        h = QLabel(heading)
        h.setStyleSheet("font-size: 20px; font-weight: 700;")
        h.setWordWrap(True)
        layout.addWidget(h)

        intro_label = QLabel(intro)
        intro_label.setWordWrap(True)
        intro_label.setStyleSheet(f"color: {self._c.SECONDARY_TEXT};")
        layout.addWidget(intro_label)

        # ログイン必須の注意（未ログイン時のみ表示。文字起こしはサーバー経由のため）
        login_note = QLabel("体験するにはログインが必要です（前のステップでログインできます）")
        login_note.setWordWrap(True)
        login_note.setStyleSheet(f"color: {self._c.WARNING}; font-weight: 600;")
        login_note.setVisible(False)
        layout.addWidget(login_note)

        # 読む例文（カードに入れて「これを読む」と分かるように）
        example_card = QFrame()
        example_card.setObjectName("card")
        ecl = QVBoxLayout(example_card)
        ecl.setContentsMargins(14, 12, 14, 12)
        ex = QLabel(f"「{example}」")
        ex.setWordWrap(True)
        ex.setStyleSheet("font-size: 15px; font-weight: 500;")
        ecl.addWidget(ex)
        layout.addWidget(example_card)

        # 促し文
        prompt_label = QLabel(prompt)
        prompt_label.setWordWrap(True)
        prompt_label.setStyleSheet("font-weight: 600;")
        layout.addWidget(prompt_label)

        if hint:
            hint_label = QLabel(hint)
            hint_label.setWordWrap(True)
            hint_label.setStyleSheet(f"color: {self._c.SECONDARY_TEXT}; font-size: 12px;")
            layout.addWidget(hint_label)

        # 練習入力欄（自動フォーカス。前面＝このウィンドウなので貼り付けはここに入る）
        edit = QTextEdit()
        edit.setPlaceholderText("ここに文字が入ります")
        edit.setFixedHeight(84)
        layout.addWidget(edit)

        # 成功演出（入力が入ったら緑のメッセージを出す）
        success_label = QLabel(f"✓ {success}")
        success_label.setWordWrap(True)
        success_label.setStyleSheet(f"color: {self._c.SUCCESS}; font-weight: 700;")
        success_label.setVisible(False)
        layout.addWidget(success_label)

        edit.textChanged.connect(
            lambda e=edit, s=success_label: self._on_practice_changed(e, s)
        )

        layout.addStretch(1)
        self._practice_pages[step] = {
            "edit": edit, "success": success_label, "login_note": login_note,
        }
        return page

    def _on_practice_changed(self, edit: QTextEdit, success_label: QLabel) -> None:
        """練習欄にテキストが入ったら成功メッセージを出す（一度出したら消さない）。"""
        if not success_label.isVisible() and _has_practice_input(edit.toPlainText()):
            success_label.setVisible(True)

    def _build_practice_basic_page(self) -> QWidget:
        return self._build_practice_page(
            step=_STEP_PRACTICE_BASIC,
            heading="さっそく使ってみましょう",
            intro="録音キーを押しながら、下の文を声に出して読んでみてください。"
                  "しゃべり終わってキーを離すと、その場に文字が入ります。",
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

    def _build_feature_tour_page(self) -> QWidget:
        """機能ツアー（紹介のみ・カード形式）。2 つ目の録音キー／履歴／統計／HUD。"""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(32, 24, 32, 24)
        layout.setSpacing(12)

        h = QLabel("できることの紹介")
        h.setStyleSheet("font-size: 20px; font-weight: 700;")
        layout.addWidget(h)
        intro = QLabel("よく使う機能です。あとで設定やタスクトレイのアイコンから使えます。")
        intro.setWordWrap(True)
        intro.setStyleSheet(f"color: {self._c.SECONDARY_TEXT};")
        layout.addWidget(intro)

        for title, body in (
            ("2 つ目の録音キー", "録音キー 2 には別のモードを割り当てられます（例: ハンズフリー）。"),
            ("履歴", "入力したテキストを最大 200 件まで自動保存。クリックでもう一度コピーできます。"),
            ("統計", "どのアプリでどれだけ入力したか、節約できた時間が見られます。"),
            ("録音中の表示（HUD）", "画面下の小さなピルが録音中だけ現れ、状態がひと目で分かります。"),
        ):
            layout.addWidget(self._tour_card(title, body))
        layout.addStretch(1)
        return page

    def _tour_card(self, title: str, body: str) -> QWidget:
        """機能ツアー 1 枚分の紹介カード（見出し＋説明）。"""
        card = QFrame()
        card.setObjectName("card")
        cl = QVBoxLayout(card)
        cl.setContentsMargins(14, 12, 14, 12)
        cl.setSpacing(3)
        name = QLabel(title)
        name.setStyleSheet("font-weight: 600;")
        cl.addWidget(name)
        desc = QLabel(body)
        desc.setWordWrap(True)
        desc.setStyleSheet(f"color: {self._c.SECONDARY_TEXT};")
        cl.addWidget(desc)
        return card

    # ------------------------------------------------------------------
    # ナビゲーション
    # ------------------------------------------------------------------

    def _on_back(self) -> None:
        if self._step > 0:
            self._step -= 1
            self._go_to(self._step)

    def _on_primary(self) -> None:
        if self._step >= _STEP_DONE:
            self._finish()
            return
        self._step += 1
        self._go_to(self._step)

    def _on_skip(self) -> None:
        """体験・機能ツアーをスキップして完了ステップへ飛ぶ（体験を強制しない）。"""
        self._step = _STEP_DONE
        self._go_to(self._step)

    def _go_to(self, step: int) -> None:
        self._stack.setCurrentIndex(step)
        self._update_nav()
        if step == _STEP_LOGIN:
            self._refresh_login_status()
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
        """現在ステップに合わせて戻る/スキップ/主ボタンの表示・文言を更新する。"""
        self._dots.set_current(self._step)
        self._back_btn.setVisible(self._step > 0)
        # スキップは体験・機能ツアーのときだけ出す（ようこそ/ログイン/完了では出さない）
        self._skip_btn.setVisible(self._step in _PRACTICE_STEPS or self._step == _STEP_FEATURE_TOUR)
        if self._step == _STEP_WELCOME:
            self._primary_btn.setText("セットアップを始める")
        elif self._step == _STEP_LOGIN:
            # ログイン済みなら「次へ」、未ログインなら「あとで」（スキップ可）
            self._primary_btn.setText("次へ" if self._is_logged_in() else "あとで")
        elif self._step == _STEP_DONE:
            self._primary_btn.setText("使い始める")
        else:
            # 体験・機能ツアーは任意なので「次へ」で常に進める（成功しなくても先へ）
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
        # 整形体験の一時オーバーライドが残っていたら必ず解除（体験ステップ上で閉じた場合の保険）
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
