"""
API キーのページ

文字起こしエンジンごとの API キーを、Windows の資格情報マネージャーへ保存・削除する。
並び・見出し・文言は Mac 版 ApiKeysTab と同じ（Keychain を資格情報マネージャーに読み替え、
Apple ローカルと Gemini の行は Windows に無いので置かない）。

キーの値は画面にもログにも出さない。保存した後は入力欄を空に戻し、出すのは「どこから読んだか」だけ。
"""

import os
from dataclasses import dataclass
from typing import Dict, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QHBoxLayout, QLineEdit, QWidget

from ...core import engines
from ...utils import secrets
from ..components import CapsuleButton, MessageLabel, Text
from ..theme import Metrics
from .context import PageContext
from .widgets import BlockRow, SettingsPage


@dataclass(frozen=True)
class KeyField:
    """
    API キーの欄 1 つ分。

    Attributes:
        title: 行の見出し
        service: 資格情報マネージャーのサービス名
        env_var: 環境変数名（資格情報マネージャーに無いときに読む）
        note: 見出しの下の補足（無ければ空）
        placeholder: 入力欄の薄い文字
        secret: 入力中も伏字にするなら True（接続先 URL だけは読めるようにする）
    """

    title: str
    service: str
    env_var: str
    note: str = ""
    placeholder: str = "API キーを入力"
    secret: bool = True


def _fields() -> tuple:
    """Mac 版と同じ並びの欄を作る（サービス名・環境変数名はエンジンのカタログから読む）。"""
    soniox = engines.engine("soniox")
    openai = engines.engine("openai")
    azure = engines.engine("azure_mai")
    eleven = engines.engine("elevenlabs")
    groq = engines.engine("groq")
    return (
        KeyField("Soniox", soniox.keyring_service, soniox.env_var),
        KeyField("OpenAI", openai.keyring_service, openai.env_var, note="文字起こし（REST / ライブ）で共用"),
        KeyField("Microsoft MAI（キー）", azure.keyring_service, azure.env_var),
        KeyField(
            "Microsoft MAI（エンドポイント）",
            azure.endpoint_service,
            azure.endpoint_env_var,
            placeholder="https://<リソース名>.cognitiveservices.azure.com",
            secret=False,
        ),
        KeyField("ElevenLabs", eleven.keyring_service, eleven.env_var),
        KeyField(
            "Groq", groq.keyring_service, groq.env_var, note="文字起こしと文章の自動整形・翻訳入力で共用"
        ),
    )


FIELDS = _fields()

_INTRO = (
    "使う文字起こしエンジンのキーだけ入れれば動きます。キーはこの PC の Windows の資格情報マネージャー"
    "（アプリ専用の項目）に保存され、画面やログには表示しません。"
)
_SAVE_FAILED = (
    "保存できませんでした（Windows の資格情報マネージャーに書き込めませんでした）。もう一度お試しください"
)
_DELETE_FAILED = (
    "削除できませんでした（Windows の資格情報マネージャーから消せませんでした）。もう一度お試しください"
)


class _KeyRow(BlockRow):
    """
    API キーの 1 行（上: 見出しと状態、中: 補足、下: 入力欄と保存・削除、最下段: 失敗の知らせ）。

    状態は作ったときに 1 回だけ調べる（資格情報マネージャーの読み出しは遅いことがあるため、
    描き直しのたびには読まない）。以後は保存・削除の結果で書き換える。
    """

    # 状態の欄の幅（文言が変わっても見出しや入力欄が動かないように固定する）
    STATUS_W = 170

    def __init__(self, field: KeyField, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.field = field

        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.addWidget(Text(field.title, size=13, weight=QFont.Weight.Medium))
        head.addStretch(1)
        self.status = MessageLabel(lines=1)
        self.status.setFixedWidth(self.STATUS_W)
        self.status.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        head.addWidget(self.status)
        self.body.addLayout(head)

        if field.note:
            self.body.addWidget(Text(field.note, size=11, role="secondary"))

        line = QHBoxLayout()
        line.setContentsMargins(0, 0, 0, 0)
        line.setSpacing(8)
        self.input = QLineEdit()
        self.input.setPlaceholderText(field.placeholder)
        self.input.setFixedHeight(Metrics.FIELD_H)
        if field.secret:
            self.input.setEchoMode(QLineEdit.EchoMode.Password)
        self.save_button = CapsuleButton("保存")
        self.delete_button = CapsuleButton("削除")
        line.addWidget(self.input, 1)
        line.addWidget(self.save_button)
        line.addWidget(self.delete_button)
        self.body.addLayout(line)

        self.error = MessageLabel(lines=1)
        self.body.addWidget(self.error)

        self.input.textChanged.connect(self._update_buttons)
        self.input.returnPressed.connect(self.save)
        self.save_button.clicked.connect(self.save)
        self.delete_button.clicked.connect(self.delete)

        self._stored = self._read_stored()
        self._show_status()

    def _read_stored(self) -> bool:
        """アプリ（資格情報マネージャー）に値が入っているかを調べる。"""
        if not secrets.is_keyring_available():
            return False
        return bool(secrets.get_api_key(self.field.service))

    def _show_status(self) -> None:
        """右上の状態（どこから読んでいるか）と、ボタンの押せる・押せないを合わせる。"""
        if self._stored:
            self.status.show_message("✓ アプリに保存済み", "ok")
        elif os.environ.get(self.field.env_var):
            self.status.show_message("✓ 環境変数から読込", "ok")
        else:
            self.status.show_message("未設定", "secondary")
        self._update_buttons()

    def _update_buttons(self) -> None:
        """保存は入力があるときだけ、削除はアプリに保存した値があるときだけ押せる。"""
        self.save_button.setEnabled(bool(self.input.text().strip()))
        # 環境変数には触らない（アプリが消せるのは自分で保存した値だけ）
        self.delete_button.setEnabled(self._stored)

    def save(self) -> None:
        """入力した値を保存する。成功したら入力欄を空にする（値を画面に残さない）。"""
        value = self.input.text().strip()
        if not value:
            return
        if not secrets.set_api_key(self.field.service, value):
            # 入力は残す（打ち直さずにもう一度押せるように）
            self.error.show_message(_SAVE_FAILED, "danger")
            return
        self.error.show_message("")
        self.input.clear()
        self._stored = True
        self._show_status()

    def delete(self) -> None:
        """アプリに保存した値を消す（環境変数の値は残る）。"""
        if not secrets.delete_api_key(self.field.service):
            self.error.show_message(_DELETE_FAILED, "danger")
            return
        self.error.show_message("")
        self.input.clear()
        self._stored = False
        self._show_status()


class ApiKeysPage(SettingsPage):
    """
    API キーのページ。

    Attributes:
        rows: サービス名 → 行
        inputs: サービス名 → 入力欄（テストと他ページからの参照用）
    """

    def __init__(self, ctx: PageContext, parent: Optional[QWidget] = None) -> None:
        super().__init__("API キー", parent)
        self.ctx = ctx

        intro = BlockRow()
        intro.body.addWidget(Text(_INTRO, size=11, role="secondary", wrap=True))
        self.add_group().add_row(intro)

        self.add_section("文字起こし")
        group = self.add_group()
        self.rows: Dict[str, _KeyRow] = {}
        for field in FIELDS:
            row = _KeyRow(field)
            group.add_row(row)
            self.rows[field.service] = row
        self.inputs: Dict[str, QLineEdit] = {service: row.input for service, row in self.rows.items()}
        self.finish()
