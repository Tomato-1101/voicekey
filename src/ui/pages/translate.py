"""
翻訳して入力のページ

文字起こし結果を訳してから貼り付ける設定（全体で 1 つ。スロットごとではない）。
並び・文言は Mac 版 TranslateInputTab と同じ（1 回翻訳キー／常に翻訳／入力する言語／翻訳エンジン／フッター）。
Windows には OS の翻訳が無いのでエンジンは Groq だけ。Mac で Groq を選んだときと同じ見た目にする
（エンジンの行は選べない表示にし、その下に Groq のモデルを置く）。
"""

from typing import Optional

from PySide6.QtWidgets import QWidget

from ...core import dictation_translator, llm_catalog
from ..components import CapsuleButton, Picker, SettingRow, Switch, Text
from .context import PageContext
from .widgets import HotkeyInput, SettingsPage, add_row_message, trailing_box

_ONCE_CAPTION = (
    "録音中にこのキーを押すと（録音キーと同時でも、話している途中でも）、その回だけ翻訳して貼り付けます。"
    "翻訳される回は録音中の表示に「→EN」のような印が出ます。録音中にもう一度押すと取り消せます。"
)
_ALWAYS_CAPTION = (
    "話した内容を毎回翻訳してから貼り付けます（例: 日本語で話す → 英語が入力される）。"
    "タスクトレイの「翻訳して入力（常時）」でも切り替えられます。"
)
_ENGINE_LABEL = "Groq（LLM・APIキーが必要）"
_ENGINE_CAPTION = "Groq の LLM で翻訳します。話し言葉のニュアンスに強い代わりに、通信と API キーが必要です。"
_FOOTER = (
    "「文章を自動で整える」と同時に使うと、整形と翻訳で 2 回 LLM を呼びます。"
    "速さを優先するなら、録音キーの設定で整形をオフにしてください。"
    "翻訳に失敗したときは原文がそのまま入力されます（文章を失いません）。"
)


class TranslatePage(SettingsPage):
    """
    翻訳して入力のページ。

    Attributes:
        once_input / once_clear / once_warning / always_switch / target_picker / model_picker: 部品
    """

    def __init__(self, ctx: PageContext, parent: Optional[QWidget] = None) -> None:
        super().__init__("翻訳して入力", parent)
        self.ctx = ctx
        self._loading = False

        group = self.add_group()
        self.once_input = HotkeyInput(platform_adapter=ctx.platform, empty_label="クリックして設定")
        self.once_clear = CapsuleButton("クリア")
        # クリアは割り当てがあるときだけ見せる。消えても場所は残して欄の位置を動かさない
        policy = self.once_clear.sizePolicy()
        policy.setRetainSizeWhenHidden(True)
        self.once_clear.setSizePolicy(policy)
        once_row = SettingRow(
            "この 1 回だけ翻訳するキー", _ONCE_CAPTION, trailing_box(self.once_input, self.once_clear)
        )
        # 重なりの警告は最大 2 文（録音キー＋再貼り付け）。空でも高さを確保する
        self.once_warning = add_row_message(once_row, lines=2)
        group.add_row(once_row)

        self.always_switch = Switch()
        group.add_row(SettingRow("常に翻訳して入力する", _ALWAYS_CAPTION, self.always_switch))

        group = self.add_group()
        self.target_picker = Picker()
        for code, label in dictation_translator.TARGET_LANGUAGES:
            self.target_picker.addItem(label, code)
        group.add_row(SettingRow("入力する言語", trailing=self.target_picker))
        group.add_row(SettingRow("翻訳エンジン", _ENGINE_CAPTION, Text(_ENGINE_LABEL, size=13)))
        self.model_picker = Picker()
        self.model_picker.setMinimumWidth(220)
        group.add_row(SettingRow("Groq のモデル", trailing=self.model_picker))
        self.add_note(_FOOTER)
        self.finish()

        self.load()
        self.once_input.valueChanged.connect(self._on_once_changed)
        self.once_clear.clicked.connect(self.once_input.clear_value)
        self.always_switch.toggled.connect(lambda v: self._save({"translate_input_enabled": v}))
        self.target_picker.currentIndexChanged.connect(
            lambda _i: self._save({"translate_input_target": self.target_picker.currentData()})
        )
        self.model_picker.currentIndexChanged.connect(
            lambda _i: self._save({"translate_input_model": self.model_picker.currentData()})
        )

    def load(self) -> None:
        """保存されている値を部品に入れる（トレイの切り替えを拾うため、開くたびにも呼ぶ）。"""
        get = self.ctx.get
        self._loading = True
        try:
            self.once_input.set_value(str(get("translate_once_key", "") or ""))
            self.always_switch.setChecked(bool(get("translate_input_enabled", False)))
            target = dictation_translator.normalize_target(get("translate_input_target"))
            self.target_picker.setCurrentIndex(self.target_picker.findData(target))
            self._fill_models(str(get("translate_input_model", "") or ""))
        finally:
            self._loading = False
        self._update_once()

    def refresh(self) -> None:
        """ページを開いたとき。"""
        self.load()

    def _fill_models(self, saved: str) -> None:
        """モデルの選択肢を作る（一覧に無い保存値は「（利用不可）」として残す。一般ページの整形モデルと同じ）。"""
        model = llm_catalog.normalize_model(saved)
        self.model_picker.clear()
        for entry in llm_catalog.MODELS:
            label = f"{entry.name}（おすすめ）" if entry.recommended else entry.name
            self.model_picker.addItem(label, entry.id)
        if self.model_picker.findData(model) < 0:
            self.model_picker.addItem(f"{model}（利用不可）", model)
        self.model_picker.setCurrentIndex(self.model_picker.findData(model))

    def _save(self, changes: dict) -> None:
        if not self._loading:
            self.ctx.commit(changes)

    def _on_once_changed(self, value: str) -> None:
        self._save({"translate_once_key": value})
        self._update_once()

    def _update_once(self) -> None:
        """クリアの出し入れと、ほかのキーとの重なりの警告を出し直す。"""
        get = self.ctx.get
        key = self.once_input.value()
        self.once_clear.setVisible(bool(key))
        slots = [str((get(f"hotkey{n}", {}) or {}).get("hotkey", "") or "") for n in (1, 2)]
        warnings = dictation_translator.translate_once_key_warnings(
            key, slots, str(get("handsfree_key", "") or ""), str(get("repaste_key", "") or "")
        )
        self.once_warning.show_message("\n".join(warnings), "warn")
