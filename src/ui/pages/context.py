"""
設定ページが共有する文脈

どのページも、設定の読み書きとアプリ本体への通知をここ経由で行う。
Mac 版と同じく「変えたらすぐ反映」にするため、保存ボタンは無い。ページは変えた項目だけを
commit に渡し、ここが settings.yaml へ書いてアプリ本体へ知らせる。
"""

from typing import Any, Dict, Optional

from PySide6.QtCore import QObject, QTimer, Signal


def _merge(base: Dict[str, Any], changes: Dict[str, Any]) -> Dict[str, Any]:
    """入れ子の辞書を、変えたキーだけ上書きして重ねる（base を書き換える）。"""
    for key, value in changes.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value
    return base


class PageContext(QObject):
    """
    設定ページへ渡す、アプリ本体との窓口。

    Attributes:
        config_manager: 設定の正本（アプリ本体と同じインスタンス）
        platform: プラットフォーム依存処理のアダプタ（自動起動など）
        history: 音声入力の履歴（無ければ None）
        stats: 使用実績（無ければ None）
        updater: 自動アップデータ（無ければ None）
        history_sync: Mac との履歴同期（無ければ None）
        api_usage: API の利用料金の記録（無ければ None）
        meetbot: Google Meet 議事録ボット（無ければ None。議事録ページは参加・要約を押せなくなる）
    """

    # 設定を保存した（アプリ本体が受けて、ホットキーなどをすぐ適用する）
    settings_saved = Signal()
    # 「セットアップガイドをもう一度見る」が押された
    open_onboarding_requested = Signal()

    # 文字を打っている間は書き込まず、手が止まってからまとめて書く（1 打鍵ごとの書き込みを避ける）
    _TYPING_DELAY_MS = 400

    def __init__(
        self,
        config_manager,
        platform=None,
        history=None,
        stats=None,
        updater=None,
        history_sync=None,
        api_usage=None,
        meetbot=None,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self.config_manager = config_manager
        self.platform = platform
        self.history = history
        self.stats = stats
        self.updater = updater
        self.history_sync = history_sync
        self.api_usage = api_usage
        self.meetbot = meetbot

        self._pending: Dict[str, Any] = {}
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(self._TYPING_DELAY_MS)
        self._timer.timeout.connect(self.flush)

    def get(self, key: str, default: Any = None) -> Any:
        """
        設定値を読む（まだ書いていない入力途中の値があればそちらを返す）。

        Args:
            key: 設定キー（最上位）
            default: 無いときの値
        """
        if key in self._pending and not isinstance(self._pending[key], dict):
            return self._pending[key]
        return self.config_manager.get(key, default)

    def commit(self, changes: Dict[str, Any]) -> bool:
        """
        変えた項目をすぐ保存して、アプリ本体へ知らせる（スイッチ・選択・ボタン用）。

        Args:
            changes: 変えたキーだけの辞書（入れ子は変えた枝だけでよい）

        Returns:
            保存できたら True
        """
        _merge(self._pending, changes)
        return self.flush()

    def commit_later(self, changes: Dict[str, Any]) -> None:
        """
        変えた項目を、手が止まってから保存する（入力欄用）。

        Args:
            changes: 変えたキーだけの辞書
        """
        _merge(self._pending, changes)
        self._timer.start()

    def flush(self) -> bool:
        """
        保存待ちの変更をいま書く（ウィンドウを閉じるとき・ページを離れるときにも呼ぶ）。

        Returns:
            書くものが無い・保存できたら True
        """
        self._timer.stop()
        if not self._pending:
            return True
        changes, self._pending = self._pending, {}
        saved = bool(self.config_manager.save(changes))
        if saved:
            self.settings_saved.emit()
        return saved
