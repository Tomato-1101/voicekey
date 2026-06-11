"""
自動アップデートモジュール（Windows 配布ビルド用）

voicekey-releases リポジトリの version.json を定期チェックし、新しいバージョンが
あればトレイ通知とメニュー項目で知らせる。インストールはユーザーがメニューを
選んだときに、インストーラをダウンロード（SHA256 検証付き）→ サイレント実行
→ アプリ終了、の順で行う（Inno Setup が旧プロセスを閉じて上書きし、新版を再起動する）。

設計方針:
- 配布（DIST）ビルドのみ動作する（開発環境では start() が何もしない）
- モーダルダイアログは使わない（音声入力アプリがフォーカスを奪うと入力事故になる）
- ネットワーク処理はバックグラウンドスレッドで行い、UI へは Qt シグナルで通知する
"""

import hashlib
import json
import os
import subprocess
import tempfile
import threading
import urllib.request
from typing import Optional

from PySide6.QtCore import QObject, QTimer, Signal

from ..config.constants import APP_VERSION
from . import secrets
from .logger import get_logger

logger = get_logger(__name__)

# 更新情報の取得元（voicekey-releases は配布専用の公開リポジトリ）
VERSION_URL = (
    "https://raw.githubusercontent.com/Tomato-1101/voicekey-releases/main/windows/version.json"
)

# 起動直後はアプリ初期化と被らないよう 60 秒待ってから初回チェックする
FIRST_CHECK_DELAY_MS = 60_000
# 以後は 24 時間ごと（Mac 版 Sparkle の SUScheduledCheckInterval と同じ周期）
CHECK_INTERVAL_MS = 24 * 60 * 60 * 1000


def parse_version(version: str) -> tuple:
    """
    "1.2.3" 形式のバージョン文字列を比較可能なタプルに変換する。

    Args:
        version: セマンティックバージョン文字列

    Returns:
        整数タプル（例: (1, 2, 3)）

    Raises:
        ValueError: 数値として解釈できない場合
    """
    return tuple(int(p) for p in version.strip().split("."))


class Updater(QObject):
    """
    バージョンチェックとインストーラ起動を担う自動アップデータ。

    Signals:
        update_available: 新バージョン検知（引数: バージョン文字列）
        update_failed: ダウンロード/検証の失敗（引数: エラーメッセージ）
        quit_requested: インストーラ起動後のアプリ終了要求
    """

    update_available = Signal(str)
    update_failed = Signal(str)
    quit_requested = Signal()

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._info: Optional[dict] = None
        self._installing = False
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.check_now)

    def start(self) -> None:
        """定期チェックを開始する（DIST ビルド以外は何もしない）。"""
        if not secrets.is_dist_build():
            logger.info("開発ビルドのため自動アップデートは無効です")
            return
        QTimer.singleShot(FIRST_CHECK_DELAY_MS, self.check_now)
        self._timer.start(CHECK_INTERVAL_MS)
        logger.info("自動アップデートの定期チェックを開始しました")

    def check_now(self) -> None:
        """バージョンチェックをバックグラウンドで実行する。"""
        threading.Thread(target=self._check, daemon=True, name="UpdateCheck").start()

    def download_and_install(self) -> None:
        """
        新バージョンのインストーラをダウンロードして起動する。

        update_available 発火後にユーザーがメニューから選んだときに呼ぶ。
        二重実行は無視する。
        """
        if self._installing or self._info is None:
            return
        self._installing = True
        threading.Thread(target=self._install, daemon=True, name="UpdateInstall").start()

    # ----- 内部処理（バックグラウンドスレッド） -----

    def _check(self) -> None:
        """version.json を取得して現行バージョンと比較する。"""
        try:
            with urllib.request.urlopen(VERSION_URL, timeout=15) as res:
                info = json.loads(res.read().decode("utf-8"))
            latest = str(info.get("version", ""))
            if parse_version(latest) > parse_version(APP_VERSION):
                logger.info(f"新バージョン {latest} を検知しました（現行 {APP_VERSION}）")
                self._info = info
                self.update_available.emit(latest)
            else:
                logger.info(f"アップデートなし（最新 {latest} / 現行 {APP_VERSION}）")
        except Exception as e:
            # ネットワーク断やフィード未公開は正常系に近いので警告ログのみ
            logger.warning(f"アップデート確認に失敗: {e}")

    def _install(self) -> None:
        """インストーラを %TEMP% にダウンロードし、検証してサイレント起動する。"""
        info = self._info or {}
        try:
            url = info["url"]
            version = info["version"]
            path = os.path.join(tempfile.gettempdir(), f"voicekey-setup-{version}.exe")
            logger.info(f"インストーラをダウンロード中: {url}")
            urllib.request.urlretrieve(url, path)

            # 改ざん・破損対策: version.json の SHA256 と一致しなければ実行しない
            digest = hashlib.sha256()
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(1024 * 1024), b""):
                    digest.update(chunk)
            expected = str(info.get("sha256", "")).lower()
            if digest.hexdigest().lower() != expected:
                raise ValueError("インストーラの SHA256 が version.json と一致しません")

            # サイレント更新: Inno Setup が実行中の旧プロセスを閉じて上書きし、
            # [Run] セクションで新版を自動再起動する
            logger.info(f"インストーラを起動します: {path}")
            subprocess.Popen(
                [path, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/CLOSEAPPLICATIONS", "/NORESTART"],
                close_fds=True,
            )
            # 終了はメインスレッド側（app._quit_app）に委ねる（クリーンアップを通すため）
            self.quit_requested.emit()
        except Exception as e:
            logger.error(f"アップデートのインストールに失敗: {e}")
            self._installing = False
            self.update_failed.emit(str(e))
