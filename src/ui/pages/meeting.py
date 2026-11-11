"""
議事録・会議のページ

並び・文言・欄の高さは Mac 版 MeetingTab に合わせる（会議に参加／記録中の発言／過去の議事録／
文字起こしの方法／ボット用ブラウザのログイン／保存先）。
Windows の文字起こしは Meet の字幕だけ（ローカル認識は Apple の機能なので無い）。

状態・発言・一覧のどれが変わっても、欄の位置と高さは変わらない（状態行 36・補足 2 行・
発言欄 160・一覧 132・本文欄 220・知らせ 1 行をいつも確保する）。
"""

import html
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from PySide6.QtCore import QModelIndex, QPointF, QRectF, QSize, Qt, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QFont, QFontMetrics, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QStyledItemDelegate,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ...meetbot import chrome, rules, transcript
from .. import icons, theme
from ..components import CapsuleButton, MessageLabel, Picker, Segmented, SettingRow, Text
from ..theme import Metrics, Theme
from .context import PageContext
from .widgets import BlockRow, SettingsPage, add_row_message, trailing_box

CONFIG_CAPTION_LANGUAGE = "meetbot_caption_language"

# ----- 文言（Mac 版と同じ） -----
_URL_PLACEHOLDER = "https://meet.google.com/abc-defg-hij"
_HINT_IDLE = "会議の URL を入れて「参加」を押してください。Google にログインしていなくても入れます。"
_HINT_STARTING = "Chrome を裏で起動して会議に入っています。数十秒かかることがあります。"
_HINT_WAITING = "会議の画面で主催者が「承認」を押すと記録が始まります。"
_HINT_RECORDING = "発言があると議事録に書き始めます。"
_HINT_FAILED = "直したら、もう一度「参加」を押してください。"
_HINT_NO_CHROME = "Google Chrome が必要です。インストールすると使えるようになります。"
_HINT_NO_SERVICE = "このビルドでは議事録ボットを使えません。"
_JOIN_GUEST = f"ゲスト「{rules.BOT_DISPLAY_NAME}」として参加をリクエストします。主催者の承認が要ります。"
_JOIN_ACCOUNT = "ログイン中の Google アカウントの名前で入ります。ほかの人の会議では主催者の承認が要ります。"
_LIVE_EMPTY = "会議に参加すると、発言がここに流れます。"
_LIST_EMPTY = "まだ議事録がありません。会議に参加すると、ここに並びます。"
_BODY_UNSELECTED = "上の一覧から議事録を選ぶと、ここに本文が出ます。"
_BODY_EMPTY = "この議事録には発言がありません。"
_SUMMARY_UNSELECTED = "上の一覧から議事録を選んでください。"
_SUMMARY_NONE = "まだ要約がありません。「要約」を押すと作ります。"
_NOTICE_DEFAULT = "要約は Groq の無料モデルで作り、議事録の隣に保存します。"
_METHOD_CAPTION = "Meet の字幕を読みます。話者名が残ります（認識は Google 側）。"
_LANGUAGE_CAPTION = "会議で話される言語を選んでください。"
_METHOD_NOTE = "参加中は変えられません。変えた設定は、次に参加したときから効きます。"
_LOGIN_NOTE = (
    "ログインしなくても、ゲストとして参加をリクエストできます。ゲストが入れない会議や、"
    "承認なしで自分の会議に入りたいときだけログインしてください。"
    "会議ではログインしたアカウントの名前で表示されるので、ボット用に別の Google アカウントを使うのがおすすめです。"
)
_LOGIN_NEVER = "まだ確かめていません"
_SAVE_NOTE = "ボットの議事録は会議ごとに 1 ファイルで必ず保存されます（参加から退出まで）。"
_REVEAL_LABEL = "エクスプローラーで表示" if sys.platform == "win32" else "フォルダで表示"

# 欄の高さ（Mac 版と同じ）
_STATUS_H = 36
_LIVE_H = 160
_LIST_H = 132
_BODY_H = 220
_PANE_RADIUS = 9
_LIST_ROW_H = 40
# 発言欄に残す行数の上限（長い会議で欄が重くならないように。全文は議事録のファイルにある）
_LIVE_KEEP = 400


def _css(color: QColor, alpha: Optional[float] = None) -> str:
    """色を QSS / リッチテキスト用の文字列にする。"""
    a = color.alpha() if alpha is None else int(round(alpha * 255))
    return f"rgba({color.red()}, {color.green()}, {color.blue()}, {a})"


def _pane_qss(selector: str) -> str:
    """灰色の欄（本文の文字色の 7%・角丸 9）の見た目。"""
    p = theme.palette()
    return (
        f"{selector} {{ background: {_css(p.text, 0.07)}; border: none;"
        f" border-radius: {_PANE_RADIUS}px; padding: 6px 8px; color: {_css(p.text)};"
        f" font-size: 12px; selection-background-color: {_css(p.accent, 0.35)}; }}"
    )


class _StateGlyph(QWidget):
    """
    状態行の左に出す小さな印（待機 = 一時停止の丸、準備 = 砂時計、記録中 = 灯り、終了 = チェック、失敗 = ！）。
    """

    SIZE = 18

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFixedSize(self.SIZE, self.SIZE)
        self._state = rules.STATE_IDLE
        Theme.instance().changed.connect(self.update)

    def set_state(self, state: str) -> None:
        """出す印を切り替える。"""
        self._state = state
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = theme.palette()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(1.5, 1.5, self.SIZE - 3.0, self.SIZE - 3.0)
        center = rect.center()
        state = self._state
        if state in (rules.STATE_LAUNCHING, rules.STATE_JOINING, rules.STATE_WAITING_APPROVAL):
            icons.paint(painter, "hourglass", QRectF(1, 1, self.SIZE - 2, self.SIZE - 2), p.text_secondary)
            return
        if state == rules.STATE_RECORDING:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(p.accent.red(), p.accent.green(), p.accent.blue(), 60))
            painter.drawEllipse(rect)
            painter.setBrush(p.accent)
            painter.drawEllipse(center, 4.0, 4.0)
            return
        color = {rules.STATE_ENDED: p.ok, rules.STATE_FAILED: p.warn}.get(state, p.text)
        pen = QPen(color, 1.5)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(rect)
        x, y = center.x(), center.y()
        if state == rules.STATE_ENDED:
            check = QPainterPath()
            check.moveTo(x - 3.4, y + 0.2)
            check.lineTo(x - 0.9, y + 2.6)
            check.lineTo(x + 3.6, y - 2.6)
            painter.drawPath(check)
        elif state == rules.STATE_FAILED:
            painter.drawLine(QPointF(x, y - 3.6), QPointF(x, y + 0.6))
            painter.drawPoint(QPointF(x, y + 3.4))
        else:
            painter.drawLine(QPointF(x - 2.0, y - 2.8), QPointF(x - 2.0, y + 2.8))
            painter.drawLine(QPointF(x + 2.0, y - 2.8), QPointF(x + 2.0, y + 2.8))


class _TextPane(QTextEdit):
    """
    高さ固定の読むだけの欄（発言の流れ・議事録の本文）。中身が無いときは中央に案内を出す。

    Attributes:
        empty_label: 中身が無いときに中央へ出す案内
    """

    def __init__(self, height: int, empty_text: str, parent: Optional[QWidget] = None) -> None:
        """
        Args:
            height: 欄の高さ（固定）
            empty_text: 中身が無いときの案内
            parent: 親ウィジェット
        """
        super().__init__(parent)
        self.setReadOnly(True)
        self.setFixedHeight(height)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.document().setDocumentMargin(2)
        self.empty_label = Text(empty_text, size=11, role="secondary", wrap=True, parent=self)
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._restyle()
        Theme.instance().changed.connect(self._restyle)

    def _restyle(self) -> None:
        self.setStyleSheet(_pane_qss("QTextEdit"))

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self.empty_label.setGeometry(12, 0, max(0, self.width() - 24), self.height())

    def set_empty_text(self, text: str) -> None:
        """中身が無いときの案内を差し替える。"""
        self.empty_label.setText(text)

    def show_html(self, body: str) -> None:
        """中身を差し替えて先頭から見せる（空なら案内を出す）。"""
        self.setHtml(body)
        self.empty_label.setVisible(not body)
        self.verticalScrollBar().setValue(0)

    def append_html(self, line: str) -> None:
        """
        末尾に 1 行足す。

        いちばん下を見ているときだけ新しい行へ送る（上へ戻して読んでいる間は動かさない。
        QTextEdit.append がその動きをする）。
        """
        self.empty_label.setVisible(False)
        self.append(line)


class _TranscriptDelegate(QStyledItemDelegate):
    """過去の議事録の 1 行（日時・会議コードと長さ・「要約あり」）。選ばれた行は灯りのピル。"""

    def sizeHint(self, _option, _index: QModelIndex) -> QSize:  # noqa: N802
        return QSize(0, _LIST_ROW_H)

    def paint(self, painter: QPainter, option, index: QModelIndex) -> None:
        from PySide6.QtWidgets import QStyle

        info = index.data(Qt.ItemDataRole.UserRole)
        if info is None:
            return
        p = theme.palette()
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(option.rect).adjusted(0, 1, 0, -1)
        if selected or hovered:
            path = QPainterPath()
            path.addRoundedRect(rect, 7, 7)
            painter.fillPath(path, p.accent if selected else QColor(p.text.red(), p.text.green(), p.text.blue(), 14))
        primary = p.on_accent if selected else p.text
        secondary = QColor(p.on_accent) if selected else p.text_secondary
        if selected:
            secondary.setAlpha(215)

        # 「要約あり」の場所は、要約が無い行でも空けておく（行ごとに文字の幅が変わらないように）
        badge_font = theme.font(10)
        badge_w = QFontMetrics(badge_font).horizontalAdvance("要約あり")
        inner = rect.adjusted(10, 0, -10, 0)
        text_rect = QRectF(inner.left(), inner.top(), inner.width() - badge_w - 10, inner.height())

        title_font = theme.font(12, QFont.Weight.Medium)
        painter.setFont(title_font)
        painter.setPen(primary)
        painter.drawText(
            QRectF(text_rect.left(), text_rect.top() + 3, text_rect.width(), 17),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, info.title,
        )
        detail_font = theme.font(11)
        painter.setFont(detail_font)
        painter.setPen(secondary)
        detail = QFontMetrics(detail_font).elidedText(info.detail, Qt.TextElideMode.ElideRight, int(text_rect.width()))
        painter.drawText(
            QRectF(text_rect.left(), text_rect.top() + 19, text_rect.width(), 16),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, detail,
        )
        if info.has_summary:
            painter.setFont(badge_font)
            painter.drawText(inner, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, "要約あり")
        painter.restore()


class _TranscriptList(QListWidget):
    """過去の議事録の一覧（高さ固定）。空のときは中央に案内を出す。"""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(_LIST_H)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setMouseTracking(True)
        self.setItemDelegate(_TranscriptDelegate(self))
        self.empty_label = Text(_LIST_EMPTY, size=11, role="secondary", wrap=True, parent=self)
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._restyle()
        Theme.instance().changed.connect(self._restyle)

    def _restyle(self) -> None:
        self.setStyleSheet(_pane_qss("QListWidget") + " QListWidget { padding: 4px; outline: none; }")
        self.viewport().update()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self.empty_label.setGeometry(12, 0, max(0, self.width() - 24), self.height())


class MeetingPage(SettingsPage):
    """
    議事録・会議のページ。

    Attributes:
        url_input: 会議の URL
        join_button: 「参加」／「会議から退出」
        login_button: 参加に失敗したときだけ見せる「Google にログイン」（場所はいつも確保）
        status_text / status_hint: 状態行と補足
        live_view: 記録中の発言
        transcript_list: 過去の議事録
        mode_switch: 「本文」／「要約」
        body_view: 選んだ議事録の本文・要約
        summarize_button: 「要約」／「作り直す」／「止める」
        notice: 要約・コピーの知らせ（1 行）
        language_picker: 会議の言語
    """

    def __init__(self, ctx: PageContext, parent: Optional[QWidget] = None) -> None:
        super().__init__("議事録・会議", parent)
        self.ctx = ctx
        self._bot = getattr(ctx, "meetbot", None)
        self._state = self._bot.state if self._bot is not None else rules.STATE_IDLE
        self._finish_message = ""
        self._state_text = rules.state_text(self._state)
        self._live_count = 0
        self._login_state = rules.LOGIN_UNKNOWN
        self._summarizing: Optional[str] = None   # 要約している議事録のパス
        self._infos: List[transcript.TranscriptInfo] = []

        self._build_join()
        self._build_live()
        self._build_history()
        self._build_method()
        self._build_login()
        self._build_storage()
        self.finish()

        if self._bot is not None:
            self._bot.state_changed.connect(self._on_state)
            self._bot.line_confirmed.connect(self._on_line)
            self._bot.finished.connect(self._on_finished)
            self._bot.summary_progress.connect(self._on_summary_progress)
            self._bot.summary_finished.connect(self._on_summary_finished)
            self._bot.summary_failed.connect(self._on_summary_failed)
            self._bot.login_state_changed.connect(self._on_login_state)
        self.notice.show_message(_NOTICE_DEFAULT)
        self._render_state(rules.state_text(self._state))
        self._render_login()
        self.refresh()

    # ------------------------------------------------------------------
    # 画面の組み立て
    # ------------------------------------------------------------------

    def _build_join(self) -> None:
        self.add_section("会議に参加")
        group = self.add_group()

        block = BlockRow()
        status = QHBoxLayout()
        status.setContentsMargins(0, 0, 0, 0)
        status.setSpacing(8)
        self.status_glyph = _StateGlyph()
        self.status_text = MessageLabel(lines=1, size=13)
        self.status_text.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        status.addWidget(self.status_glyph)
        status.addWidget(self.status_text, 1)
        holder = QWidget()
        holder.setFixedHeight(_STATUS_H)
        holder.setLayout(status)
        block.body.addWidget(holder)
        self.status_hint = MessageLabel(lines=2)
        block.body.addWidget(self.status_hint)
        group.add_row(block)

        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText(_URL_PLACEHOLDER)
        self.url_input.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.url_input.setFixedHeight(Metrics.FIELD_H)
        self.url_input.setMinimumWidth(300)
        # Mac と同じく枠も地も無い入力欄（行そのものが欄に見える）
        self.url_input.setStyleSheet("QLineEdit { border: none; background: transparent; padding: 0; }")
        self.url_input.textChanged.connect(lambda _text: self._render_state(self._state_text))
        self.url_input.returnPressed.connect(self._on_join_clicked)
        group.add_row(SettingRow("会議の URL", trailing=self.url_input))

        self.login_button = CapsuleButton("Google にログイン")
        policy = self.login_button.sizePolicy()
        policy.setRetainSizeWhenHidden(True)
        self.login_button.setSizePolicy(policy)
        self.login_button.setVisible(False)
        self.login_button.clicked.connect(self._on_open_login)
        self.join_button = CapsuleButton("会議から退出")
        # 文字が「参加」⇔「会議から退出」と変わっても幅が変わらないよう、長い方の幅で固定する
        self.join_button.setFixedWidth(self.join_button.sizeHint().width())
        self.join_button.setText("参加")
        self.join_button.clicked.connect(self._on_join_clicked)
        self.join_row = SettingRow(
            "ボットを会議に入れる", trailing=trailing_box(self.login_button, self.join_button)
        )
        self.join_caption = add_row_message(self.join_row, lines=3)
        group.add_row(self.join_row)

    def _build_live(self) -> None:
        self.add_section("記録中の発言")
        group = self.add_group()
        block = BlockRow()
        self.live_view = _TextPane(_LIVE_H, _LIVE_EMPTY)
        block.body.addWidget(self.live_view)
        group.add_row(block)

    def _build_history(self) -> None:
        self.add_section("過去の議事録")
        group = self.add_group()
        block = BlockRow()
        block.body.setSpacing(8)
        self.transcript_list = _TranscriptList()
        self.transcript_list.currentRowChanged.connect(self._on_selected)
        block.body.addWidget(self.transcript_list)

        bar = QHBoxLayout()
        bar.setContentsMargins(0, 0, 0, 0)
        bar.setSpacing(8)
        self.mode_switch = Segmented(["本文", "要約"])
        self.mode_switch.currentChanged.connect(lambda _index: self._render_body())
        self.reveal_button = CapsuleButton(_REVEAL_LABEL)
        self.copy_button = CapsuleButton("コピー")
        self.summarize_button = CapsuleButton("作り直す")
        self.summarize_button.setFixedWidth(self.summarize_button.sizeHint().width())
        self.summarize_button.setText("要約")
        self.reveal_button.clicked.connect(self._on_reveal)
        self.copy_button.clicked.connect(self._on_copy)
        self.summarize_button.clicked.connect(self._on_summarize)
        bar.addWidget(self.mode_switch)
        bar.addStretch(1)
        bar.addWidget(self.reveal_button)
        bar.addWidget(self.copy_button)
        bar.addWidget(self.summarize_button)
        block.body.addLayout(bar)

        self.body_view = _TextPane(_BODY_H, _BODY_UNSELECTED)
        block.body.addWidget(self.body_view)
        self.notice = MessageLabel(lines=1)
        block.body.addWidget(self.notice)
        group.add_row(block)

    def _build_method(self) -> None:
        self.add_section("文字起こしの方法")
        group = self.add_group()
        group.add_row(SettingRow(
            "文字起こしの方法", _METHOD_CAPTION, trailing=Text("Meet の字幕", size=13, role="secondary"),
        ))
        self.language_picker = Picker()
        self.language_picker.addItem("日本語", "ja")
        self.language_picker.addItem("英語", "en")
        self.language_picker.activated.connect(self._on_language)
        group.add_row(SettingRow("会議の言語", _LANGUAGE_CAPTION, trailing=self.language_picker))
        self.add_note(_METHOD_NOTE)

    def _build_login(self) -> None:
        self.add_section("ボット用ブラウザのログイン（任意）")
        group = self.add_group()
        self.open_login_button = CapsuleButton("ログイン画面を開く")
        self.open_login_button.clicked.connect(self._on_open_login)
        self.login_row = SettingRow(rules.LOGIN_UNKNOWN, trailing=self.open_login_button)
        # 見出しは状態で文字が変わる。漢字だけの文とカナ混じりの文で行の高さが違うことがあるので、
        # いちばん高い文に合わせて固定する（状態が変わっても下の欄が 1px も動かないように）
        title = self.login_row.title_label
        title_h = 0
        for text in (rules.LOGIN_UNKNOWN, rules.LOGIN_CHECKING, rules.LOGIN_DONE, rules.LOGIN_NONE, rules.LOGIN_WAITING):
            title.setText(text)
            title_h = max(title_h, title.sizeHint().height())
        title.setFixedHeight(title_h)
        self.login_caption = add_row_message(self.login_row, lines=1)
        self._login_checked: Optional[datetime] = None
        group.add_row(self.login_row)
        self.add_note(_LOGIN_NOTE)

    def _build_storage(self) -> None:
        self.add_section("保存先")
        group = self.add_group()
        block = BlockRow()
        block.body.addWidget(Text("保存先", size=13))
        self.directory_label = MessageLabel(lines=1)
        block.body.addWidget(self.directory_label)
        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.setSpacing(8)
        self.open_latest_button = CapsuleButton("最新の議事録を開く")
        self.open_folder_button = CapsuleButton("保存フォルダを開く")
        self.open_latest_button.clicked.connect(self._on_open_latest)
        self.open_folder_button.clicked.connect(self._on_open_folder)
        buttons.addWidget(self.open_latest_button)
        buttons.addWidget(self.open_folder_button)
        buttons.addStretch(1)
        block.body.addLayout(buttons)
        group.add_row(block)
        self.add_note(_SAVE_NOTE)

    # ------------------------------------------------------------------
    # 開いたとき
    # ------------------------------------------------------------------

    def refresh(self) -> None:
        """ページを開いたとき。設定・一覧・保存先を読み直す（参加の状態は知らせで追っているので触らない）。"""
        language = str(self.ctx.get(CONFIG_CAPTION_LANGUAGE, "ja") or "ja")
        index = self.language_picker.findData("en" if language == "en" else "ja")
        self.language_picker.setCurrentIndex(max(0, index))
        self.directory_label.show_message(str(self._directory()))
        self._chrome_found = chrome.find_chrome() is not None
        self._prefill_url()
        self._reload_list()
        self._render_state(self._state_text)

    def _directory(self) -> Path:
        """議事録の保存先。"""
        return transcript.default_directory()

    def _prefill_url(self) -> None:
        """入力欄が空で、クリップボードに会議の URL があれば入れておく。"""
        if self.url_input.text().strip() or self._is_active():
            return
        clip = QApplication.clipboard().text().strip()
        if clip and len(clip) < 200 and rules.normalize_meet_url(clip):
            self.url_input.setText(clip)

    # ------------------------------------------------------------------
    # 会議に参加
    # ------------------------------------------------------------------

    def _is_active(self) -> bool:
        return self._state in (
            rules.STATE_LAUNCHING, rules.STATE_JOINING, rules.STATE_WAITING_APPROVAL, rules.STATE_RECORDING,
        )

    def _on_join_clicked(self) -> None:
        if self._bot is None:
            return
        if self._is_active():
            self._bot.stop()
            return
        self._finish_message = ""
        refused = self._bot.start(self.url_input.text())
        if refused:
            # 断られた理由は補足の欄に出す（状態は変えない）
            self.status_hint.show_message(refused, "warn")

    def _on_state(self, state: str, text: str) -> None:
        if state == rules.STATE_LAUNCHING and not self._is_active():
            # 新しい会議が始まった。前の会議の発言を片付ける
            self.live_view.show_html("")
            self._live_count = 0
        self._state = state
        self._render_state(text)

    def _on_finished(self, _path: str, message: str) -> None:
        self._finish_message = message
        self._render_state(self._state_text)
        self._reload_list()

    def _on_line(self, speaker: str, time: str, text: str) -> None:
        p = theme.palette()
        parts = [f'<span style="font-family: Consolas, Menlo, monospace; color: {_css(p.text_secondary)};">'
                 f"{html.escape(time)}</span>"]
        if speaker:
            parts.append(f'<span style="font-weight: 600;">{html.escape(speaker)}</span>')
        parts.append(html.escape(text))
        if self._live_count >= _LIVE_KEEP:
            # 古い行を落とす（先頭の 1 段落を消す）
            cursor = self.live_view.textCursor()
            cursor.movePosition(cursor.MoveOperation.Start)
            cursor.select(cursor.SelectionType.BlockUnderCursor)
            cursor.removeSelectedText()
            cursor.deleteChar()
        else:
            self._live_count += 1
        self.live_view.append_html("&nbsp; ".join(parts))
        if self._live_count == 1:
            # 最初の発言で、補足を「書き始めます」から保存先の案内に替える
            self._render_state(self._state_text)

    def _render_state(self, text: str) -> None:
        """状態行・補足・ボタン・入力欄を、いまの状態に合わせる（どれも場所と大きさは変えない）。"""
        state = self._state
        active = self._is_active()
        self._state_text = text or rules.state_text(state)
        self.status_glyph.set_state(state)
        self.status_text.show_message(self._state_text, "primary")

        if self._bot is None:
            hint, kind = _HINT_NO_SERVICE, "secondary"
        elif state in (rules.STATE_LAUNCHING, rules.STATE_JOINING):
            hint, kind = _HINT_STARTING, "secondary"
        elif state == rules.STATE_WAITING_APPROVAL:
            hint, kind = _HINT_WAITING, "secondary"
        elif state == rules.STATE_RECORDING:
            hint = f"保存先: {self._directory()}" if self._live_count else _HINT_RECORDING
            kind = "secondary"
        elif state == rules.STATE_ENDED:
            hint, kind = self._finish_message, "secondary"
        elif state == rules.STATE_FAILED:
            hint, kind = _HINT_FAILED, "warn"
        elif not getattr(self, "_chrome_found", True):
            hint, kind = _HINT_NO_CHROME, "warn"
        else:
            hint, kind = _HINT_IDLE, "secondary"
        self.status_hint.show_message(hint, kind)

        self.join_button.setText("会議から退出" if active else "参加")
        # 退出はいつでも押せる。参加は URL が入っているときだけ（Mac と同じ）
        self.join_button.setEnabled(self._bot is not None and (active or bool(self.url_input.text().strip())))
        self.url_input.setEnabled(not active)
        self.language_picker.setEnabled(not active)
        self.login_button.setVisible(state == rules.STATE_FAILED)
        self.open_login_button.setEnabled(self._bot is not None and not active)

    # ------------------------------------------------------------------
    # 過去の議事録
    # ------------------------------------------------------------------

    def _reload_list(self) -> None:
        """一覧を読み直す（選んでいた議事録は選んだままにする）。"""
        selected = self._selected()
        keep = str(selected.path) if selected is not None else None
        self._infos = transcript.list_transcripts(self._directory())
        self.transcript_list.blockSignals(True)
        self.transcript_list.clear()
        row_to_select = -1
        for row, info in enumerate(self._infos):
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, info)
            self.transcript_list.addItem(item)
            if keep is not None and str(info.path) == keep:
                row_to_select = row
        self.transcript_list.setCurrentRow(row_to_select)
        self.transcript_list.blockSignals(False)
        self.transcript_list.empty_label.setVisible(not self._infos)
        self.open_latest_button.setEnabled(bool(self._infos))
        self._render_body()

    def _selected(self) -> Optional[transcript.TranscriptInfo]:
        row = self.transcript_list.currentRow()
        return self._infos[row] if 0 <= row < len(self._infos) else None

    def _on_selected(self, _row: int) -> None:
        self.notice.show_message(_NOTICE_DEFAULT)
        self._render_body()

    def _render_body(self) -> None:
        """選んだ議事録の本文か要約を出し、操作のボタンを合わせる。"""
        info = self._selected()
        summary_mode = self.mode_switch.current_index() == 1
        body = ""
        if info is None:
            self.body_view.set_empty_text(_SUMMARY_UNSELECTED if summary_mode else _BODY_UNSELECTED)
        elif summary_mode:
            self.body_view.set_empty_text(_SUMMARY_NONE)
            body = self._summary_html(transcript.read_summary(info.path) or "")
        else:
            self.body_view.set_empty_text(_BODY_EMPTY)
            body = self._lines_html(transcript.parse_lines(transcript.read_text(info.path) or ""))
        self.body_view.show_html(body)

        busy = self._summarizing is not None
        self.reveal_button.setEnabled(info is not None)
        self.copy_button.setEnabled(info is not None and bool(body))
        self.summarize_button.setEnabled(self._bot is not None and (busy or (info is not None and info.line_count > 0)))
        if busy:
            self.summarize_button.setText("止める")
        else:
            self.summarize_button.setText("作り直す" if info is not None and info.has_summary else "要約")

    @staticmethod
    def _lines_html(lines) -> str:
        """議事録の発言を、時刻（等幅）＋話者（半太字）＋本文の段落にする。"""
        p = theme.palette()
        out = []
        for line in lines:
            parts = []
            if line.time:
                parts.append(
                    f'<span style="font-family: Consolas, Menlo, monospace; color: {_css(p.text_secondary)};">'
                    f"{html.escape(line.time)}</span>"
                )
            if line.speaker:
                parts.append(f'<span style="font-weight: 600;">{html.escape(line.speaker)}</span>')
            parts.append(html.escape(line.text))
            out.append(f'<p style="margin: 0 0 5px 0;">{"&nbsp; ".join(parts)}</p>')
        return "".join(out)

    @staticmethod
    def _summary_html(text: str) -> str:
        """要約を段落にする（「## 見出し」は太字にする）。"""
        out = []
        for raw in text.strip().splitlines():
            line = raw.rstrip()
            if not line:
                out.append('<p style="margin: 0; font-size: 5px;">&nbsp;</p>')
            elif line.startswith("#"):
                out.append(f'<p style="margin: 6px 0 3px 0; font-weight: 700;">{html.escape(line.lstrip("#").strip())}</p>')
            else:
                out.append(f'<p style="margin: 0 0 2px 0;">{html.escape(line)}</p>')
        return "".join(out)

    def _on_reveal(self) -> None:
        info = self._selected()
        if info is None:
            return
        if sys.platform == "win32":
            subprocess.Popen(["explorer", f"/select,{info.path}"])
        else:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(info.path.parent)))

    def _on_copy(self) -> None:
        info = self._selected()
        if info is None:
            return
        if self.mode_switch.current_index() == 1:
            text = transcript.read_summary(info.path) or ""
        else:
            text = transcript.read_text(info.path) or ""
        if text:
            QApplication.clipboard().setText(text)
            self.notice.show_message("コピーしました", "ok")

    def _on_summarize(self) -> None:
        if self._bot is None:
            return
        if self._summarizing is not None:
            self._bot.cancel_summary()
            return
        info = self._selected()
        if info is None:
            return
        refused = self._bot.summarize(str(info.path))
        if refused:
            self.notice.show_message(refused, "warn")
            return
        self._summarizing = str(info.path)
        self.notice.show_message("要約の準備中…")
        self._render_body()

    def _on_summary_progress(self, _path: str, message: str) -> None:
        self.notice.show_message(f"{message}…")

    def _on_summary_finished(self, path: str, _text: str, message: str) -> None:
        self._summarizing = None
        self.notice.show_message(message, "ok")
        self._reload_list()
        info = self._selected()
        if info is not None and str(info.path) == path:
            # できた要約をそのまま見せる
            self.mode_switch.set_current_index(1, animate=False, notify=False)
            self._render_body()

    def _on_summary_failed(self, _path: str, message: str) -> None:
        self._summarizing = None
        self.notice.show_message(message, "warn")
        self._render_body()

    # ------------------------------------------------------------------
    # 設定・ログイン・保存先
    # ------------------------------------------------------------------

    def _on_language(self, _index: int) -> None:
        self.ctx.commit({CONFIG_CAPTION_LANGUAGE: self.language_picker.currentData()})

    def _on_open_login(self) -> None:
        if self._bot is None:
            return
        refused = self._bot.open_login()
        if refused:
            self.login_caption.show_message(refused, "warn")

    def _on_login_state(self, state: str) -> None:
        self._login_state = state
        if state in (rules.LOGIN_DONE, rules.LOGIN_NONE):
            self._login_checked = datetime.now()
        self._render_login()

    def _render_login(self) -> None:
        """ログインの状態を、ログインの行と「ボットを会議に入れる」の説明に出す。"""
        self.login_row.title_label.setText(self._login_state)
        checked = self._login_checked
        self.login_caption.show_message(
            f"最終確認: {checked.strftime('%Y/%m/%d %H:%M')}" if checked else _LOGIN_NEVER
        )
        self.join_caption.show_message(
            _JOIN_ACCOUNT if self._login_state == rules.LOGIN_DONE else _JOIN_GUEST
        )

    def _on_open_latest(self) -> None:
        if self._infos:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._infos[0].path)))

    def _on_open_folder(self) -> None:
        directory = self._directory()
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(directory)))
