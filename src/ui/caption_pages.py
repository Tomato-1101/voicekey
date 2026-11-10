"""
設定画面の「字幕と議事録」= ライブ字幕 / 議事録・会議 のページ（Windows）

Mac の CaptionSettingsTab.swift / MeetBotSettingsTab.swift と同じ並び・同じ文言にそろえる
（Windows に無い Apple の端末内翻訳・読み上げの項目は出さない）。設定の置き場所は設定画面に集約し、
トレイのメニューは開始・停止などの入口だけにする。

値の保存は他のページと同じく「保存」ボタン（settings.yaml の `caption:` / `meet_bot:`）。
ただし「字幕を開始」「参加」を押したときは、そのページの値をその場で保存してから動かす
（変えた設定で動かないと、押した人には壊れて見えるため）。

字幕サービス・ボットは app が遅延生成する（ディクテーションの経路に 1ms も足さない）。
ページはそれを `caption_host`（app）経由で受け取り、表示のためには作らない。
"""

import os
from typing import Optional

from PySide6.QtCore import QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (
    QComboBox, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QSlider, QVBoxLayout, QWidget,
)
from PySide6.QtCore import Qt

from ..caption import settings as cs
from ..config import llm_models
from ..utils import secrets

# 状態表示の更新間隔（ページを開いている間だけ）
_STATUS_REFRESH_MSEC = 1000


def open_path(path) -> None:
    """フォルダ・ファイルを既定のアプリで開く（フォルダが無ければ作る）。"""
    from pathlib import Path

    target = Path(path)
    if target.suffix == "":
        target.mkdir(parents=True, exist_ok=True)
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))


def key_status_text(item: str) -> str:
    """キーの状態だけ（値は絶対に出さない）。"""
    source = secrets.api_key_source(item)
    labels = {
        secrets.SOURCE_APP: "設定済み（アプリに保存）",
        secrets.SOURCE_ENVIRONMENT: "設定済み（環境変数）",
        secrets.SOURCE_CENTRAL: "設定済み（資格情報マネージャー・共有）",
    }
    return labels.get(source, "未設定") if source else "未設定"


class CaptionPagesMixin:
    """SettingsWindow に混ぜる「ライブ字幕」「議事録・会議」ページ。

    Attributes:
        caption_host: app（`caption_service(create)` / `meet_bot(create)` を持つ）。None なら操作できない
    """

    caption_host = None

    # ------------------------------------------------------------------
    # ライブ字幕
    # ------------------------------------------------------------------

    def _create_caption_page(self) -> QWidget:
        from .settings_window import _add_row, _make_caption, _make_card

        page, layout = self._new_page()

        # 状態
        layout.addWidget(self._section_title("状態"))
        card, cl = _make_card()
        self._caption_state_label = QLabel("停止中")
        self._caption_state_detail = _make_caption("")
        self._caption_toggle_btn = QPushButton("字幕を開始")
        self._caption_toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._caption_toggle_btn.clicked.connect(self._on_caption_toggle)
        state_box = QWidget()
        sb = QVBoxLayout(state_box)
        sb.setContentsMargins(0, 0, 0, 0)
        sb.setSpacing(3)
        row = QHBoxLayout()
        row.addWidget(self._caption_state_label)
        row.addStretch()
        row.addWidget(self._caption_toggle_btn)
        sb.addLayout(row)
        sb.addWidget(self._caption_state_detail)
        from .settings_window import _add_block

        _add_block(cl, state_box)
        self._caption_autostart_check = self._make_toggle()
        _add_row(cl, "起動時に字幕を自動開始", self._caption_autostart_check,
                 caption="一度でも字幕を開始したあとから効きます（初回起動でいきなり始めないため）。")
        layout.addWidget(card)

        # モード
        layout.addWidget(self._section_title("モード"))
        card, cl = _make_card()
        self._caption_mode_combo = QComboBox()
        for value, label in cs.MODE_LABELS.items():
            self._caption_mode_combo.addItem(label, value)
        self._caption_mode_caption = _make_caption("")
        self._caption_mode_combo.currentIndexChanged.connect(lambda _i: self._refresh_caption_controls())
        _add_row(cl, "字幕の動作", self._caption_mode_combo, caption_widget=self._caption_mode_caption)
        self._caption_language_combo = QComboBox()
        for value, label in cs.LANGUAGE_LABELS.items():
            self._caption_language_combo.addItem(label, value)
        self._caption_language_row = QWidget()
        lr = QHBoxLayout(self._caption_language_row)
        lr.setContentsMargins(14, 4, 14, 8)
        lr.addWidget(QLabel("認識する言語"))
        lr.addStretch()
        lr.addWidget(self._caption_language_combo)
        cl.addWidget(self._caption_language_row)
        layout.addWidget(card)

        # 議事録
        layout.addWidget(self._section_title("議事録（文字起こしの保存）"))
        card, cl = _make_card()
        self._caption_save_check = self._make_toggle()
        _add_row(cl, "文字起こしを自動で保存する", self._caption_save_check,
                 caption="字幕が動いている間、確定した文字起こしを Markdown に追記します"
                         "（訳文は保存しません）。5 分以上あくと別のファイルになります。")
        open_btn = QPushButton("保存先を開く")
        open_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        open_btn.clicked.connect(lambda: open_path(self._transcripts_dir()))
        _add_row(cl, "保存先", open_btn, caption=str(self._transcripts_dir()))
        layout.addWidget(card)

        # 認識と翻訳
        layout.addWidget(self._section_title("翻訳"))
        card, cl = _make_card()
        self._caption_engine_button = self._make_grouped_choice()
        self._caption_engine_button.setMinimumWidth(240)
        self._caption_engine_caption = _make_caption("")
        self._caption_choice = (cs.ENGINE_GROQ, cs.DEFAULT_GROQ_MODEL, cs.DEFAULT_GEMINI_MODEL)
        self._caption_engine_button.chosen.connect(self._set_caption_engine_choice)
        self._caption_engine_row = QWidget()
        er = QVBoxLayout(self._caption_engine_row)
        er.setContentsMargins(0, 0, 0, 0)
        _add_row(er, "翻訳エンジン", self._caption_engine_button, caption_widget=self._caption_engine_caption)
        cl.addWidget(self._caption_engine_row)
        self._caption_recognizer_combo = QComboBox()
        for value, label in cs.RECOGNIZER_LABELS.items():
            self._caption_recognizer_combo.addItem(label, value)
        self._caption_recognizer_row = QWidget()
        rr = QVBoxLayout(self._caption_recognizer_row)
        rr.setContentsMargins(0, 0, 0, 0)
        _add_row(rr, "文字起こしエンジン", self._caption_recognizer_combo,
                 caption="パソコンの音を API へ送って文字にします（Windows では端末内の認識は使いません）。"
                         "Soniox は安く速いのでおすすめです。")
        cl.addWidget(self._caption_recognizer_row)
        layout.addWidget(card)

        # 表示と対象
        layout.addWidget(self._section_title("表示と対象"))
        card, cl = _make_card()
        self._caption_frontmost_check = self._make_toggle()
        _add_row(cl, "最前面のアプリだけを翻訳", self._caption_frontmost_check,
                 caption="裏で流している音楽などを字幕に混ぜないための既定です。"
                         "古い Windows でアプリ単位に拾えないときは、パソコン全体の音を拾います。")
        self._caption_source_check = self._make_toggle()
        _add_row(cl, "英語の原文も表示", self._caption_source_check)
        self._caption_scale_slider = QSlider(Qt.Orientation.Horizontal)
        self._caption_scale_slider.setRange(7, 20)  # 70〜200%（10% 刻み）
        self._caption_scale_slider.setFixedWidth(180)
        self._caption_scale_label = _make_caption("")
        self._caption_scale_slider.valueChanged.connect(self._on_caption_scale_changed)
        _add_row(cl, "字幕の大きさ", self._caption_scale_slider, caption_widget=self._caption_scale_label)
        layout.addWidget(card)

        # API キー（状態だけ）
        layout.addWidget(self._section_title("API キー"))
        card, cl = _make_card()
        self._caption_key_labels = {}
        for item, name in (("soniox", "Soniox"), ("openai", "OpenAI"), ("groq", "Groq"), ("gemini", "Gemini")):
            label = QLabel("")
            label.setObjectName("caption")
            self._caption_key_labels[item] = label
            _add_row(cl, name, label)
        layout.addWidget(card)
        layout.addWidget(_make_caption("キーは「API キー」ページで入力できます。ここでは状態だけを表示します"
                                       "（アプリに保存した値 → 環境変数 → 資格情報マネージャーの順に読みます）。"))
        layout.addStretch()

        self._caption_status_timer = QTimer(self)
        self._caption_status_timer.setInterval(_STATUS_REFRESH_MSEC)
        self._caption_status_timer.timeout.connect(self._refresh_caption_pages_status)
        self._caption_status_timer.start()
        return self._wrap_scroll(page)

    def _make_grouped_choice(self):
        from .settings_widgets import GroupedChoiceButton

        return GroupedChoiceButton()

    @staticmethod
    def _transcripts_dir():
        from ..caption.transcript_recorder import transcripts_dir

        return transcripts_dir()

    def _set_caption_engine_choice(self, value) -> None:
        engine, model = value
        _, groq_model, gemini_model = self._caption_choice
        if engine == cs.ENGINE_GROQ:
            groq_model = model
        elif engine == cs.ENGINE_GEMINI:
            gemini_model = model
        self._caption_choice = (engine, groq_model, gemini_model)
        self._refresh_caption_controls()

    def _render_caption_engine(self) -> None:
        engine, groq_model, gemini_model = self._caption_choice
        groups = [
            (None, [((cs.ENGINE_SONIOX, ""), "Soniox（認識と翻訳をまとめて・最速 約 $0.12/時）")]),
            ("Groq（確定した文だけ送る）",
             [((cs.ENGINE_GROQ, mid), label) for mid, label in llm_models.options(llm_models.GROQ, groq_model)]),
            ("Gemini（有料の場合あり）",
             [((cs.ENGINE_GEMINI, mid), label) for mid, label in llm_models.options(llm_models.GEMINI, gemini_model)]),
        ]
        current_model = {cs.ENGINE_GROQ: groq_model, cs.ENGINE_GEMINI: gemini_model}.get(engine, "")
        self._caption_engine_button.set_groups(groups, (engine, current_model), cs.engine_title(engine, current_model))
        common = ("Groq / Gemini は確定した文だけを送って訳します（失敗したときは原文のまま出します）。"
                  "Groq の既定は単価がいちばん安い gpt-oss-20b です。")
        if engine == cs.ENGINE_SONIOX:
            detail = ("Soniox は音声を送り続けて認識と翻訳を同時に行います（最速）。字幕を動かしている間、"
                      "声が出ている時間ぶん課金されます（約 $0.12/時）。無音が 30 秒続くと接続を切って課金を止め、"
                      "声が戻ったら自動でつなぎ直します。キーは「API キー」の Soniox を使います。")
        elif engine == cs.ENGINE_GEMINI:
            detail = common + "\n" + llm_models.detail(llm_models.GEMINI, gemini_model)
        else:
            detail = common + "\n" + llm_models.detail(llm_models.GROQ, groq_model)
        self._caption_engine_caption.setText(detail)

    def _refresh_caption_controls(self) -> None:
        """モード・エンジンに応じて、出す行と説明を切り替える。"""
        mode = self._caption_mode_combo.currentData() or cs.MODE_TRANSLATE
        transcribing = mode == cs.MODE_TRANSCRIBE
        self._caption_mode_caption.setText(
            "パソコンの音を API で文字起こしします。翻訳はしません。" if transcribing
            else "英語を聞いて日本語に訳します（認識は英語で固定）。"
        )
        self._caption_language_row.setVisible(transcribing)
        self._caption_engine_row.setVisible(not transcribing)
        engine = self._caption_choice[0]
        self._caption_recognizer_row.setVisible(transcribing or engine != cs.ENGINE_SONIOX)
        self._render_caption_engine()

    def _on_caption_scale_changed(self, value: int) -> None:
        self._caption_scale_label.setText(f"{value * 10}%。位置は録音ピルの真上に固定です（大きさだけ変えられます）。")
        # 動いている字幕に即反映する（見ながら合わせられるように。保存は「保存」で）
        service = self._caption_service(create=False)
        if service is not None:
            service.set_hud_scale(value / 10.0)

    def _load_caption_settings(self, config) -> None:
        """settings.yaml の値をページへ入れる（_load_current_settings から呼ぶ）。"""
        cfg = cs.CaptionConfig.from_config(config)
        self._caption_autostart_check.setChecked(cfg.auto_start)
        self._caption_mode_combo.setCurrentIndex(max(0, self._caption_mode_combo.findData(cfg.mode)))
        self._caption_language_combo.setCurrentIndex(max(0, self._caption_language_combo.findData(cfg.language)))
        self._caption_save_check.setChecked(cfg.saves_transcript)
        self._caption_choice = (cfg.engine, cfg.groq_model, cfg.gemini_model)
        self._caption_recognizer_combo.setCurrentIndex(
            max(0, self._caption_recognizer_combo.findData(cfg.recognizer)))
        self._caption_frontmost_check.setChecked(cfg.scope == cs.SCOPE_FRONTMOST)
        self._caption_source_check.setChecked(cfg.shows_source)
        self._caption_scale_slider.setValue(int(round(cfg.hud_scale * 10)))
        self._on_caption_scale_changed(self._caption_scale_slider.value())
        self._refresh_caption_controls()
        meet = cs.MeetBotConfig.from_config(config)
        self._meet_source_combo.setCurrentIndex(max(0, self._meet_source_combo.findData(meet.source)))
        self._meet_language_combo.setCurrentIndex(max(0, self._meet_language_combo.findData(meet.language)))
        self._meet_recognizer_combo.setCurrentIndex(max(0, self._meet_recognizer_combo.findData(meet.recognizer)))
        self._refresh_meet_controls()
        self._refresh_caption_pages_status()

    def _collect_caption_config(self) -> dict:
        """ページの値を settings.yaml の `caption:` / `meet_bot:` 用にまとめる。"""
        engine, groq_model, gemini_model = self._caption_choice
        return {
            "caption": {
                "mode": self._caption_mode_combo.currentData() or cs.MODE_TRANSLATE,
                "language": self._caption_language_combo.currentData() or cs.LANG_JA,
                "engine": engine,
                "recognizer": self._caption_recognizer_combo.currentData() or cs.RECOGNIZER_SONIOX,
                "groq_model": groq_model,
                "gemini_model": gemini_model,
                "save_transcript": self._caption_save_check.isChecked(),
                "show_source": self._caption_source_check.isChecked(),
                "hud_scale": self._caption_scale_slider.value() / 10.0,
                "scope": cs.SCOPE_FRONTMOST if self._caption_frontmost_check.isChecked() else cs.SCOPE_ALL,
                "auto_start": self._caption_autostart_check.isChecked(),
            },
            "meet_bot": {
                "source": self._meet_source_combo.currentData() or cs.MEET_SOURCE_CAPTIONS,
                "language": self._meet_language_combo.currentData() or cs.LANG_JA,
                "recognizer": self._meet_recognizer_combo.currentData() or cs.RECOGNIZER_SONIOX,
            },
        }

    def _save_caption_section(self) -> None:
        """このページの値だけをその場で保存する（開始・参加の前）。"""
        try:
            self._config_manager.save(self._collect_caption_config())
        except Exception:
            pass

    def _caption_service(self, create: bool):
        host = self.caption_host
        if host is None:
            return None
        try:
            return host.caption_service(create)
        except Exception:
            return None

    def _meet_bot(self, create: bool):
        host = self.caption_host
        if host is None:
            return None
        try:
            return host.meet_bot(create)
        except Exception:
            return None

    def _on_caption_toggle(self) -> None:
        service = self._caption_service(create=False)
        if service is not None and service.is_active:
            service.stop()
        else:
            # 変えた設定で動かす（押した時点のページの値を保存してから開始）
            self._save_caption_section()
            service = self._caption_service(create=True)
            if service is None:
                QMessageBox.information(self, "ライブ字幕", "この画面からは字幕を開始できません（アプリ本体から開いてください）。")
                return
            service.apply_config()
            service.start()
        self._refresh_caption_pages_status()

    # ------------------------------------------------------------------
    # 議事録・会議
    # ------------------------------------------------------------------

    def _create_meeting_page(self) -> QWidget:
        from .settings_window import _add_block, _add_row, _make_caption, _make_card

        page, layout = self._new_page()

        # ログイン（初めての人が上から順にたどれる並び: ログイン → 参加 → 取り方 → 議事録）
        layout.addWidget(self._section_title("ボット用ブラウザのログイン"))
        card, cl = _make_card()
        self._meet_login_label = QLabel("未確認")
        self._meet_login_checked = _make_caption("")
        self._meet_login_btn = QPushButton("ログイン画面を開く")
        self._meet_login_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._meet_login_btn.clicked.connect(self._on_meet_login)
        box = QWidget()
        bl = QVBoxLayout(box)
        bl.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        row.addWidget(self._meet_login_label)
        row.addStretch()
        row.addWidget(self._meet_login_btn)
        bl.addLayout(row)
        bl.addWidget(self._meet_login_checked)
        _add_block(cl, box)
        layout.addWidget(card)
        layout.addWidget(_make_caption(
            "ボットは普段の Chrome とは別のブラウザで動くので、最初に 1 回だけ Google にログインしてください。"
            "ログイン画面のウィンドウを閉じると、ログインできたかを確かめます。"
            "会議ではこのアカウント名で表示されるので、ボット用に別の Google アカウントを作って使うのがおすすめです。"
        ))

        # 会議に参加
        layout.addWidget(self._section_title("会議に参加"))
        card, cl = _make_card()
        self._meet_chrome_warning = _make_caption("Google Chrome が必要です。インストールすると使えるようになります。")
        self._meet_chrome_warning.setVisible(False)
        cl.addWidget(self._meet_chrome_warning)
        self._meet_status_label = QLabel("参加していません")
        self._meet_status_detail = _make_caption("")
        status_box = QWidget()
        stl = QVBoxLayout(status_box)
        stl.setContentsMargins(0, 0, 0, 0)
        stl.setSpacing(3)
        stl.addWidget(self._meet_status_label)
        stl.addWidget(self._meet_status_detail)
        _add_block(cl, status_box)
        self._meet_url_input = QLineEdit()
        self._meet_url_input.setPlaceholderText("https://meet.google.com/abc-defg-hij")
        self._meet_url_input.returnPressed.connect(self._on_meet_join)
        self._meet_url_input.textChanged.connect(lambda _t: self._refresh_caption_pages_status())
        self._meet_join_btn = QPushButton("参加")
        self._meet_join_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._meet_join_btn.setProperty("class", "primary")
        self._meet_join_btn.clicked.connect(self._on_meet_join)
        self._meet_join_row = QWidget()
        jr = QVBoxLayout(self._meet_join_row)
        jr.setContentsMargins(14, 6, 14, 8)
        jr.setSpacing(5)
        jh = QHBoxLayout()
        jh.addWidget(self._meet_url_input, 1)
        jh.addWidget(self._meet_join_btn)
        jr.addLayout(jh)
        jr.addWidget(_make_caption("見えない Chrome が、上でログインした Google アカウントの名前で"
                                   "マイクとカメラをオフにして入ります。ほかの人の会議ではホストの承認が要ります。"))
        cl.addWidget(self._meet_join_row)
        self._meet_leave_btn = QPushButton("会議から退出")
        self._meet_leave_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._meet_leave_btn.clicked.connect(self._on_meet_leave)
        self._meet_leave_row = QWidget()
        lv = QVBoxLayout(self._meet_leave_row)
        lv.setContentsMargins(0, 0, 0, 0)
        _add_row(lv, "退出すると議事録を閉じます", self._meet_leave_btn,
                 caption="会議が終わったり外されたりしたときは自動で閉じます。")
        cl.addWidget(self._meet_leave_row)
        self._meet_needs_login_btn = QPushButton("まず Google にログイン")
        self._meet_needs_login_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._meet_needs_login_btn.clicked.connect(self._on_meet_login)
        self._meet_needs_login_row = QWidget()
        nl = QVBoxLayout(self._meet_needs_login_row)
        nl.setContentsMargins(0, 0, 0, 0)
        _add_row(nl, "参加の前にログインが要ります", self._meet_needs_login_btn,
                 caption="ボット用のブラウザで Google に 1 回ログインすると、会議の URL を入れて参加できるようになります。")
        cl.addWidget(self._meet_needs_login_row)
        layout.addWidget(card)

        # 文字起こしの方法
        layout.addWidget(self._section_title("文字起こしの方法"))
        card, cl = _make_card()
        self._meet_source_combo = QComboBox()
        for value, label in cs.MEET_SOURCE_LABELS.items():
            self._meet_source_combo.addItem(label, value)
        self._meet_source_caption = _make_caption("")
        self._meet_source_combo.currentIndexChanged.connect(lambda _i: self._refresh_meet_controls())
        _add_row(cl, "取り方", self._meet_source_combo, caption_widget=self._meet_source_caption)
        self._meet_language_combo = QComboBox()
        for value, label in cs.LANGUAGE_LABELS.items():
            self._meet_language_combo.addItem(label, value)
        _add_row(cl, "会議の言語", self._meet_language_combo,
                 caption="会議で話される言語を選んでください。ライブ字幕の設定とは別です。")
        self._meet_recognizer_combo = QComboBox()
        for value, label in cs.RECOGNIZER_LABELS.items():
            self._meet_recognizer_combo.addItem(label, value)
        self._meet_recognizer_row = QWidget()
        mr = QVBoxLayout(self._meet_recognizer_row)
        mr.setContentsMargins(0, 0, 0, 0)
        _add_row(mr, "文字起こしエンジン", self._meet_recognizer_combo)
        cl.addWidget(self._meet_recognizer_row)
        self._meet_locked_caption = _make_caption("参加中は変えられません。次に参加したときから効きます。")
        cl.addWidget(self._meet_locked_caption)
        layout.addWidget(card)

        # 議事録
        layout.addWidget(self._section_title("議事録"))
        card, cl = _make_card()
        cl.addWidget(_make_caption("ボットの議事録は会議ごとに 1 ファイルで必ず保存されます（参加から退出まで）。"))
        self._meet_save_error = _make_caption("")
        self._meet_save_error.setVisible(False)
        cl.addWidget(self._meet_save_error)
        buttons = QWidget()
        bh = QHBoxLayout(buttons)
        bh.setContentsMargins(0, 0, 0, 0)
        latest_btn = QPushButton("最新の議事録を開く")
        latest_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        latest_btn.clicked.connect(self._open_latest_transcript)
        folder_btn = QPushButton("保存フォルダを開く")
        folder_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        folder_btn.clicked.connect(lambda: open_path(self._transcripts_dir()))
        bh.addWidget(latest_btn)
        bh.addWidget(folder_btn)
        _add_row(cl, "保存先", buttons, caption=str(self._transcripts_dir()))
        layout.addWidget(card)
        layout.addStretch()
        return self._wrap_scroll(page)

    def _open_latest_transcript(self) -> None:
        from ..caption.transcript_recorder import latest_file

        latest = latest_file()
        open_path(latest if latest is not None else self._transcripts_dir())

    def _refresh_meet_controls(self) -> None:
        source = self._meet_source_combo.currentData() or cs.MEET_SOURCE_CAPTIONS
        self._meet_source_caption.setText(
            "Meet の字幕を読みます。話者名が残ります（認識は Google 側・追加の料金はかかりません）。"
            if source == cs.MEET_SOURCE_CAPTIONS else
            "ボット用 Chrome の音だけを拾って API で文字にします（Soniox なら約 $0.12/時）。"
            "話者名は会議画面から読めたときだけです。始めるとライブ字幕は止まります。"
        )
        self._meet_recognizer_row.setVisible(source == cs.MEET_SOURCE_API)

    def _on_meet_login(self) -> None:
        bot = self._meet_bot(create=True)
        if bot is not None:
            bot.show_login_window()
        self._refresh_caption_pages_status()

    def _on_meet_join(self) -> None:
        from ..caption.meetbot import rules
        from ..caption.meetbot.service import needs_login

        bot = self._meet_bot(create=True)
        if bot is None or bot.is_active:
            return
        if rules.normalized_meet_url(self._meet_url_input.text()) is None or needs_login(bot.login_status):
            return
        # 押した時点の取り方・言語で参加する
        self._save_caption_section()
        bot.join(self._meet_url_input.text())
        self._refresh_caption_pages_status()

    def _on_meet_leave(self) -> None:
        bot = self._meet_bot(create=False)
        if bot is not None:
            bot.leave()
        self._refresh_caption_pages_status()

    def _prefill_meet_url(self) -> None:
        """会議 URL は直前にコピーしていることが多いので、入っていれば初期値にする。"""
        from ..caption.meetbot import rules

        if self._meet_url_input.text():
            return
        try:
            text = QGuiApplication.clipboard().text() or ""
        except Exception:
            text = ""
        if rules.normalized_meet_url(text):
            self._meet_url_input.setText(text.strip())

    # ------------------------------------------------------------------
    # 状態の表示（ページを開いている間 1 秒ごと）
    # ------------------------------------------------------------------

    def _refresh_caption_pages_status(self) -> None:
        if not hasattr(self, "_caption_state_label") or not hasattr(self, "_meet_status_label"):
            return
        if not self.isVisible():
            return
        # 開いているのがこの 2 ページのときだけ読み直す（キーの状態の読み出しを毎秒回さない）
        nav = getattr(self, "_nav", None)
        titles = getattr(self, "_nav_titles", [])
        if nav is not None and 0 <= nav.currentRow() < len(titles):
            if titles[nav.currentRow()] not in ("ライブ字幕", "議事録・会議"):
                return
        # ライブ字幕
        service = self._caption_service(create=False)
        if service is None:
            title, lines, active = "停止中", [], False
        else:
            title, lines, active = service.title, service.status_lines(), service.is_active
        self._caption_state_label.setText(title)
        self._caption_state_detail.setText("\n".join(lines) or "トレイのメニューからも開始・停止できます。")
        self._caption_toggle_btn.setText("字幕を停止" if active else "字幕を開始")
        self._caption_toggle_btn.setEnabled(self.caption_host is not None)
        for item, label in getattr(self, "_caption_key_labels", {}).items():
            label.setText(key_status_text(item))

        # 議事録・会議
        from ..caption.meetbot import cdp as meet_cdp
        from ..caption.meetbot import rules
        from ..caption.meetbot.service import LOGIN_TITLES, needs_login

        chrome = meet_cdp.is_chrome_installed()
        bot = self._meet_bot(create=False)
        meet_cfg = cs.MeetBotConfig.from_config(self._config_manager)
        if bot is not None:
            login_status, checked_at = bot.login_status, bot.login_checked_at
            status_line, state, is_active = bot.status_line, bot.state, bot.is_active
            lines, transcript, save_error = bot.recorded_lines, bot.current_transcript, bot.save_error
        else:
            login_status = ("loggedIn" if meet_cfg.logged_in else "loggedOut") if meet_cfg.login_checked_at else "unknown"
            checked_at = meet_cfg.login_checked_at
            status_line, state, is_active, lines, transcript, save_error = "参加していません", "idle", False, 0, None, None
        self._meet_chrome_warning.setVisible(not chrome)
        self._meet_login_label.setText(LOGIN_TITLES.get(login_status, "未確認"))
        self._meet_login_checked.setText(f"最終確認: {checked_at.replace('T', ' ')}" if checked_at else "")
        self._meet_login_btn.setEnabled(chrome and not is_active and self.caption_host is not None)
        self._meet_status_label.setText(status_line)
        self._meet_status_detail.setText(_meet_status_detail(state, lines, transcript))
        self._meet_leave_row.setVisible(is_active)
        login_needed = not is_active and needs_login(login_status)
        self._meet_needs_login_row.setVisible(login_needed)
        self._meet_needs_login_btn.setEnabled(chrome and self.caption_host is not None)
        self._meet_join_row.setVisible(not is_active and not login_needed)
        if not is_active and not login_needed:
            self._prefill_meet_url()
        url_ok = rules.normalized_meet_url(self._meet_url_input.text()) is not None
        self._meet_join_btn.setEnabled(chrome and url_ok and self.caption_host is not None)
        for widget in (self._meet_source_combo, self._meet_language_combo, self._meet_recognizer_combo):
            widget.setEnabled(not is_active)
        self._meet_locked_caption.setVisible(is_active)
        self._meet_save_error.setVisible(bool(save_error))
        self._meet_save_error.setText(f"保存に失敗しています: {save_error}" if save_error else "")


def _meet_status_detail(state: str, lines: int, transcript) -> str:
    """状態の下に出す補足（次に何をすればいいか・Mac と同じ文言）。"""
    if state == "idle":
        return "会議の URL を入れて「参加」を押してください。"
    if state in ("launching", "joining"):
        return "Chrome を裏で起動して会議に入っています。数十秒かかることがあります。"
    if state == "waitingForApproval":
        return "会議の画面でホストが「承認」を押すと記録が始まります（最大 10 分待ちます）。"
    if state == "recording":
        if transcript:
            return f"保存先: {os.path.basename(str(transcript))}"
        return "発言があると議事録に書き始めます。"
    if state == "ended":
        return f"議事録に {lines} 行を保存しました。" if lines > 0 else "議事録に残った発言はありませんでした。"
    return "直したら、もう一度「参加」を押してください。"
