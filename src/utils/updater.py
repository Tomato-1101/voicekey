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
import shutil
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

# 更新情報の取得元（配布ページと同じ Vercel サイト。GitHub はテスターから見えない構成）
VERSION_URL = "https://voicekey.vercel.app/windows/version.json"

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
    # 先頭の "v" とプレリリース/ビルドメタ（"-beta"、"+build" 等）を許容してから数値化する。
    # 数値でなければ int() が ValueError を投げ、呼び出し側（_check）が握って更新をスキップする
    cleaned = version.strip().lstrip("vV").split("-")[0].split("+")[0]
    return tuple(int(p) for p in cleaned.split("."))


class Updater(QObject):
    """
    バージョンチェックとインストーラ起動を担う自動アップデータ。

    Signals:
        update_available: 新バージョン検知（引数: バージョン文字列）
        up_to_date: 最新版だった（手動チェック時に「最新です」と表示するため）
        update_failed: ダウンロード/検証の失敗（引数: エラーメッセージ）
        quit_requested: インストーラ起動後のアプリ終了要求
    """

    update_available = Signal(str)
    up_to_date = Signal()
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

    def check_now(self, manual: bool = False) -> None:
        """バージョンチェックをバックグラウンドで実行する。

        Args:
            manual: 設定画面の「アップデートを確認」から手動実行された場合 True。
                手動時はネットワーク失敗も update_failed で通知する（定期チェック時はログのみ）。
        """
        threading.Thread(
            target=self._check, args=(manual,), daemon=True, name="UpdateCheck"
        ).start()

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

    def _check(self, manual: bool = False) -> None:
        """version.json を取得して現行バージョンと比較する。

        Args:
            manual: 手動チェック（設定画面）からの実行か。失敗通知の有無を分ける。
        """
        try:
            with urllib.request.urlopen(VERSION_URL, timeout=15) as res:
                # version.json は Windows PowerShell が BOM 付き UTF-8 で書き出すことがある。
                # utf-8-sig で先頭 BOM を許容する（BOM が無くても安全。素朴な utf-8 だと
                # 先頭の ﻿ で json.loads が落ち、アップデートが静かに効かなくなる）
                info = json.loads(res.read().decode("utf-8-sig"))
            latest = str(info.get("version", ""))
            if parse_version(latest) > parse_version(APP_VERSION):
                logger.info(f"新バージョン {latest} を検知しました（現行 {APP_VERSION}）")
                self._info = info
                self.update_available.emit(latest)
            else:
                logger.info(f"アップデートなし（最新 {latest} / 現行 {APP_VERSION}）")
                self.up_to_date.emit()
        except Exception as e:
            # ネットワーク断やフィード未公開は正常系に近いので、定期チェックでは警告ログのみ。
            # ただし手動チェックは「押したのに無反応」を避けるため失敗を UI に通知する。
            logger.warning(f"アップデート確認に失敗: {e}")
            if manual:
                self.update_failed.emit("更新の確認に失敗しました。ネットワーク接続をご確認ください")

    def _install(self) -> None:
        """インストーラを %TEMP% にダウンロードし、検証してサイレント起動する。"""
        info = self._info or {}
        try:
            url = info.get("url")
            version = info.get("version", "")
            expected = str(info.get("sha256", "")).lower()
            if not url:
                raise ValueError("version.json に url がありません")
            # sha256 が無いと改ざん検証ができないので、その場合はインストールしない
            if not expected:
                raise ValueError("version.json に sha256 がないため検証できません")
            path = os.path.join(tempfile.gettempdir(), f"voicekey-setup-{version}.exe")
            logger.info(f"インストーラをダウンロード中: {url}")
            # urlretrieve はタイムアウトを持てず、回線が stall するとスレッドが永久ブロックし
            # 「インストール中」から戻れなくなる。timeout 付き urlopen + ストリームコピーで
            # 確実に打ち切れるようにする
            with urllib.request.urlopen(url, timeout=30) as res, open(path, "wb") as f:
                shutil.copyfileobj(res, f)

            # 改ざん・破損対策: version.json の SHA256 と一致しなければ実行しない
            digest = hashlib.sha256()
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(1024 * 1024), b""):
                    digest.update(chunk)
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
