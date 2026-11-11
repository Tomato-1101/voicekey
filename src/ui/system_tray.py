"""
システムトレイモジュール

タスクバー通知領域にアイコンを表示し、
アプリケーション状態の表示とコンテキストメニューを提供する。
"""

from typing import Optional, Union

from PySide6.QtCore import QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from ..config.types import AppState
from ..core import api_pricing
from ..platform import PlatformAdapter, get_platform_adapter
from . import brand


class SystemTray(QSystemTrayIcon):
    """
    動的ステータス表示付きシステムトレイアイコン。
    
    アプリケーション状態に応じてアイコンの灯りが変化し、
    設定画面や終了へのアクセスをコンテキストメニューで提供する。
    
    メニューの並びは Mac 版のメニューバーと同じ（更新／状態／今日の API 代／ホーム／設定…／
    フィードバック／終了）。強制リセットだけは Windows 固有の脱出口として終了の上に置く。

    Signals:
        open_home: ホーム（ダッシュボード）を開く要求
        open_settings: 設定を開く要求（最後に開いていたページ）
        force_reset: 録音状態の強制リセット要求（フリーズ復帰用）
        quit_app: アプリケーション終了要求
        install_update: 新バージョンのインストール要求（自動アップデート）
    """

    # メニューアクション用シグナル
    open_home = Signal()
    open_settings = Signal()
    open_meeting = Signal()
    force_reset = Signal()
    quit_app = Signal()
    install_update = Signal()
    
    def __init__(
        self,
        platform_adapter: Optional[PlatformAdapter] = None,
        parent=None
    ) -> None:
        """システムトレイアイコンを初期化する。"""
        super().__init__(parent)
        self._platform = platform_adapter or get_platform_adapter()
        # 今日の API 代を数える元（set_api_usage で渡されるまでは行を出さない）
        self._api_usage = None
        # いまの料金の数え方を返す関数（無ければ定価）
        self._cost_mode = None

        self._setup_icon()
        self._setup_menu()
        # トレイアイコンの左クリックで直接 Settings を開かない仕様に変更。
        # 左クリックでは setContextMenu で登録したメニューを表示するのみで、
        # ユーザーがメニューから「Settings」を選んだ時に初めてウィンドウを開く。

        self.show()

    def _setup_icon(self) -> None:
        """初期アイコンを設定する。"""
        self._set_icon(AppState.IDLE)

    # 状態行の文言（Mac 版の AppState.label と同じ）
    _STATE_LABELS = {
        AppState.IDLE: "待機中",
        AppState.RECORDING: "録音中",
        AppState.RECORDING_AUTO_ENTER: "録音中（自動送信）",
        AppState.TRANSCRIBING: "変換中…",
    }

    def _setup_menu(self) -> None:
        """コンテキストメニューを設定する（並びは Mac 版のメニューバーと同じ）。"""
        self._menu = QMenu()

        # 新バージョンを検知したときだけ、いちばん上に出す（モーダルは出さない方針）
        self._update_action = self._menu.addAction("")
        self._update_action.triggered.connect(self.install_update.emit)
        self._update_action.setVisible(False)
        self._update_separator = self._menu.addSeparator()
        self._update_separator.setVisible(False)

        # 状態と今日の API 代は読むだけの行（押せない）
        self._state_action = self._menu.addAction(self._STATE_LABELS[AppState.IDLE])
        self._state_action.setEnabled(False)
        self._cost_action = self._menu.addAction("")
        self._cost_action.setEnabled(False)
        self._cost_action.setVisible(False)

        self._menu.addSeparator()

        home_action = self._menu.addAction("ホーム")
        home_action.triggered.connect(self.open_home.emit)
        settings_action = self._menu.addAction("設定…")
        settings_action.triggered.connect(self.open_settings.emit)
        # Google Meet 議事録ボット（Mac と同じサブメニュー。ボットが渡されるまでは出さない）
        self._meetbot = None
        self._meet_menu = QMenu(self._MEET_TITLE)
        self._meet_menu.aboutToShow.connect(self._rebuild_meet_menu)
        self._meet_action = self._menu.addMenu(self._meet_menu)
        self._meet_action.setVisible(False)
        # 翻訳して入力（常時）。Mac のメニューと同じ位置のチェック項目（set_translate_toggle で出す）
        self._translate_get = None
        self._translate_set = None
        self._translate_action = self._menu.addAction("翻訳して入力（常時）")
        self._translate_action.setCheckable(True)
        self._translate_action.setVisible(False)
        self._translate_action.triggered.connect(self._on_translate_triggered)

        # セットアップガイドの再表示項目は置かない（Mac と同じく起動時の自動判定のみで表示する。
        # ユーザー指示: 「起動時にだけ表示するようにして」）。

        feedback_action = self._menu.addAction("フィードバックを送る…")
        feedback_action.triggered.connect(self._send_feedback)

        self._menu.addSeparator()

        # 録音/マイクが詰まったときに再起動なしで内部状態を作り直す脱出口（Windows 固有）
        reset_action = self._menu.addAction("強制リセット（フリーズ復帰）")
        reset_action.triggered.connect(self.force_reset.emit)

        quit_action = self._menu.addAction("voicekey を終了")
        quit_action.triggered.connect(self.quit_app.emit)

        # 料金は開くたびに数え直す（録音のたびに更新するより安く、開いた時点の値が必ず出る）
        self._menu.aboutToShow.connect(self._refresh_cost)
        self._menu.aboutToShow.connect(self._refresh_translate)

        self.setContextMenu(self._menu)

    _MEET_TITLE = "Meet 議事録ボット"

    def set_translate_toggle(self, getter, setter) -> None:
        """
        「翻訳して入力（常時）」の読み書き先を渡す（項目が出るようになる）。

        Args:
            getter: いまの値を返す関数（メニューを開くたびに読み直す。設定画面での切り替えを拾うため）
            setter: 新しい値を保存する関数
        """
        self._translate_get = getter
        self._translate_set = setter
        self._translate_action.setVisible(True)
        self._refresh_translate()

    def _refresh_translate(self) -> None:
        if self._translate_get is not None:
            self._translate_action.setChecked(bool(self._translate_get()))

    def _on_translate_triggered(self, checked: bool) -> None:
        if self._translate_set is not None:
            self._translate_set(checked)

    def set_meetbot(self, meetbot) -> None:
        """
        議事録ボットを渡す（サブメニューが出るようになる）。

        Args:
            meetbot: MeetBotService
        """
        self._meetbot = meetbot
        self._meet_action.setVisible(True)
        # メニューを開かなくても「いま会議に入っている」が見えるよう、見出しに状態を出す（Mac と同じ）
        meetbot.state_changed.connect(lambda state, _text: self._update_meet_title(state))

    def _update_meet_title(self, state: str) -> None:
        from ..meetbot import rules

        suffix = {
            rules.STATE_RECORDING: "（参加中）",
            rules.STATE_WAITING_APPROVAL: "（承認待ち）",
            rules.STATE_LAUNCHING: "（準備中）",
            rules.STATE_JOINING: "（準備中）",
        }.get(state, "")
        self._meet_menu.setTitle(self._MEET_TITLE + suffix)

    def _rebuild_meet_menu(self) -> None:
        """サブメニューを開くたびに作り直す（並び・文言は Mac と同じ。文字起こしの方法は Windows は字幕だけなので出さない）。"""
        from ..meetbot import chrome, rules, transcript

        menu = self._meet_menu
        menu.clear()
        bot = self._meetbot
        if chrome.find_chrome() is None:
            # Chrome が無くても項目自体は残し、何が足りないかを見せる（消すと機能の存在に気づけない）
            menu.addAction("Google Chrome が必要です").setEnabled(False)
        else:
            active = bot.is_active()
            menu.addAction(rules.state_text(bot.state)).setEnabled(False)
            menu.addAction("文字起こし: Meet の字幕").setEnabled(False)
            menu.addSeparator()
            if active:
                menu.addAction("会議から退出").triggered.connect(bot.stop)
            else:
                # Windows は URL を聞く小窓を出さず、URL 欄のある議事録・会議ページを開く
                menu.addAction("会議に参加…").triggered.connect(self.open_meeting.emit)
            menu.addSeparator()
            login = menu.addAction("ボット用ブラウザで Google にログイン…")
            login.setEnabled(not active)
            login.triggered.connect(bot.open_login)

        menu.addSeparator()
        files = transcript.list_transcripts()
        latest = menu.addAction("最新の議事録を開く")
        latest.setEnabled(bool(files))
        if files:
            latest.triggered.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(files[0].path))))
        menu.addAction("議事録フォルダを開く").triggered.connect(self._open_transcripts)
        menu.addAction("議事録・会議の設定…").triggered.connect(self.open_meeting.emit)

    @staticmethod
    def _open_transcripts() -> None:
        from ..meetbot import transcript

        directory = transcript.default_directory()
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(directory)))

    def set_api_usage(self, api_usage, mode=None) -> None:
        """
        今日の API 代を数える元を渡す（渡されるまで料金の行は出さない）。

        Args:
            api_usage: API の利用料金の記録（ApiUsageStore）
            mode: いまの数え方（"list" / "paid"）を返す関数。ダッシュボードで選んだ数え方に
                合わせるために、開くたびに呼ぶ。無ければ定価で数える
        """
        self._api_usage = api_usage
        self._cost_mode = mode
        self._refresh_cost()

    def _refresh_cost(self) -> None:
        """今日の API 代の行を数え直す（ドルだけ。単価が分からない分があればその旨を足す）。"""
        if self._api_usage is None:
            self._cost_action.setVisible(False)
            return
        mode = self._cost_mode() if self._cost_mode is not None else api_pricing.MODE_LIST
        summary = self._api_usage.today_summary(mode if mode in (api_pricing.MODE_LIST, api_pricing.MODE_PAID) else api_pricing.MODE_LIST)
        title = f"今日の API 代 {api_pricing.format_usd(summary['usd'])}"
        if summary.get("has_unpriced"):
            title += "＋単価未確認分"
        self._cost_action.setText(title)
        self._cost_action.setVisible(True)

    def show_update_available(self, version: str) -> None:
        """
        新バージョンの通知を表示し、メニューにインストール項目を出す。

        Args:
            version: 検知した新バージョン文字列
        """
        self._update_action.setText(f"v{version.lstrip('vV')} に更新して再起動")
        self._update_action.setVisible(True)
        self._update_separator.setVisible(True)
        self.showMessage(
            "voicekey アップデート",
            f"新しいバージョン {version} があります。\nトレイメニューからインストールできます。",
            QSystemTrayIcon.MessageIcon.Information,
            10000,
        )

    def show_update_failed(self, message: str) -> None:
        """アップデート失敗をトレイ通知で知らせる。"""
        self.showMessage(
            "voicekey アップデート失敗",
            f"{message}\n次回のチェックで再試行されます。",
            QSystemTrayIcon.MessageIcon.Warning,
            10000,
        )

    def _send_feedback(self) -> None:
        """フィードバック入力ダイアログを開く（本文を自社サーバーへ送信する）。

        ログイン済みならアカウントに紐づき、未ログインでも匿名で送れる。
        遅延 import で起動コスト・循環 import を避ける。
        """
        from .feedback_dialog import FeedbackDialog

        dialog = FeedbackDialog()
        dialog.exec()

    def set_status(self, status: Union[str, AppState]) -> None:
        """
        アプリケーション状態に応じてアイコンを更新する。
        
        Args:
            status: 現在のアプリケーション状態
        """
        # 文字列の場合はAppStateに変換
        if isinstance(status, str):
            status = AppState(status)
        
        tooltip = self._get_tooltip(status)

        self._set_icon(status)
        self.setToolTip(tooltip)
        self._state_action.setText(self._STATE_LABELS.get(status, "待機中"))

    def _get_tooltip(self, status: AppState) -> str:
        """
        状態に応じたツールチップテキストを取得する。
        
        Args:
            status: 現在のアプリケーション状態
            
        Returns:
            ツールチップ文字列
        """
        tooltips = {
            AppState.IDLE: "voicekey - 待機中",
            AppState.RECORDING: "voicekey - 録音中",
            AppState.RECORDING_AUTO_ENTER: "voicekey - 録音中（自動 Enter）",
            AppState.TRANSCRIBING: "voicekey - 変換中",
        }
        return tooltips.get(status, "voicekey")

    def _set_icon(self, status: AppState) -> None:
        """
        状態に応じたアイコン（Mac のメニューバーと同じキーの形）を設定する。

        タスクバーの明暗は毎回読み直す。使っている途中で Windows の配色を変えられても、
        次に状態が変わったときに枠の色が合うようにするため。

        Args:
            status: アプリの状態
        """
        self.setIcon(brand.status_icon(status, brand.taskbar_is_light()))
