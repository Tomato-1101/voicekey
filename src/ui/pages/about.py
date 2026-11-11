"""
バージョン情報のページ

並び・文言は Mac 版 AboutTab に合わせる（現在のバージョン・状態行・更新ボタン）。
更新の仕組みは Windows の Updater（起動時と 1 日ごとに確認・インストーラを裏で実行）なので、
フッタの説明と「今すぐ更新する」の流れは旧 Windows 画面のものを使う。
"""

from typing import Optional

from PySide6.QtWidgets import QHBoxLayout, QWidget

from ...config.constants import APP_VERSION
from ..components import CapsuleButton, MessageLabel, SettingRow, Text
from .context import PageContext
from .widgets import BlockRow, SettingsPage

_FOOTER = (
    "新しいバージョンが見つかると「今すぐ更新する」が押せるようになります。"
    "更新は起動時と 1 日ごとに自動で確認されます。"
)


class AboutPage(SettingsPage):
    """
    バージョン情報のページ。

    Attributes:
        status: 更新の状態を出す欄（高さ固定）
        check_button: 「アップデートを確認」
        update_button: 「今すぐ更新する」（新しい版が見つかるまでは押せない）
    """

    def __init__(self, ctx: PageContext, parent: Optional[QWidget] = None) -> None:
        super().__init__("バージョン情報", parent)
        self.ctx = ctx
        self._updater = ctx.updater
        # 新しい版を見つけているか（失敗のあとで「今すぐ更新する」を押せるようにするかの判断に使う）
        self._found = False

        self.add_section("バージョン情報")
        group = self.add_group()
        group.add_row(SettingRow("現在のバージョン", trailing=Text(APP_VERSION, size=13, role="secondary")))

        block = BlockRow()
        self.status = MessageLabel(lines=1)
        block.body.addWidget(self.status)
        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.setSpacing(8)
        self.check_button = CapsuleButton("アップデートを確認")
        # ボタンを出し入れすると隣がずれるので、いつも置いておき、見つかるまでは押せなくする
        self.update_button = CapsuleButton("今すぐ更新する", kind="primary")
        self.update_button.setEnabled(False)
        buttons.addWidget(self.check_button)
        buttons.addWidget(self.update_button)
        buttons.addStretch(1)
        block.body.addLayout(buttons)
        group.add_row(block)
        self.add_note(_FOOTER)
        self.finish()

        if self._updater is None:
            # 開発実行など、更新の仕組みが無いビルド
            self.check_button.setEnabled(False)
            self.status.show_message("このビルドでは自動アップデートは利用できません")
            return
        self.check_button.clicked.connect(self._on_check)
        self.update_button.clicked.connect(self._on_update_now)
        # 窓を開いたまま裏の確認で見つかっても表示に出るよう、知らせを受け続ける
        self._updater.update_available.connect(self._on_available)
        self._updater.up_to_date.connect(self._on_up_to_date)
        self._updater.update_failed.connect(self._on_failed)

    def _on_check(self) -> None:
        self.status.show_message("確認中…")
        self._updater.check_now(manual=True)

    def _on_available(self, version: str) -> None:
        self._found = True
        self.status.show_message(f"新しいバージョン {version} が利用可能です", "ok")
        self.update_button.setEnabled(True)

    def _on_up_to_date(self) -> None:
        self._found = False
        self.status.show_message("最新です")
        self.update_button.setEnabled(False)

    def _on_failed(self, message: str) -> None:
        self.status.show_message(message or "更新に失敗しました", "warn")
        # 失敗したらもう一度押せるようにする（新しい版を見つけていた場合だけ意味がある）
        self.update_button.setEnabled(self._found)

    def _on_update_now(self) -> None:
        self.update_button.setEnabled(False)
        self.status.show_message("更新を準備中… 完了するとアプリが再起動します")
        self._updater.download_and_install()
