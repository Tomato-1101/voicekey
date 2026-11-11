"""
設定ページで共通に使う部品

- SettingsPage: 1 ページ分の器（見出しはスクロールの外・本文は最大幅 720 で中央寄せ。Mac の SettingsView と同じ）
- BlockRow: 見出しの下に入力欄などを縦に積む行（API キーの行など、横 1 列に収まらない行）
- HotkeyInput: クリックしてキーを押すとホットキーを記録する欄
- hotkey_display: 保存形式（pynput 形式）のホットキーを、画面に出す名前へ直す
"""

from typing import List, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont, QKeyEvent
from PySide6.QtWidgets import QHBoxLayout, QLineEdit, QScrollArea, QVBoxLayout, QWidget

from ...platform import PlatformAdapter, get_platform_adapter
from ..components import MessageLabel, SettingRow, SettingsGroup, Text
from ..theme import Metrics


# ---------------------------------------------------------------------------
# ページの器
# ---------------------------------------------------------------------------


def _centered_column(margins: tuple) -> tuple:
    """
    最大幅 720 の列を、左右の余白を等しく取って中央に置く入れ物を作る。

    Args:
        margins: 入れ物の内側余白（左, 上, 右, 下）

    Returns:
        (入れ物, 列の縦レイアウト)
    """
    holder = QWidget()
    holder.setAutoFillBackground(False)
    outer = QHBoxLayout(holder)
    outer.setContentsMargins(*margins)
    outer.setSpacing(0)
    column = QWidget()
    column.setMaximumWidth(Metrics.CONTENT_MAX_W)
    body = QVBoxLayout(column)
    body.setContentsMargins(0, 0, 0, 0)
    body.setSpacing(10)
    # 列が 720 で止まった後の余りを左右へ等しく配る（Mac の frame(maxWidth: 720) と同じ中央寄せ）
    outer.addStretch(1)
    outer.addWidget(column, 1000)
    outer.addStretch(1)
    return holder, body


class SettingsPage(QWidget):
    """
    設定ページ 1 枚分の器。

    見出しはスクロールの外に置く（Mac 版は見出しが動かず、フォームだけがスクロールする）。
    本文は最大幅 720 の列に縦に並べ、窓が広いときは左右に同じだけ余白を残す。

    Attributes:
        body: 本文を足していく縦のレイアウト
        scroll: 本文のスクロール領域
    """

    def __init__(self, title: str, parent: Optional[QWidget] = None) -> None:
        """
        Args:
            title: ページの見出し（サイドバーの項目名と同じ文字）
            parent: 親ウィジェット
        """
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # 見出し（上 18・左右 20。Mac の SettingsView の見出しと同じ余白）
        head, head_body = _centered_column(
            (Metrics.CONTENT_PADDING, 18, Metrics.CONTENT_PADDING, 10)
        )
        self.title_label = Text(title, size=17, weight=QFont.Weight.Bold)
        head_body.addWidget(self.title_label)
        root.addWidget(head)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.viewport().setAutoFillBackground(False)
        holder, self.body = _centered_column(
            (Metrics.CONTENT_PADDING, 0, Metrics.CONTENT_PADDING, Metrics.CONTENT_PADDING)
        )
        self.scroll.setWidget(holder)
        root.addWidget(self.scroll, 1)

    def add_section(self, title: str) -> Text:
        """
        棚の上に置く小見出しを足す（左端は行の文字と揃える）。

        Args:
            title: 小見出し

        Returns:
            足した小見出し
        """
        self.body.addSpacing(8)
        label = Text(title, size=13, weight=QFont.Weight.DemiBold)
        label.setContentsMargins(Metrics.ROW_PADDING_H, 0, 0, 0)
        self.body.addWidget(label)
        return label

    def add_group(self) -> SettingsGroup:
        """設定行をまとめる棚を足して返す。"""
        group = SettingsGroup()
        self.body.addWidget(group)
        return group

    def add_note(self, text: str) -> Text:
        """
        棚の下に置く注記（小さい補助の文字）を足す。

        Args:
            text: 注記の文

        Returns:
            足した注記
        """
        note = Text(text, size=11, role="secondary", wrap=True)
        note.setContentsMargins(Metrics.ROW_PADDING_H, 0, Metrics.ROW_PADDING_H, 0)
        self.body.addWidget(note)
        return note

    def finish(self) -> None:
        """中身を上に寄せる（最後に 1 回呼ぶ）。"""
        self.body.addStretch(1)


class BlockRow(QWidget):
    """
    棚の中の 1 行。見出し・説明・入力欄を縦に積む（横 1 列に収まらない行に使う）。

    余白は SettingRow と同じにして、棚の中で文字の左端が揃うようにする。

    Attributes:
        body: 中身を足していく縦のレイアウト
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(
            Metrics.ROW_PADDING_H, Metrics.ROW_PADDING_V, Metrics.ROW_PADDING_H, Metrics.ROW_PADDING_V
        )
        self.body.setSpacing(6)
        self.setMinimumHeight(Metrics.ROW_MIN_H)


def add_row_message(row: SettingRow, lines: int = 1) -> MessageLabel:
    """
    SettingRow の見出しの下に、高さ固定の知らせ欄を足す。

    SettingRow.set_caption は説明が空になると欄ごと消して行の高さが変わるので、
    状態によって文が変わる行はこちらを使う（空でも高さを保つ）。

    Args:
        row: 足す先の行
        lines: 確保する行数

    Returns:
        足した知らせ欄
    """
    message = MessageLabel(lines=lines)
    labels = row.layout().itemAt(0).layout()
    labels.addWidget(message)
    return message


def trailing_box(*widgets: QWidget, spacing: int = 8) -> QWidget:
    """
    行の右側に複数の部品を横に並べて置くための入れ物を作る。

    Args:
        widgets: 左から並べる部品
        spacing: 部品の間隔

    Returns:
        入れ物
    """
    box = QWidget()
    layout = QHBoxLayout(box)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(spacing)
    for widget in widgets:
        layout.addWidget(widget)
    return box


# ---------------------------------------------------------------------------
# ホットキー
# ---------------------------------------------------------------------------


# 修飾キーの表示名（Windows のキーの刻印に合わせる。Mac の「右⌘」に当たる）
_TOKEN_NAMES = {
    "ctrl": "Ctrl",
    "ctrl_l": "左 Ctrl",
    "ctrl_r": "右 Ctrl",
    "shift": "Shift",
    "shift_l": "左 Shift",
    "shift_r": "右 Shift",
    "alt": "Alt",
    "alt_l": "左 Alt",
    "alt_r": "右 Alt",
    "alt_gr": "AltGr",
    "cmd": "Win",
    "cmd_l": "左 Win",
    "cmd_r": "右 Win",
    "space": "Space",
    "enter": "Enter",
    "tab": "Tab",
    "esc": "Esc",
    "backspace": "Backspace",
    "delete": "Delete",
    "insert": "Insert",
    "home": "Home",
    "end": "End",
    "page_up": "PageUp",
    "page_down": "PageDown",
    "up": "↑",
    "down": "↓",
    "left": "←",
    "right": "→",
    "caps_lock": "CapsLock",
    "num_lock": "NumLock",
    "scroll_lock": "ScrollLock",
    "print_screen": "PrintScreen",
    "pause": "Pause",
    "menu": "Menu",
}


def _token_display(token: str) -> str:
    """ホットキーの 1 要素（"<ctrl_r>" や "a"）を表示名にする。"""
    if token.startswith("<") and token.endswith(">"):
        name = token[1:-1]
        if name in _TOKEN_NAMES:
            return _TOKEN_NAMES[name]
        # F キー（f1〜f24）は大文字にする
        if name[:1] == "f" and name[1:].isdigit():
            return name.upper()
        return name
    return token.upper() if len(token) == 1 else token


def hotkey_display(value: str) -> str:
    """
    保存形式のホットキー（例 "<ctrl_r>+<f2>"）を画面に出す名前（例 "右 Ctrl + F2"）にする。

    Args:
        value: 保存形式のホットキー（空なら空文字を返す）

    Returns:
        表示名
    """
    if not value:
        return ""
    return " + ".join(_token_display(token) for token in value.split("+") if token)


class HotkeyInput(QLineEdit):
    """
    ホットキーを記録する欄。クリックしてキーを押すと、その組み合わせを pynput 形式で持つ。

    左右の Alt / Ctrl / Shift / Win を区別し、修飾キー単独も割り当てられる。
    値（保存形式）と表示（「右 Ctrl」など）は分けて持ち、表示だけを欄に出す。
    読み取りの手順は旧設定画面の HotkeyInput と同じ。

    Signals:
        valueChanged(str): 割り当てが確定したとき（キーを離した・ESC・割り当てを外した）
    """

    valueChanged = Signal(str)

    # Qt のキーから、修飾キーの種類への対応
    MODIFIER_KEY_MAP = {
        Qt.Key.Key_Control: "ctrl",
        Qt.Key.Key_Shift: "shift",
        Qt.Key.Key_Alt: "alt",
        Qt.Key.Key_Meta: "cmd",
    }

    WIDTH = 180

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        platform_adapter: Optional[PlatformAdapter] = None,
        empty_label: Optional[str] = None,
    ) -> None:
        """
        Args:
            parent: 親ウィジェット
            platform_adapter: 左右のキーの判別に使うアダプタ（省略時はいまの OS のもの）
            empty_label: 未割り当てのときに出す文言（Mac の HotkeyRecorderView の emptyLabel と対応）
        """
        super().__init__(parent)
        self._platform = platform_adapter or get_platform_adapter()
        self.setReadOnly(True)
        self.setPlaceholderText(empty_label or "クリックしてキーを押す…")
        self.setFixedSize(self.WIDTH, Metrics.FIELD_H)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._pressed_keys: List[str] = []  # 押した順に並べたキー
        self._is_recording = False
        self._value = ""

    # ----- 値 -----

    def value(self) -> str:
        """いまの割り当て（保存形式。未割り当てなら空文字）。"""
        return self._value

    def set_value(self, value: str) -> None:
        """
        割り当てを外から入れる（設定の読み込み用。valueChanged は出さない）。

        Args:
            value: 保存形式のホットキー
        """
        self._value = value or ""
        self._pressed_keys = self._value.split("+") if self._value else []
        self._is_recording = False
        self.setText(hotkey_display(self._value))

    def clear_value(self) -> None:
        """割り当てを外す（「割り当てを外す」ボタン用。valueChanged を出す）。"""
        self._commit([])

    def _commit(self, keys: List[str]) -> None:
        """記録したキーを割り当てとして確定し、変わっていれば知らせる。"""
        value = "+".join(keys)
        self.setText(hotkey_display(value))
        if value != self._value:
            self._value = value
            self.valueChanged.emit(value)

    # ----- キー入力 -----

    def focusInEvent(self, event) -> None:  # noqa: N802
        """
        フォーカスを得たら記録待ちにする。

        ここでは記録中のキーを消さない。クリックだけしてキーを押さずに離れると、
        「中身は空なのに表示は前の値」という食い違いになるため。前の値は最初のキーで消す。
        """
        super().focusInEvent(event)
        self._is_recording = False

    def focusOutEvent(self, event) -> None:  # noqa: N802
        """フォーカスを失ったら記録を終える。"""
        super().focusOutEvent(event)
        self._is_recording = False

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        """
        キーを押したら記録する。押し始めの 1 打目で前の割り当てを消す。

        Args:
            event: キーイベント
        """
        key = event.key()
        virtual_key = event.nativeVirtualKey()
        scan_code = event.nativeScanCode()

        # ESC は「割り当てなし」（空）として確定する（Mac 版と同じ）。
        # 空のスロットは app._slot_matches が False を返すので録音が始まらない
        if key == Qt.Key.Key_Escape:
            self._pressed_keys = []
            self._is_recording = False
            self._commit([])
            event.accept()
            return

        if not self._is_recording:
            self._pressed_keys = []
            self._is_recording = True

        if key in self.MODIFIER_KEY_MAP:
            # 仮想キーコード・スキャンコードで左右を見分ける。分からなければ左右なしの名前にする
            key_name = self._platform.modifier_hotkey_from_native(
                virtual_key=virtual_key, scan_code=scan_code
            )
            if not key_name:
                key_name = f"<{self.MODIFIER_KEY_MAP[key]}>"
            if key_name not in self._pressed_keys:
                self._pressed_keys.append(key_name)
                self.setText(hotkey_display("+".join(self._pressed_keys)))
            event.accept()
            return

        key_text = self._platform.qt_key_to_hotkey_token(key, scan_code)
        if key_text and key_text not in self._pressed_keys:
            self._pressed_keys.append(key_text)
            self.setText(hotkey_display("+".join(self._pressed_keys)))
        event.accept()

    def keyReleaseEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        """最初のキーを離した時点で組み合わせを確定する（それ以上は足さない）。"""
        if self._is_recording and self._pressed_keys:
            self._commit(list(self._pressed_keys))
        self._is_recording = False
        event.accept()
