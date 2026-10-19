"""
設定画面の部品（Mac 版 GroupedChoiceMenu / サイドバーの行の作りに合わせたもの）

- GroupedChoiceButton: 見出しつきの 1 つのポップアップ（エンジン＋モデル、翻訳エンジン＋モデル）。
  閉じた表示は「Soniox · stt-rt-v5」のような選択中の名前、開くと見出しの下に選択肢が並ぶ
- NavItemDelegate: サイドバーの見出し行（選べない小さな文字）と、右端の補足（割り当て中のキー）を描く
- key_label: ホットキー文字列（"<ctrl_r>" など）を人が読める表記（「右Ctrl」）にする
"""

from typing import Any, List, Optional, Sequence, Tuple

from PySide6.QtCore import QRect, QSize, Qt, Signal
from PySide6.QtGui import QAction, QColor, QFont, QPainter
from PySide6.QtWidgets import (
    QMenu,
    QPushButton,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QWidget,
)

# サイドバーの項目の役割（QListWidgetItem の UserRole + 1 に入れる）
ROLE_KIND = int(Qt.ItemDataRole.UserRole) + 1
# 右端に出す補足（割り当て中のキーなど）
ROLE_TRAILING = int(Qt.ItemDataRole.UserRole) + 2

KIND_HEADER = "header"
KIND_PAGE = "page"


class GroupedChoiceButton(QPushButton):
    """見出しつきの選択肢を 1 つのポップアップで選ぶボタン（Mac の GroupedChoiceMenu）。

    Signals:
        chosen: 選ばれた値（set_groups で渡した値そのもの）
    """

    chosen = Signal(object)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setObjectName("choiceButton")
        self._menu = QMenu(self)
        self.setMenu(self._menu)
        self._value: Any = None
        self._values: List[Any] = []

    def set_groups(
        self,
        groups: Sequence[Tuple[Optional[str], Sequence[Tuple[Any, str]]]],
        current: Any,
        current_title: str,
    ) -> None:
        """選択肢を入れ直す。

        Args:
            groups: [(見出し or None, [(値, 表示名), ...]), ...]
            current: いま選ばれている値（チェックを付ける）
            current_title: 閉じたボタンに出す名前
        """
        self._menu.clear()
        self._values = []
        self._value = current
        for title, options in groups:
            if title:
                self._menu.addSection(title)
            for value, label in options:
                action = QAction(label, self._menu)
                action.setCheckable(True)
                action.setChecked(value == current)
                action.triggered.connect(lambda _=False, v=value: self.select(v))
                self._menu.addAction(action)
                self._values.append(value)
        self.setText(current_title)

    @property
    def value(self) -> Any:
        """いま選ばれている値。"""
        return self._value

    def values(self) -> List[Any]:
        """選べる値の一覧（テスト・ハーネス用）。"""
        return list(self._values)

    def select(self, value: Any) -> None:
        """値を選ぶ（メニューから選んだのと同じ。chosen を出す）。"""
        self._value = value
        self.chosen.emit(value)


class NavItemDelegate(QStyledItemDelegate):
    """サイドバーの見出し行と、右端の補足（割り当て中のキー）を描くデリゲート。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.secondary_color = QColor("#7F796F")

    def sizeHint(self, option: QStyleOptionViewItem, index) -> QSize:
        size = super().sizeHint(option, index)
        if index.data(ROLE_KIND) == KIND_HEADER:
            # 見出しは上に余白を取り、項目より低くする
            return QSize(size.width(), 30)
        return QSize(size.width(), max(size.height(), 32))

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index) -> None:
        if index.data(ROLE_KIND) == KIND_HEADER:
            painter.save()
            font = QFont(option.font)
            font.setPointSizeF(max(7.5, font.pointSizeF() - 1.5))
            font.setWeight(QFont.Weight.DemiBold)
            painter.setFont(font)
            painter.setPen(self.secondary_color)
            rect = option.rect.adjusted(10, 10, -6, 0)
            painter.drawText(rect, int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                             str(index.data(Qt.ItemDataRole.DisplayRole) or ""))
            painter.restore()
            return
        super().paint(painter, option, index)
        trailing = index.data(ROLE_TRAILING)
        if trailing:
            painter.save()
            selected = bool(option.state & QStyle.StateFlag.State_Selected)
            font = QFont(option.font)
            font.setPointSizeF(max(7.5, font.pointSizeF() - 1.0))
            painter.setFont(font)
            painter.setPen(QColor("#FFFFFF") if selected else self.secondary_color)
            rect = QRect(option.rect).adjusted(0, 0, -10, 0)
            painter.drawText(rect, int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
                             str(trailing))
            painter.restore()


# 単独キーの表記（Windows の呼び名。Mac の KeyToken.displayName に当たる）
_KEY_NAMES = {
    "ctrl": "Ctrl", "ctrl_l": "左Ctrl", "ctrl_r": "右Ctrl",
    "shift": "Shift", "shift_l": "左Shift", "shift_r": "右Shift",
    "alt": "Alt", "alt_l": "左Alt", "alt_r": "右Alt", "alt_gr": "右Alt",
    "cmd": "Win", "cmd_l": "左Win", "cmd_r": "右Win",
    "space": "Space", "enter": "Enter", "tab": "Tab", "esc": "Esc",
    "caps_lock": "CapsLock", "backspace": "Backspace", "delete": "Delete",
    "insert": "Insert", "home": "Home", "end": "End", "page_up": "PageUp", "page_down": "PageDown",
    "up": "↑", "down": "↓", "left": "←", "right": "→",
    "nonconvert": "無変換", "convert": "変換", "kana": "かな", "hankaku_zenkaku": "半角/全角", "eisu": "英数",
    "pause": "Pause", "print_screen": "PrintScreen", "scroll_lock": "ScrollLock", "num_lock": "NumLock",
}


def key_label(hotkey: str, empty: str = "未割り当て") -> str:
    """ホットキー文字列（"<ctrl_r>" / "<ctrl>+<alt>+v"）を人が読める表記にする（「右Ctrl」「Ctrl+Alt+V」）。"""
    tokens = [t.strip().strip("<>").lower() for t in (hotkey or "").split("+") if t.strip().strip("<>")]
    if not tokens:
        return empty
    names = []
    for token in tokens:
        if token in _KEY_NAMES:
            names.append(_KEY_NAMES[token])
        elif token.startswith("f") and token[1:].isdigit():
            names.append(token.upper())
        elif len(token) == 1:
            names.append(token.upper())
        else:
            names.append(token)
    return "+".join(names)


def hotkey_tokens(hotkey: str) -> set:
    """ホットキー文字列をトークン集合にする（app._parse_hotkey と同じ規則）。"""
    return {t.strip().strip("<>").lower() for t in (hotkey or "").split("+") if t.strip().strip("<>")}


_GENERIC = ("ctrl", "alt", "shift", "cmd")


def _names(token: str) -> set:
    """トークンが一致しうるキー名の集合（app._acceptable_names と同じ・左右と汎用名の展開）。"""
    if token in _GENERIC:
        return {token, f"{token}_l", f"{token}_r"}
    if token.endswith("_l") and token[:-2] in _GENERIC:
        return {token, token[:-2]}
    if token.endswith("_r") and token[:-2] in _GENERIC:
        return {token, token[:-2]}
    return {token}


def keys_conflict(a: str, b: str) -> bool:
    """2 つのキー設定が同じ押し方として扱われるか（両方空なら False）。"""
    ta, tb = hotkey_tokens(a), hotkey_tokens(b)
    if not ta or not tb or len(ta) != len(tb):
        return False
    return all(any(_names(x) & _names(y) for y in tb) for x in ta)


def keys_covered(inner: str, outer_tokens: set) -> bool:
    """inner のキーがすべて outer のトークンに含まれるか（含まれると押すたびに一緒に効いてしまう）。"""
    ti = hotkey_tokens(inner)
    if not ti or not outer_tokens:
        return False
    outer_names = set()
    for t in outer_tokens:
        outer_names |= _names(t)
    return all(_names(t) & outer_names for t in ti)


def translate_once_key_warnings(key: str, slot_hotkeys: Sequence[str], handsfree_key: str, repaste_key: str) -> List[str]:
    """1 回翻訳キーが他のキー設定と重なっていないか（設定画面の警告文・Mac の translateOnceKeyWarnings）。"""
    if not hotkey_tokens(key):
        return []
    warnings: List[str] = []
    for index, slot in enumerate(slot_hotkeys):
        if not hotkey_tokens(slot):
            continue
        if keys_conflict(key, slot) or keys_covered(key, hotkey_tokens(slot)):
            warnings.append(f"録音キー {index + 1} と重なっています。この 1 回翻訳キーは効きません")
    if not warnings and hotkey_tokens(handsfree_key):
        covered = keys_conflict(key, handsfree_key) or keys_covered(key, hotkey_tokens(handsfree_key)) or any(
            hotkey_tokens(s) and keys_covered(key, hotkey_tokens(s) | hotkey_tokens(handsfree_key))
            for s in slot_hotkeys
        )
        if covered:
            warnings.append("ハンズフリーキーと重なっています。ハンズフリーが優先され、この 1 回翻訳キーは効きません")
    if hotkey_tokens(repaste_key) and (
        keys_conflict(key, repaste_key) or keys_covered(repaste_key, hotkey_tokens(key))
    ):
        warnings.append("再貼り付けキーと重なっています。録音していないときに押すと再貼り付けが動きます")
    return warnings


class CostTrendBars(QWidget):
    """直近 30 日の API 料金の縦バー（最も高い日を 1.0 とした相対の高さ・Mac のホームと同じ表現）。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(64)
        self._values: List[float] = []
        self.accent = QColor("#FF5A1F")
        self.empty = QColor(127, 121, 111, 40)

    def set_values(self, values: Sequence[float]) -> None:
        """日ごとの料金（古い順）を設定して描き直す。"""
        self._values = [max(0.0, float(v)) for v in values]
        self.update()

    def paintEvent(self, event) -> None:
        if not self._values:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        count = len(self._values)
        gap = 3.0
        width = max(1.0, (self.width() - gap * (count - 1)) / count)
        top = max(self._values) or 0.0
        height = float(self.height())
        for i, value in enumerate(self._values):
            ratio = value / top if top > 0 else 0.0
            bar_h = max(4.0, height * ratio)
            color = QColor(self.accent) if value > 0 else self.empty
            if value > 0:
                color.setAlpha(190)
            painter.setBrush(color)
            x = i * (width + gap)
            painter.drawRoundedRect(int(x), int(height - bar_h), int(max(1.0, width)), int(bar_h), 2, 2)
        painter.end()
