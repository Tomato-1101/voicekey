"""
設定ウィンドウモジュール

Mac 版のメインウィンドウと同じ「左のサイドバー＋右の本文」の 1 枚窓。
窓の骨格（サイドバー・本文の切り替え）は WindowShell、各ページの中身は ui/pages/ にあり、
ここはページの並びと、ページ間で共有する PageContext の受け渡しだけを持つ。

保存ボタンは無い。各ページが変えた項目をその場で保存する（入力欄は手が止まってから）。
"""

from typing import Optional

from PySide6.QtCore import Signal

from ..config import ConfigManager
from ..platform import get_platform_adapter
from .pages.about import AboutPage
from .pages.api_keys import ApiKeysPage
from .pages.context import PageContext
from .pages.dictionary import DictionaryPage
from .pages.general import GeneralPage, appearance_from
from .pages.meeting import MeetingPage
from .pages.translate import TranslatePage
from .pages.slot import SlotPage
from .shell import WindowShell
from .theme import Theme

# 最後に開いていたページを覚えておく設定キー（次に開いたときにそこから始める。Mac と同じ）
LAST_PAGE_KEY = "last_settings_page"
HOME_KEY = "home"


class SettingsWindow(WindowShell):
    """
    設定ウィンドウ。

    Signals:
        settings_saved: 設定を保存し終えた（アプリ本体はこれを受けて変更をすぐ反映する）
        open_onboarding_requested: 「セットアップガイド」をもう一度開いてほしい
    """

    settings_saved = Signal()
    open_onboarding_requested = Signal()

    def __init__(
        self,
        platform_adapter=None,
        history=None,
        stats=None,
        config_manager: Optional[ConfigManager] = None,
        updater=None,
        history_sync=None,
        api_usage=None,
        meetbot=None,
    ) -> None:
        """
        Args:
            platform_adapter: OS 依存の処理（None なら今の OS のものを使う）
            history: 音声入力の履歴（ダッシュボードで使う）
            stats: 使用実績（ダッシュボードで使う）
            config_manager: アプリ本体と共有する設定（None なら単体で作る）
            updater: 自動アップデータ（None ならバージョン情報の更新ボタンは押せない）
            history_sync: Mac との履歴共有（None なら同期の状況は出さない）
            api_usage: API 料金の記録（ダッシュボードで使う）
            meetbot: Google Meet 議事録ボット（議事録・会議ページで使う）
        """
        super().__init__()
        self.setWindowTitle("voicekey")
        self._config_manager = config_manager or ConfigManager()
        self.ctx = PageContext(
            self._config_manager,
            platform=platform_adapter or get_platform_adapter(),
            history=history,
            stats=stats,
            updater=updater,
            history_sync=history_sync,
            api_usage=api_usage,
            meetbot=meetbot,
            parent=self,
        )
        self.ctx.settings_saved.connect(self.settings_saved)
        self.ctx.open_onboarding_requested.connect(self.open_onboarding_requested)

        ctx = self.ctx
        self.add_page(HOME_KEY, "ダッシュボード", self._make_home, "grid")
        self.add_section("音声入力")
        self.add_page("api_keys", "API キー", lambda: ApiKeysPage(ctx), "key")
        self.add_page("slot1", "録音キー 1（メイン）", lambda: SlotPage(ctx, 1), "one")
        self.add_page("slot2", "録音キー 2（サブ）", lambda: SlotPage(ctx, 2), "two")
        self.add_page("dictionary", "ユーザー辞書", lambda: DictionaryPage(ctx), "book")
        self.add_page("translate", "翻訳して入力", lambda: TranslatePage(ctx), "translate")
        # ライブ字幕は Windows に無いので、この区分は議事録だけ（並びは Mac と同じ）
        self.add_section("字幕と議事録")
        self.add_page("meeting", "議事録・会議", lambda: MeetingPage(ctx), "people")
        self.add_section("アプリ")
        self.add_page("general", "一般", lambda: GeneralPage(ctx), "gear")
        self.add_page("about", "バージョン情報", lambda: AboutPage(ctx), "info")

        # 保存されている外観で始める（Theme.changed で WindowShell が標準部品の見た目も合わせる）
        Theme.instance().set_mode(appearance_from(ctx.get))
        self._apply_appearance()

        last = ctx.get(LAST_PAGE_KEY, HOME_KEY)
        self.select(last if last in self.keys() else HOME_KEY, animate=False)
        self.pageChanged.connect(self._on_page_changed)

    def _make_home(self):
        # ダッシュボードはメインセッション側の実装（pages/home.py）。ここでは作って配線するだけ
        from .pages.home import HomePage

        page = HomePage(self.ctx)
        page.navigate.connect(self.select)
        return page

    # ----- 公開 -----

    def select_home(self) -> None:
        """ダッシュボードを開く（サイドノッチ・トレイの「ホームを開く」から呼ぶ）。"""
        self.select(HOME_KEY)

    def refresh_history_views(self) -> None:
        """
        Mac との同期で履歴が変わったとき、開いたことのあるページの表示を作り直す。

        本体からキュー接続（UI のスレッド）で呼ばれる。まだ作っていないページは開くときに読むので触らない。
        """
        home = self.created_page(HOME_KEY)
        if home is not None and hasattr(home, "refresh"):
            home.refresh()
        general = self.created_page("general")
        if general is not None:
            general.refresh_sync_status()

    # ----- 内部 -----

    def _on_page_changed(self, key: str) -> None:
        """ページを移ったとき。書きかけの入力を保存し、開いたページを最新にし、場所を覚える。"""
        self.ctx.flush()
        page = self.created_page(key)
        if page is not None and hasattr(page, "refresh"):
            page.refresh()
        # 場所の記録は設定の変更ではないので、本体に「変わった」と知らせずに書くだけにする
        self._config_manager.save({LAST_PAGE_KEY: key})

    def showEvent(self, event) -> None:  # noqa: N802
        """
        開くたびに、いま見えるページを読み直す。

        窓は閉じても捨てずに使い回すので、閉じている間にトレイや設定ファイルで変わった値を拾う。
        """
        super().showEvent(event)
        page = self.created_page(self.current_key())
        if page is not None and hasattr(page, "refresh"):
            page.refresh()

    def hideEvent(self, event) -> None:  # noqa: N802
        """閉じる・隠すときは、書きかけの入力を保存し、マイクテストを必ず止める。"""
        self.ctx.flush()
        for key in self.keys():
            page = self.created_page(key)
            if page is not None and hasattr(page, "stop_mic_test"):
                page.stop_mic_test()
        super().hideEvent(event)

    def closeEvent(self, event) -> None:  # noqa: N802
        """閉じるときも書きかけの入力を保存する（hideEvent が来ない閉じ方に備える）。"""
        self.ctx.flush()
        super().closeEvent(event)
