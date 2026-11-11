"""
一般のページ

見出しの順・行の並び・文言は Mac 版 GeneralSettingsTab と同じ
（入力 / キー操作 / 文章の整形 / 数字入力 / 表示 / サウンド / 起動 / 履歴）。
変えた項目はその場で保存する（入力欄は手が止まってから、スイッチ・選択はすぐ）。

Windows に無い・src に実装が無いので置かない行:
- Dock に表示（Mac 専用）
- 履歴を保存（履歴を止める設定が src に無い）
代わりに Windows だけの行として「外観」（システムに合わせる／ライト／ダーク）を「表示」に置く。
"""

from typing import Callable, List, Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QHBoxLayout, QLineEdit, QPlainTextEdit, QSpinBox, QWidget

from ...core import llm_catalog, text_formatter
from ...core.audio_recorder import AudioRecorder
from ...utils import autostart, secrets
from .. import theme
from ..components import CapsuleButton, MessageLabel, Picker, SettingRow, SettingsGroup, Switch, Text
from ..theme import Metrics, Theme
from .context import PageContext
from .widgets import BlockRow, HotkeyInput, SettingsPage, add_row_message, trailing_box

try:  # pragma: no cover - 同期モジュールが無くても設定画面は開けるようにする（旧画面と同じ）
    from ...core.history_sync import parse_iso_date
except Exception:  # pragma: no cover
    parse_iso_date = None  # type: ignore

# 言語（表示名, 保存値）。空文字は API 側の自動判定。Mac の Picker と同じ 3 択
LANGUAGE_OPTIONS = (("日本語", "ja"), ("英語", "en"), ("自動判定", ""))

# 外観（保存値, 表示名）
APPEARANCE_OPTIONS = (
    (theme.APPEARANCE_SYSTEM, "システムに合わせる"),
    (theme.APPEARANCE_LIGHT, "ライト"),
    (theme.APPEARANCE_DARK, "ダーク"),
)

# マイク自動検出の結果を出しておく時間（Mac と同じ: 成功 2 秒・失敗 4 秒）
_DETECT_OK_MS = 2000
_DETECT_FAIL_MS = 4000

_SYNC_CAPTION = "Cloudflare 上の自分専用 Worker に履歴を送り、Mac で入力した履歴もここに表示します。"


def appearance_from(get: Callable) -> str:
    """
    保存されている外観を読む。

    旧画面はライト/ダークの 2 択を dark_mode に持っていたので、appearance がまだ無いときだけ
    そこから読み替える（True ならダーク、それ以外はシステムに合わせる）。dark_mode には書き戻さない。

    Args:
        get: 設定を読む関数（key, default）
    """
    value = get("appearance", None)
    if value in (theme.APPEARANCE_SYSTEM, theme.APPEARANCE_LIGHT, theme.APPEARANCE_DARK):
        return value
    return theme.APPEARANCE_DARK if get("dark_mode", False) else theme.APPEARANCE_SYSTEM


def _model_label(model: llm_catalog.LLMModel) -> str:
    return f"{model.name}（おすすめ）" if model.recommended else model.name


class _WordRow(QWidget):
    """変換しない語 1 行（語と、右端の削除）。"""

    def __init__(self, word: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.word = word
        layout = QHBoxLayout(self)
        layout.setContentsMargins(
            Metrics.ROW_PADDING_H, Metrics.ROW_PADDING_V, Metrics.ROW_PADDING_H, Metrics.ROW_PADDING_V
        )
        layout.addWidget(Text(word, size=13), 1)
        self.delete_button = CapsuleButton("削除", kind="destructive")
        self.delete_button.setToolTip("この語を削除")
        layout.addWidget(self.delete_button)
        self.setMinimumHeight(Metrics.ROW_MIN_H)


class GeneralPage(SettingsPage):
    """
    一般のページ。

    Attributes:
        language_picker / device_picker / handsfree_input / delay_input / model_picker / prompt_edit /
        numeral_switch / counter_switch / pill_switch / notch_switch / appearance_picker /
        sound_switch / duck_switch / autostart_switch / sync_switch / sync_url_input /
        sync_token_input: 各行の操作部品
    """

    # マイク自動検出の結果（別スレッドから UI のスレッドへ渡す）
    _detect_done = Signal(object)

    def __init__(self, ctx: PageContext, parent: Optional[QWidget] = None) -> None:
        super().__init__("一般", parent)
        self.ctx = ctx
        self._loading = False
        # 自動検出の結果表示を消すタイマーが、次の検出の表示まで消さないように世代で見分ける
        self._detect_generation = 0
        self._token_stored = False

        self._build_input()
        self._build_keys()
        self._build_format()
        self._build_numeral()
        self._build_display()
        self._build_sound()
        self._build_startup()
        self._build_history()
        self.finish()

        self._detect_done.connect(self._on_detect_done)
        self._fill_devices()
        self._token_stored = self._read_token_stored()
        self.load()
        self._connect()

    # ------------------------------------------------------------------
    # 組み立て
    # ------------------------------------------------------------------

    def _build_input(self) -> None:
        self.add_section("入力")
        group = self.add_group()
        self.language_picker = Picker()
        for label, value in LANGUAGE_OPTIONS:
            self.language_picker.addItem(label, value)
        group.add_row(SettingRow("言語", trailing=self.language_picker))

        self.device_picker = Picker()
        self.device_picker.setMinimumWidth(220)
        self.device_picker.setMaximumWidth(300)
        self.reload_devices_button = CapsuleButton("再読込", icon="refresh")
        self.reload_devices_button.setToolTip("デバイス一覧を更新")
        self.detect_button = CapsuleButton("自動検出")
        self.detect_button.setToolTip("全マイクを監視し、喋った声が入ったマイクを自動選択します")
        mic_row = SettingRow(
            "マイク", trailing=trailing_box(self.device_picker, self.reload_devices_button, self.detect_button)
        )
        self.detect_message = add_row_message(mic_row)
        group.add_row(mic_row)

    def _build_keys(self) -> None:
        self.add_section("キー操作")
        group = self.add_group()
        self.handsfree_input = HotkeyInput(platform_adapter=self.ctx.platform, empty_label="クリックして設定")
        self.handsfree_clear = CapsuleButton("クリア")
        group.add_row(SettingRow(
            "ハンズフリーキー",
            "押しながら録音キーを押すと、押しっぱなしにしなくても録音が続きます（もう一度録音キーで停止）。"
            "右 Shift などの修飾キーがおすすめです。",
            trailing_box(self.handsfree_input, self.handsfree_clear),
        ))

        self.repaste_input = HotkeyInput(platform_adapter=self.ctx.platform, empty_label="クリックして設定")
        self.repaste_clear = CapsuleButton("クリア")
        group.add_row(SettingRow(
            "最後の文字起こしを貼り付け",
            "直前に入力したテキストをもう一度貼り付けます。",
            trailing_box(self.repaste_input, self.repaste_clear),
        ))

        self.delay_input = QSpinBox()
        self.delay_input.setRange(0, 2000)
        self.delay_input.setSingleStep(10)
        self.delay_input.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        self.delay_input.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.delay_input.setFixedSize(70, Metrics.FIELD_H)
        group.add_row(SettingRow(
            "ダブルタップ送信の待ち時間",
            "録音キーを素早く 2 回押したとき、貼り付けてから Enter を押すまでの待ち時間です。",
            trailing_box(self.delay_input, Text("ms", size=13, role="secondary"), spacing=6),
        ))

    def _build_format(self) -> None:
        self.add_section("文章の整形")
        group = self.add_group()
        self.model_picker = Picker()
        self.model_picker.setMinimumWidth(220)
        model_row = SettingRow("整形モデル", trailing=self.model_picker)
        self.model_message = add_row_message(model_row, lines=2)
        group.add_row(model_row)

        prompt_row = BlockRow()
        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.addWidget(Text("整形の指示", size=13), 1)
        self.prompt_reset_button = CapsuleButton("既定に戻す")
        head.addWidget(self.prompt_reset_button)
        prompt_row.body.addLayout(head)
        self.prompt_edit = QPlainTextEdit()
        # 行数ぶんの高さで固定する（中身の長さで棚の高さが変わらないように。長い文はスクロール）
        line = self.prompt_edit.fontMetrics().lineSpacing()
        self.prompt_edit.setFixedHeight(line * 8 + 16)
        prompt_row.body.addWidget(self.prompt_edit)
        group.add_row(prompt_row)
        self.add_note("整形するかどうかは「録音キー」の各ページで切り替えます。")

    def _build_numeral(self) -> None:
        self.add_section("数字入力")
        group = self.add_group()
        self.numeral_switch = Switch()
        group.add_row(SettingRow(
            "数字を半角に変換",
            "話した数字を半角のアラビア数字に直します（十二人→12人・二〇二六→2026）。",
            self.numeral_switch,
        ))
        self.counter_switch = Switch()
        group.add_row(SettingRow(
            "助数詞つきの漢数字も変換（三時→3時）",
            "「三時」「十時」「百人」のような、単位の付いた漢数字も半角数字にします。",
            self.counter_switch,
        ))
        self.protect_input = QLineEdit()
        self.protect_input.setPlaceholderText("語を追加")
        self.protect_input.setFixedSize(160, Metrics.FIELD_H)
        self.protect_add_button = CapsuleButton("追加")
        group.add_row(SettingRow(
            "変換しない語",
            "「一人」「十分」など、数字に読める普通の言葉を守ります。",
            trailing_box(self.protect_input, self.protect_add_button),
        ))
        # 語の一覧は別の棚にして、語の出し入れが上の行に響かないようにする
        self.words_group = SettingsGroup()
        self.body.addWidget(self.words_group)
        self.word_rows: List[_WordRow] = []

    def _build_display(self) -> None:
        self.add_section("表示")
        group = self.add_group()
        self.pill_switch = Switch()
        group.add_row(SettingRow(
            "ピルを常に表示", "待機中も画面下部に小さなピルを表示します。録音を始めると大きくなります。",
            self.pill_switch,
        ))
        self.notch_switch = Switch()
        group.add_row(SettingRow(
            "サイドノッチを表示",
            "画面の左端に細いバーを表示します。クリックすると、これまでの音声入力の履歴を開けます。",
            self.notch_switch,
        ))
        self.appearance_picker = Picker()
        for value, label in APPEARANCE_OPTIONS:
            self.appearance_picker.addItem(label, value)
        group.add_row(SettingRow("外観", trailing=self.appearance_picker))

    def _build_sound(self) -> None:
        self.add_section("サウンド")
        group = self.add_group()
        self.sound_switch = Switch()
        group.add_row(SettingRow("操作音", "録音の開始と停止のときに、短い効果音を鳴らします。", self.sound_switch))
        self.duck_switch = Switch()
        group.add_row(SettingRow(
            "音声入力中はメディアの音量を下げる",
            "録音している間だけ、再生中の音楽や動画の音量を下げて、停止すると元に戻します。",
            self.duck_switch,
        ))

    def _build_startup(self) -> None:
        self.add_section("起動")
        group = self.add_group()
        self.autostart_switch = Switch()
        row = SettingRow("ログイン時に起動", trailing=self.autostart_switch)
        self.autostart_message = add_row_message(row)
        group.add_row(row)
        if not autostart.is_supported():
            self.autostart_switch.setEnabled(False)
            self.autostart_message.show_message("この機能は Windows でのみ利用できます。")

    def _build_history(self) -> None:
        self.add_section("履歴")
        group = self.add_group()
        self.sync_switch = Switch()
        group.add_row(SettingRow("Mac と履歴を共有", _SYNC_CAPTION, self.sync_switch))

        self.sync_url_input = QLineEdit()
        self.sync_url_input.setPlaceholderText("https://voicekey-history-sync.<subdomain>.workers.dev")
        self.sync_url_input.setFixedHeight(Metrics.FIELD_H)
        self.sync_url_input.setMinimumWidth(320)
        group.add_row(SettingRow("同期サーバー URL", trailing=self.sync_url_input))

        self.sync_token_input = QLineEdit()
        self.sync_token_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.sync_token_input.setPlaceholderText("Mac から貼り付け")
        self.sync_token_input.setFixedSize(160, Metrics.FIELD_H)
        self.sync_token_save = CapsuleButton("保存")
        self.sync_token_delete = CapsuleButton("削除")
        token_row = SettingRow(
            "共有トークン", trailing=trailing_box(self.sync_token_input, self.sync_token_save, self.sync_token_delete)
        )
        self.sync_token_status = add_row_message(token_row)
        group.add_row(token_row)

        status_row = BlockRow()
        self.sync_status = MessageLabel(lines=1)
        status_row.body.addWidget(self.sync_status)
        group.add_row(status_row)

    def _connect(self) -> None:
        self.language_picker.currentIndexChanged.connect(
            lambda _i: self._save({"language": self.language_picker.currentData() or ""})
        )
        self.device_picker.currentIndexChanged.connect(self._on_device_changed)
        self.reload_devices_button.clicked.connect(self._fill_devices)
        self.detect_button.clicked.connect(self._start_detect)

        self.handsfree_input.valueChanged.connect(self._on_handsfree_changed)
        self.handsfree_clear.clicked.connect(self.handsfree_input.clear_value)
        self.repaste_input.valueChanged.connect(self._on_repaste_changed)
        self.repaste_clear.clicked.connect(self.repaste_input.clear_value)
        self.delay_input.valueChanged.connect(self._on_delay_changed)

        self.model_picker.currentIndexChanged.connect(self._on_model_changed)
        self.prompt_edit.textChanged.connect(self._on_prompt_changed)
        self.prompt_reset_button.clicked.connect(
            lambda: self.prompt_edit.setPlainText(text_formatter.DEFAULT_FORMAT_PROMPT)
        )

        self.numeral_switch.toggled.connect(self._on_numeral_toggled)
        self.counter_switch.toggled.connect(lambda v: self._save({"numeral_convert_counter": v}))
        self.protect_input.textChanged.connect(self._update_buttons)
        self.protect_input.returnPressed.connect(self._add_word)
        self.protect_add_button.clicked.connect(self._add_word)

        self.pill_switch.toggled.connect(lambda v: self._save({"hud_always_visible": v}))
        self.notch_switch.toggled.connect(lambda v: self._save({"side_notch_enabled": v}))
        self.appearance_picker.currentIndexChanged.connect(self._on_appearance_changed)
        self.sound_switch.toggled.connect(lambda v: self._save({"sound_effects_enabled": v}))
        self.duck_switch.toggled.connect(lambda v: self._save({"duck_media_enabled": v}))
        self.autostart_switch.toggled.connect(self._on_autostart_toggled)

        self.sync_switch.toggled.connect(lambda v: self._save({"history_sync": {"enabled": v}}))
        self.sync_url_input.textChanged.connect(self._on_sync_url_changed)
        self.sync_url_input.editingFinished.connect(self.ctx.flush)
        self.sync_token_input.textChanged.connect(self._update_buttons)
        self.sync_token_input.returnPressed.connect(self._save_token)
        self.sync_token_save.clicked.connect(self._save_token)
        self.sync_token_delete.clicked.connect(self._delete_token)

    # ------------------------------------------------------------------
    # 読み込み
    # ------------------------------------------------------------------

    def load(self) -> None:
        """
        保存されている値を部品に入れる（ページを開くたびにも呼ぶ）。

        トレイのメニュー・設定ファイルの手直し・自動起動の外からの変更を拾うため、毎回読み直す。
        """
        get = self.ctx.get
        self._loading = True
        try:
            language = get("language", "ja") or ""
            index = self.language_picker.findData(language)
            if index < 0:
                # 選択肢に無い保存値も消さずに残す（旧画面と同じ）
                self.language_picker.addItem(language, language)
                index = self.language_picker.count() - 1
            self.language_picker.setCurrentIndex(index)
            self._select_device(get("audio_input_device", "default"))

            self.handsfree_input.set_value(str(get("handsfree_key", "") or ""))
            self.repaste_input.set_value(str(get("repaste_key", "") or ""))
            self.delay_input.setValue(int(get("auto_enter_delay_ms", 50) or 0))

            self._fill_models(str(get("format_model", "") or ""))
            prompt = str(get("format_auto_prompt", "") or "")
            shown = prompt or text_formatter.DEFAULT_FORMAT_PROMPT
            if self.prompt_edit.toPlainText() != shown:
                self.prompt_edit.setPlainText(shown)

            self.numeral_switch.setChecked(bool(get("numeral_normalize_enabled", True)))
            self.counter_switch.setChecked(bool(get("numeral_convert_counter", True)))
            words = [w for w in (get("numeral_protect_words", []) or []) if isinstance(w, str) and w]
            if words != [row.word for row in self.word_rows]:
                self._build_words(words)

            self.pill_switch.setChecked(bool(get("hud_always_visible", False)))
            self.notch_switch.setChecked(bool(get("side_notch_enabled", True)))
            self.appearance_picker.setCurrentIndex(self.appearance_picker.findData(appearance_from(get)))
            self.sound_switch.setChecked(bool(get("sound_effects_enabled", True)))
            self.duck_switch.setChecked(bool(get("duck_media_enabled", True)))
            # 自動起動の正本はレジストリ（設定ファイルには持たない）
            self.autostart_switch.setChecked(autostart.is_supported() and autostart.is_enabled())

            sync = get("history_sync", {}) or {}
            self.sync_switch.setChecked(bool(sync.get("enabled", False)))
            url = str(sync.get("url", "") or "")
            if self.sync_url_input.text().strip().rstrip("/") != url:
                self.sync_url_input.setText(url)
        finally:
            self._loading = False
        self.counter_switch.setEnabled(self.numeral_switch.isChecked())
        self._show_token_status()
        self.refresh_sync_status()

    def refresh(self) -> None:
        """ページを開いたとき。"""
        self.load()

    def _fill_models(self, saved: str) -> None:
        """整形モデルの選択肢を作る（一覧に無い保存値は「（利用不可）」として残す）。"""
        model = llm_catalog.normalize_model(saved)
        self.model_picker.clear()
        for entry in llm_catalog.MODELS:
            self.model_picker.addItem(_model_label(entry), entry.id)
        if self.model_picker.findData(model) < 0:
            self.model_picker.addItem(f"{model}（利用不可）", model)
        self.model_picker.setCurrentIndex(self.model_picker.findData(model))
        self._show_model_message()

    def _show_model_message(self) -> None:
        model = self.model_picker.currentData()
        for entry in llm_catalog.MODELS:
            if entry.id == model:
                # Mac と同じく、説明の後ろにモデル id を添える
                self.model_message.show_message(f"{entry.note}（{entry.id}）")
                return
        self.model_message.show_message(
            "このモデルは提供終了か、使えない状態です。おすすめ（Groq は "
            f"{llm_catalog.RECOMMENDED_MODEL}）に切り替えてください。",
            "warn",
        )

    def _save(self, changes: dict) -> None:
        if not self._loading:
            self.ctx.commit(changes)

    def _update_buttons(self, *_args) -> None:
        self.handsfree_clear.setEnabled(bool(self.handsfree_input.value()))
        self.repaste_clear.setEnabled(bool(self.repaste_input.value()))
        self.protect_add_button.setEnabled(bool(self.protect_input.text().strip()))
        self.sync_token_save.setEnabled(bool(self.sync_token_input.text().strip()))
        self.sync_token_delete.setEnabled(self._token_stored)

    # ------------------------------------------------------------------
    # 入力（マイク）
    # ------------------------------------------------------------------

    def _fill_devices(self) -> None:
        """マイクの一覧を読み直す（選んでいたものは残す）。"""
        current = self.device_picker.currentData()
        if current is None:
            current = self.ctx.get("audio_input_device", "default")
        was_loading, self._loading = self._loading, True
        try:
            self.device_picker.clear()
            self.device_picker.addItem("システム既定", "default")
            for device in AudioRecorder.list_input_devices():
                device_id = int(device["id"])
                self.device_picker.addItem(f"{device_id}: {device['label']}", device_id)
            self._select_device(current)
        finally:
            self._loading = was_loading

    def _select_device(self, value) -> None:
        normalized = AudioRecorder.normalize_device_setting(value)
        target = "default" if normalized is None else normalized
        index = self.device_picker.findData(target)
        self.device_picker.setCurrentIndex(index if index >= 0 else 0)

    def _on_device_changed(self, _index: int) -> None:
        data = self.device_picker.currentData()
        self._save({"audio_input_device": "default" if data is None else data})

    def _start_detect(self) -> None:
        """喋った声が入ったマイクを探す（数秒かかるので別スレッドで回し、その間は状態を出す）。"""
        from ...core.mic_auto_detect import detect_async

        self._detect_generation += 1
        self.detect_button.setEnabled(False)
        self.detect_message.show_message("自動検出中… マイクに向かって喋ってください", "accent")
        detect_async(self._detect_done.emit)

    def _on_detect_done(self, result) -> None:
        self.detect_button.setEnabled(True)
        if result:
            self._fill_devices()
            self._select_device(int(result["id"]))
            self.detect_message.show_message(f"「{result['name']}」を選択しました")
            delay = _DETECT_OK_MS
        else:
            self.detect_message.show_message("音声を検出できませんでした。喋りながらもう一度お試しください")
            delay = _DETECT_FAIL_MS
        generation = self._detect_generation
        QTimer.singleShot(delay, lambda: self._clear_detect_message(generation))

    def _clear_detect_message(self, generation: int) -> None:
        # 次の検出が始まっていたら、その表示は消さない
        if generation == self._detect_generation:
            self.detect_message.show_message("")

    # ------------------------------------------------------------------
    # キー操作・整形
    # ------------------------------------------------------------------

    def _on_handsfree_changed(self, value: str) -> None:
        self._update_buttons()
        self._save({"handsfree_key": value})

    def _on_repaste_changed(self, value: str) -> None:
        self._update_buttons()
        self._save({"repaste_key": value})

    def _on_delay_changed(self, value: int) -> None:
        if not self._loading:
            self.ctx.commit_later({"auto_enter_delay_ms": value})

    def _on_model_changed(self, _index: int) -> None:
        self._show_model_message()
        self._save({"format_model": self.model_picker.currentData()})

    def _on_prompt_changed(self) -> None:
        if self._loading:
            return
        text = self.prompt_edit.toPlainText()
        # 既定と同じ文面は空で保存する（既定の文面が今後変わったときに追従させるため。Mac と同じ）
        stored = "" if text.strip() == text_formatter.DEFAULT_FORMAT_PROMPT.strip() else text
        self.ctx.commit_later({"format_auto_prompt": stored})

    # ------------------------------------------------------------------
    # 数字入力
    # ------------------------------------------------------------------

    def _on_numeral_toggled(self, checked: bool) -> None:
        # 全体が OFF なら助数詞の切り替えは意味が無いので押せなくする（Mac と同じ）
        self.counter_switch.setEnabled(checked)
        self._save({"numeral_normalize_enabled": checked})

    def _build_words(self, words: List[str]) -> None:
        for row in self.word_rows:
            row.setParent(None)
            row.deleteLater()
        self.word_rows = []
        for word in words:
            self._append_word(word)

    def _append_word(self, word: str) -> None:
        row = _WordRow(word)
        row.delete_button.clicked.connect(lambda _=False, r=row: self._remove_word(r))
        self.words_group.add_row(row)
        self.word_rows.append(row)

    def _save_words(self) -> None:
        self.ctx.commit({"numeral_protect_words": [row.word for row in self.word_rows]})

    def _add_word(self) -> None:
        """入力した語を足す（前後の空白は除き、もうある語は足さない）。"""
        word = self.protect_input.text().strip()
        if not word:
            return
        if word not in [row.word for row in self.word_rows]:
            self._append_word(word)
            self._save_words()
        self.protect_input.clear()

    def _remove_word(self, row: _WordRow) -> None:
        if row in self.word_rows:
            self.word_rows.remove(row)
        row.setParent(None)
        row.deleteLater()
        self._save_words()

    # ------------------------------------------------------------------
    # 表示・起動
    # ------------------------------------------------------------------

    def _on_appearance_changed(self, _index: int) -> None:
        if self._loading:
            return
        mode = self.appearance_picker.currentData()
        self.ctx.commit({"appearance": mode})
        # 選んだらすぐ見た目を切り替える（保存の結果を待たない）
        Theme.instance().set_mode(mode)

    def _on_autostart_toggled(self, checked: bool) -> None:
        """自動起動を切り替える。失敗したらスイッチを元に戻して知らせる（Mac も登録失敗で戻す）。"""
        if self._loading:
            return
        if autostart.set_enabled(checked):
            self.autostart_message.show_message("")
            return
        self._loading = True
        try:
            self.autostart_switch.setChecked(not checked)
        finally:
            self._loading = False
        self.autostart_message.show_message("ログイン時の起動を変更できませんでした。もう一度お試しください", "danger")

    # ------------------------------------------------------------------
    # 履歴の共有
    # ------------------------------------------------------------------

    def _on_sync_url_changed(self, text: str) -> None:
        if not self._loading:
            # 末尾の「/」は付けても付けなくても同じ先なので外して保存する
            self.ctx.commit_later({"history_sync": {"url": text.strip().rstrip("/")}})

    def _read_token_stored(self) -> bool:
        if not secrets.is_keyring_available():
            return False
        return bool(secrets.get_api_key(secrets.SERVICE_SYNC_TOKEN))

    def _show_token_status(self, result: str = "", kind: str = "secondary") -> None:
        """
        共有トークンの状態を出す（操作の直後はその結果、それ以外は登録済み/未登録）。

        値そのものは画面に戻さない。
        """
        if result:
            self.sync_token_status.show_message(result, kind)
        elif not secrets.is_keyring_available():
            self.sync_token_status.show_message("資格情報ストア利用不可", "warn")
        elif self._token_stored:
            self.sync_token_status.show_message("登録済み", "ok")
        else:
            self.sync_token_status.show_message("未登録")
        self._update_buttons()

    def _save_token(self) -> None:
        token = self.sync_token_input.text().strip()
        if not token:
            return
        if not secrets.set_api_key(secrets.SERVICE_SYNC_TOKEN, token):
            self._show_token_status("共有トークンを保存できませんでした", "danger")
            return
        self.sync_token_input.clear()
        self._token_stored = True
        self._show_token_status("共有トークンを保存しました", "ok")
        if self.ctx.history_sync is not None:
            self.ctx.history_sync.apply_config()
        self.refresh_sync_status()

    def _delete_token(self) -> None:
        if not secrets.delete_api_key(secrets.SERVICE_SYNC_TOKEN):
            self._show_token_status("共有トークンを削除できませんでした", "danger")
            return
        self.sync_token_input.clear()
        self._token_stored = False
        self._show_token_status("共有トークンを削除しました")
        if self.ctx.history_sync is not None:
            self.ctx.history_sync.apply_config()
        self.refresh_sync_status()

    def refresh_sync_status(self) -> None:
        """Mac との同期の状況を出す（同期の知らせを受けたとき・ページを開いたときに呼ぶ）。"""
        sync = self.ctx.history_sync
        if sync is None:
            self.sync_status.show_message("")
            return
        status = sync.status()
        if not status.get("enabled"):
            self.sync_status.show_message("同期はオフです")
            return
        if not status.get("configured"):
            self.sync_status.show_message("URL とトークンを設定すると同期が始まります")
            return
        if status.get("token_invalid"):
            self.sync_status.show_message("トークンが無効です（保存し直してください）", "warn")
            return
        last_sync = status.get("last_sync")
        last_text = None
        if last_sync and parse_iso_date is not None:
            try:
                last_text = parse_iso_date(last_sync).astimezone().strftime("%H:%M")
            except Exception:
                last_text = None
        if last_text is None:
            self.sync_status.show_message("まだ同期していません")
        else:
            self.sync_status.show_message(f"送信待ち {status.get('pending', 0)} 件・最終同期 {last_text}")
