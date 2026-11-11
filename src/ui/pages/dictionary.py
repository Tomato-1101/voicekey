"""
ユーザー辞書のページ

文字起こし・整形が終わった文章を貼り付ける直前に「変換元」→「変換先」へ機械置換するルールを編集する。
並び・文言は Mac 版 DictionaryTab と同じ（空のときの案内・一覧・下部の「追加」と注記）。
編集はその場で保存する（文字の入力は少し待ってまとめて保存、切り替え・追加・削除はすぐ保存）。
"""

from typing import List, Optional

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QFont, QPainter
from PySide6.QtWidgets import QHBoxLayout, QLineEdit, QStackedWidget, QVBoxLayout, QWidget

from .. import icons, theme
from ..components import CapsuleButton, SettingsGroup, Switch, Text
from ..theme import Metrics, Theme
from .context import PageContext
from .widgets import SettingsPage, _centered_column

_EMPTY_TITLE = "確定置換ルールがありません"
_EMPTY_HINT = "「追加」で、よく誤変換される語の置き換えを登録できます。"
_FOOTER = "貼り付け直前に置き換えます（部分一致・登録順）。"


class _EmptyIcon(QWidget):
    """空のときの案内の上に出す大きな本のアイコン（Mac の character.book.closed に当たる）。"""

    SIZE = 36

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFixedSize(self.SIZE, self.SIZE)
        Theme.instance().changed.connect(self.update)

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        icons.paint(painter, "book", QRectF(0, 0, self.SIZE, self.SIZE), theme.palette().text_secondary)


class _RuleRow(QWidget):
    """
    置換ルール 1 行（有効切り替え・変換元・→・変換先・削除）。

    Attributes:
        enabled / from_input / to_input / delete_button: 行の部品
    """

    def __init__(self, rule: dict, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(
            Metrics.ROW_PADDING_H, Metrics.ROW_PADDING_V, Metrics.ROW_PADDING_H, Metrics.ROW_PADDING_V
        )
        layout.setSpacing(8)
        self.setMinimumHeight(Metrics.ROW_MIN_H)

        self.enabled = Switch(bool(rule.get("enabled", True)))
        self.enabled.setToolTip("この行を有効/無効にする")
        self.from_input = QLineEdit(str(rule.get("from", "")))
        self.from_input.setPlaceholderText("変換元")
        self.from_input.setFixedHeight(Metrics.FIELD_H)
        arrow = Text("→", size=13, role="secondary")
        self.to_input = QLineEdit(str(rule.get("to", "")))
        self.to_input.setPlaceholderText("変換先")
        self.to_input.setFixedHeight(Metrics.FIELD_H)
        self.delete_button = CapsuleButton("削除", kind="destructive")
        self.delete_button.setToolTip("この行を削除")

        layout.addWidget(self.enabled)
        layout.addWidget(self.from_input, 1)
        layout.addWidget(arrow)
        layout.addWidget(self.to_input, 1)
        layout.addWidget(self.delete_button)

    def rule(self) -> dict:
        """この行を保存形式（from / to / enabled）にする。"""
        return {
            "from": self.from_input.text().strip(),
            "to": self.to_input.text(),
            "enabled": self.enabled.isChecked(),
        }


class DictionaryPage(SettingsPage):
    """
    ユーザー辞書のページ。

    Attributes:
        rows: いま並んでいるルール行（上から登録順）
        add_button: 「追加」ボタン
        stack: 空のときの案内（0）と一覧（1）の切り替え
    """

    def __init__(self, ctx: PageContext, parent: Optional[QWidget] = None) -> None:
        super().__init__("ユーザー辞書", parent)
        self.ctx = ctx
        self.rows: List[_RuleRow] = []

        # 空のときの案内と一覧は同じ場所に重ね、どちらかを見せる（行の出し入れで下部が動かないように）
        self.stack = QStackedWidget()
        empty = QWidget()
        empty_layout = QVBoxLayout(empty)
        empty_layout.setContentsMargins(0, 40, 0, 40)
        empty_layout.setSpacing(6)
        empty_layout.addStretch(1)
        empty_layout.addWidget(_EmptyIcon(), 0, Qt.AlignmentFlag.AlignHCenter)
        title = Text(_EMPTY_TITLE, size=13, weight=QFont.Weight.Medium, role="secondary")
        title.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        empty_layout.addWidget(title)
        hint = Text(_EMPTY_HINT, size=11, role="secondary", wrap=True)
        hint.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        empty_layout.addWidget(hint)
        empty_layout.addStretch(1)
        self.stack.addWidget(empty)

        self.group = SettingsGroup()
        self.stack.addWidget(self.group)
        self.body.addWidget(self.stack)
        self.finish()

        # 下部（スクロールの外・区切り線の下に固定）: 左に「追加」、右に注記
        footer, footer_body = _centered_column(
            (Metrics.CONTENT_PADDING, 12, Metrics.CONTENT_PADDING, 16)
        )
        bar = QHBoxLayout()
        bar.setContentsMargins(Metrics.ROW_PADDING_H, 0, Metrics.ROW_PADDING_H, 0)
        bar.setSpacing(12)
        self.add_button = CapsuleButton("＋ 追加")
        bar.addWidget(self.add_button)
        bar.addStretch(1)
        bar.addWidget(Text(_FOOTER, size=11, role="secondary"))
        footer_body.addLayout(bar)
        self.layout().addWidget(footer)

        self.add_button.clicked.connect(self._on_add)
        self._build(self._saved_rules())

    # ----- 読み込み -----

    def _saved_rules(self) -> List[dict]:
        """保存されているルール（形の崩れた要素は飛ばす）。"""
        rules = self.ctx.get("replacements", []) or []
        return [rule for rule in rules if isinstance(rule, dict)]

    def _build(self, rules: List[dict]) -> None:
        """一覧をルールから作り直す。"""
        for row in self.rows:
            row.setParent(None)
            row.deleteLater()
        self.rows = []
        for rule in rules:
            self._append_row(rule)
        self._update_stack()

    def refresh(self) -> None:
        """
        ページを開いたとき。設定ファイルの手直しなどで中身が変わっていたときだけ作り直す。

        いつも作り直すと、書きかけの空の行（まだ保存されていない）が消えてしまうため。
        """
        saved = [
            {"from": str(r.get("from", "")), "to": str(r.get("to", "")), "enabled": bool(r.get("enabled", True))}
            for r in self._saved_rules()
        ]
        if saved != self._collect():
            self._build(saved)

    # ----- 行の出し入れ -----

    def _append_row(self, rule: dict) -> _RuleRow:
        row = _RuleRow(rule)
        row.enabled.toggled.connect(self._save_now)
        row.from_input.textChanged.connect(self._save_later)
        row.to_input.textChanged.connect(self._save_later)
        row.delete_button.clicked.connect(lambda _=False, r=row: self._on_delete(r))
        self.group.add_row(row)
        self.rows.append(row)
        return row

    def _update_stack(self) -> None:
        """ルールが無いときだけ空の案内を見せる。"""
        self.stack.setCurrentIndex(1 if self.rows else 0)

    def _on_add(self) -> None:
        """空の行を 1 つ足して、変換元へ入力を移す（変換元が空のあいだは保存されない）。"""
        row = self._append_row({})
        self._update_stack()
        row.from_input.setFocus()

    def _on_delete(self, row: _RuleRow) -> None:
        if row in self.rows:
            self.rows.remove(row)
        row.setParent(None)
        row.deleteLater()
        self._update_stack()
        self._save_now()

    # ----- 保存 -----

    def _collect(self) -> List[dict]:
        """並んでいる行を保存形式にする（変換元が空の行は捨てる）。"""
        return [rule for rule in (row.rule() for row in self.rows) if rule["from"]]

    def _save_now(self, *_args) -> None:
        self.ctx.commit({"replacements": self._collect()})

    def _save_later(self, *_args) -> None:
        # 1 文字ごとに書き込まないよう、入力が止まってからまとめて保存する
        self.ctx.commit_later({"replacements": self._collect()})
