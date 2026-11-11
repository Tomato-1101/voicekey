"""
録音キー 1 / 録音キー 2 のページ

同じ画面を 2 回使う（slot_id で hotkey1 / hotkey2 を読み書きする）。並び・文言は Mac 版 SlotSettingsTab と同じ。
変えた項目はその場で保存する（保存ボタンは無い）。

Mac は「モデル」行を候補が 1 つのエンジンでは出さず、「整え方」を整形 OFF のときは出さないが、
ここでは行を消すと下の行がずれるので、行は残して選べないようにする。
"""

from typing import Optional

from PySide6.QtWidgets import QWidget

from ...config import HotkeyMode
from ...core import engines, text_formatter
from ..components import CapsuleButton, Picker, Segmented, SettingRow, Switch
from .context import PageContext
from .widgets import HotkeyInput, SettingsPage, add_row_message, trailing_box

# 録音のしかた（保存値, 表示名）。表示名は Mac 版 HotkeyMode の表示と同じ
MODE_OPTIONS = (
    (HotkeyMode.HOLD.value, "押している間だけ"),
    (HotkeyMode.TOGGLE.value, "押すたびに開始・停止"),
)

# 設定ファイルに無いときの既定（ホットキー, エンジン）。Windows の既定キーは F2 / F3
SLOT_DEFAULTS = {1: ("<f2>", "soniox"), 2: ("<f3>", "groq")}

TITLES = {1: "録音キー 1（メイン）", 2: "録音キー 2（サブ）"}

_DUPLICATE_WARNING = "もう一方の録音キーと同じキーです。どちらかを変えてください"

# 選んだエンジン・整え方の説明が最長のときに収まる行数（文が変わっても下の行がずれないように固定）。
# 窓をいちばん狭く（760 幅）しても、Microsoft MAI 以外は省略されない行数（MAI は末尾を省略し全文はツールチップ）
_CAPTION_LINES = 5
_PRESET_CAPTION_LINES = 3


class SlotPage(SettingsPage):
    """
    録音キー 1 つ分の設定ページ。

    Attributes:
        slot_id: 1 または 2
        hotkey_input / unassign_button / mode_segmented / engine_picker / model_picker /
        format_switch / preset_picker: 各行の操作部品
    """

    def __init__(self, ctx: PageContext, slot_id: int, parent: Optional[QWidget] = None) -> None:
        super().__init__(TITLES[slot_id], parent)
        self.ctx = ctx
        self.slot_id = slot_id
        self._key = f"hotkey{slot_id}"
        self._other_key = f"hotkey{2 if slot_id == 1 else 1}"
        # 読み込みで部品の値を入れている間は、変更の知らせを保存に回さない
        self._loading = False

        # ----- キー -----
        self.add_section("キー")
        keys = self.add_group()
        self.hotkey_input = HotkeyInput(platform_adapter=ctx.platform, empty_label="未割り当て")
        self.unassign_button = CapsuleButton("割り当てを外す")
        hotkey_row = SettingRow(
            "ホットキー",
            "クリックしてキーを押すと割り当てます。ESC で取り消し。",
            trailing_box(self.hotkey_input, self.unassign_button),
        )
        self.duplicate_message = add_row_message(hotkey_row)
        keys.add_row(hotkey_row)
        self.mode_segmented = Segmented([label for _, label in MODE_OPTIONS])
        keys.add_row(SettingRow("録音のしかた", trailing=self.mode_segmented))

        # ----- 文字起こし -----
        self.add_section("文字起こし")
        transcribe = self.add_group()
        self.engine_picker = Picker()
        for eng in engines.ENGINES:
            self.engine_picker.addItem(eng.label, eng.id)
        engine_row = SettingRow("エンジン", trailing=self.engine_picker)
        self.engine_caption = add_row_message(engine_row, lines=_CAPTION_LINES)
        transcribe.add_row(engine_row)
        self.model_picker = Picker()
        self.model_picker.setMinimumWidth(200)
        model_row = SettingRow("モデル", trailing=self.model_picker)
        self.model_message = add_row_message(model_row, lines=2)
        transcribe.add_row(model_row)

        # ----- 整形 -----
        self.add_section("整形")
        shaping = self.add_group()
        self.format_switch = Switch()
        shaping.add_row(SettingRow("文章を自動で整える", trailing=self.format_switch))
        self.preset_picker = Picker()
        for preset in text_formatter.FORMAT_PRESETS:
            self.preset_picker.addItem(preset.name, preset.id)
        preset_row = SettingRow("整え方", trailing=self.preset_picker)
        self.preset_caption = add_row_message(preset_row, lines=_PRESET_CAPTION_LINES)
        shaping.add_row(preset_row)
        self.finish()

        self.load()

        self.hotkey_input.valueChanged.connect(self._on_hotkey_changed)
        self.unassign_button.clicked.connect(self.hotkey_input.clear_value)
        self.mode_segmented.currentChanged.connect(self._on_mode_changed)
        self.engine_picker.currentIndexChanged.connect(self._on_engine_changed)
        self.model_picker.currentIndexChanged.connect(self._on_model_changed)
        self.format_switch.toggled.connect(self._on_format_toggled)
        self.preset_picker.currentIndexChanged.connect(self._on_preset_changed)

    # ----- 読み込み -----

    def _slot(self) -> dict:
        """保存されているこのスロットの設定（無ければ空の辞書）。"""
        return self.ctx.get(self._key, {}) or {}

    def load(self) -> None:
        """
        保存されている値を部品に入れる（ページを開くたびにも呼ぶ）。

        トレイのメニューや設定ファイルの手直しで値が変わっていることがあるため、毎回読み直す。
        """
        self._loading = True
        try:
            slot = self._slot()
            default_hotkey, default_backend = SLOT_DEFAULTS[self.slot_id]
            self.hotkey_input.set_value(str(slot.get("hotkey", default_hotkey) or ""))

            mode = slot.get("hotkey_mode", HotkeyMode.TOGGLE.value)
            modes = [value for value, _ in MODE_OPTIONS]
            index = modes.index(mode) if mode in modes else modes.index(HotkeyMode.TOGGLE.value)
            self.mode_segmented.set_current_index(index, animate=False, notify=False)

            backend = engines.normalize_id(slot.get("backend", default_backend))
            self.engine_picker.setCurrentIndex(self.engine_picker.findData(backend))
            self._fill_models(backend, engines.normalize_model(backend, slot.get("api_model", "")))
            self.model_message.show_message("")

            self.format_switch.setChecked(bool(slot.get("format_enabled", False)))
            preset = text_formatter.normalize_preset(slot.get("format_preset"))
            self.preset_picker.setCurrentIndex(self.preset_picker.findData(preset))
        finally:
            self._loading = False
        self._show_engine_caption()
        self._show_preset_caption()
        self._update_states()

    def refresh(self) -> None:
        """ページを開いたとき。もう一方の録音キーが変わっているかもしれないので読み直す。"""
        self.load()

    def _fill_models(self, backend: str, selected: str) -> None:
        """
        モデルの選択肢をエンジンに合わせて作り直す。

        Args:
            backend: エンジン id
            selected: 選んでおくモデル（一覧に無ければ選択肢に足して残す）
        """
        eng = engines.engine(backend)
        was_loading, self._loading = self._loading, True
        try:
            self.model_picker.clear()
            for model in eng.models:
                label = f"{model}（推奨）" if model == eng.default_model else model
                self.model_picker.addItem(label, model)
            if self.model_picker.findData(selected) < 0:
                self.model_picker.addItem(selected, selected)
            self.model_picker.setCurrentIndex(self.model_picker.findData(selected))
            # 候補が 1 つなら選ぶものが無い（Mac はこの行を出さない。ここでは行を残して押せなくする）
            self.model_picker.setEnabled(self.model_picker.count() > 1)
        finally:
            self._loading = was_loading

    # ----- 表示の更新 -----

    def _show_engine_caption(self) -> None:
        """選んだエンジンの説明を出す。"""
        backend = self.engine_picker.currentData()
        caption = engines.engine(backend).caption if backend else ""
        # 省略表示（elidedText）は改行を扱えないので、続けた 1 段落にして折り返しに任せる
        self.engine_caption.show_message(caption.replace("\n", ""))

    def _show_preset_caption(self) -> None:
        """選んだ整え方の説明を出す。"""
        preset_id = self.preset_picker.currentData()
        for preset in text_formatter.FORMAT_PRESETS:
            if preset.id == preset_id:
                self.preset_caption.show_message(preset.description)
                return
        self.preset_caption.show_message("")

    def _update_states(self) -> None:
        """割り当ての有無・整形の ON/OFF・重なりの警告を表示に反映する。"""
        value = self.hotkey_input.value()
        self.unassign_button.setEnabled(bool(value))
        self.preset_picker.setEnabled(self.format_switch.isChecked())
        other = (self.ctx.get(self._other_key, {}) or {}).get("hotkey", "")
        if value and value == other:
            self.duplicate_message.show_message("⚠ " + _DUPLICATE_WARNING, "warn")
        else:
            self.duplicate_message.show_message("")

    # ----- 変更をすぐ保存する -----

    def _save(self, changes: dict) -> None:
        """このスロットの変えた項目だけを保存する。"""
        self.ctx.commit({self._key: changes})

    def _on_hotkey_changed(self, value: str) -> None:
        if self._loading:
            return
        self._save({"hotkey": value})
        self._update_states()

    def _on_mode_changed(self, index: int) -> None:
        if self._loading:
            return
        self._save({"hotkey_mode": MODE_OPTIONS[index][0]})

    def _on_engine_changed(self, _index: int) -> None:
        """エンジンを変えたら、モデルをそのエンジンの既定へ切り替える（整形の設定は触らない。Mac と同じ）。"""
        if self._loading:
            return
        backend = self.engine_picker.currentData()
        default_model = engines.engine(backend).default_model
        self._fill_models(backend, default_model)
        self._show_engine_caption()
        self.model_message.show_message(f"エンジンを変えたので、モデルを「{default_model}」に切り替えました")
        self._save({"backend": backend, "api_model": default_model})

    def _on_model_changed(self, _index: int) -> None:
        if self._loading:
            return
        self.model_message.show_message("")
        self._save({"api_model": self.model_picker.currentData()})

    def _on_format_toggled(self, checked: bool) -> None:
        if self._loading:
            return
        self._update_states()
        self._save({"format_enabled": checked})

    def _on_preset_changed(self, _index: int) -> None:
        if self._loading:
            return
        self._show_preset_caption()
        self._save({"format_preset": self.preset_picker.currentData()})
