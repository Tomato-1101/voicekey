"""
自動アップデートモジュール（Windows 配布ビルド用）

本リポジトリの GitHub Releases の Latest（releases/latest API・認証なし）を定期チェックし、
Windows 用インストーラが付いた新しいバージョンがあればトレイ通知とメニュー項目で知らせる。
インストールはユーザーがメニューを選んだときに、インストーラをダウンロード → サイズ照合 →
固定公開鍵による Ed25519 署名検証 ＋ SHA256（GitHub が資産ごとに出す digest）照合 →
サイレント実行 → アプリ終了、の順で行う（Inno Setup が旧プロセスを閉じて上書きし、
新版を再起動する）。署名検証により、リリースが改竄・MITM されても秘密鍵を持たない第三者は
不正な更新を実行できない（Mac 版 Sparkle EdDSA と同方針）。

リリースは Mac 版が先に（appcast 付きで）作り、Windows 用の資産は後から足す運用。
そのため Windows 用の資産が無い Latest（Mac だけのリリース）は「更新なし」として黙って終える。

設計方針:
- PyInstaller で固めた実行ファイルでのみ動作する（ソースからの実行では start() が何もしない）
- モーダルダイアログは使わない（音声入力アプリがフォーカスを奪うと入力事故になる）
- ネットワーク処理はバックグラウンドスレッドで行い、UI へは Qt シグナルで通知する
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.request
from typing import Optional

from PySide6.QtCore import QObject, QTimer, Signal

from ..config.constants import APP_VERSION, UPDATE_PUBLIC_KEY_ED25519
from .logger import get_logger
from .update_signing import verify_ed25519

logger = get_logger(__name__)

# 更新情報の取得元（本リポジトリの GitHub Releases の Latest。draft / pre-release は返らない）
GITHUB_REPO = "Tomato-1101/voicekey"
LATEST_RELEASE_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"

# Windows 用インストーラの資産名の規則（ビルドスクリプト・CI ワークフローと必ず揃える）。
# Mac の資産（voicekey-<版>.zip / appcast.xml）と同じリリースに並ぶので windows を名前に入れる
WINDOWS_ASSET_NAME = "voicekey-{version}-windows-setup.exe"
# Ed25519 署名の資産名（中身は署名の base64 文字列 1 行）。秘密鍵は CI に置かないため、
# Mac 側で scripts/build/sign_update.py で作って後から同じリリースへ足す
SIGNATURE_SUFFIX = ".sig"

# 起動直後はアプリ初期化と被らないよう 60 秒待ってから初回チェックする
FIRST_CHECK_DELAY_MS = 60_000
# 以後は 24 時間ごと（Mac 版 Sparkle の SUScheduledCheckInterval と同じ周期）
CHECK_INTERVAL_MS = 24 * 60 * 60 * 1000


def windows_asset_name(version: str) -> str:
    """バージョン（"1.2.3"）から Windows 用インストーラの資産名を返す。"""
    return WINDOWS_ASSET_NAME.format(version=version)


def _open(url: str, timeout: float):
    """GitHub へのリクエストを開く（User-Agent が無いと GitHub API は 403 を返すため付ける）。"""
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "voicekey-updater", "Accept": "application/vnd.github+json"},
    )
    return urllib.request.urlopen(req, timeout=timeout)


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
        """定期チェックを開始する（PyInstaller で固めた実行ファイル以外は何もしない）。"""
        if not getattr(sys, "frozen", False):
            logger.info("ソースからの実行のため自動アップデートは無効です")
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
        """GitHub Releases の Latest を取得して現行バージョンと比較する。

        Args:
            manual: 手動チェック（設定画面）からの実行か。失敗通知の有無を分ける。
        """
        try:
            with _open(LATEST_RELEASE_URL, timeout=15) as res:
                release = json.loads(res.read().decode("utf-8-sig"))
            tag = str(release.get("tag_name", ""))
            latest_tuple = parse_version(tag)
            latest = ".".join(str(p) for p in latest_tuple)
            if latest_tuple <= parse_version(APP_VERSION):
                logger.info(f"アップデートなし（最新 {latest} / 現行 {APP_VERSION}）")
                self.up_to_date.emit()
                return
            assets = {
                str(a.get("name", "")): a
                for a in release.get("assets", []) or []
                if isinstance(a, dict)
            }
            name = windows_asset_name(latest)
            asset = assets.get(name)
            sig_asset = assets.get(name + SIGNATURE_SUFFIX)
            # Windows 用の資産が無い Latest（Mac だけのリリース）は更新扱いにしない。
            # 署名が後から足される運用なので、署名がまだ無い間も同じく「更新なし」で待つ
            if asset is None or (UPDATE_PUBLIC_KEY_ED25519 and sig_asset is None):
                missing = name if asset is None else name + SIGNATURE_SUFFIX
                logger.info(f"Latest {latest} に Windows 用の資産 {missing} が無いため更新なしとします")
                self.up_to_date.emit()
                return
            logger.info(f"新バージョン {latest} を検知しました（現行 {APP_VERSION}）")
            self._info = {
                "version": latest,
                "url": asset.get("browser_download_url"),
                "size": asset.get("size"),
                "digest": asset.get("digest"),
                "signature_url": sig_asset.get("browser_download_url") if sig_asset else None,
            }
            self.update_available.emit(latest)
        except Exception as e:
            # ネットワーク断やリリース未公開は正常系に近いので、定期チェックでは警告ログのみ。
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
            if not url:
                raise ValueError("リリースの資産にダウンロード URL がありません")
            path = os.path.join(tempfile.gettempdir(), f"voicekey-setup-{version}.exe")
            logger.info(f"インストーラをダウンロード中: {url}")
            # urlretrieve はタイムアウトを持てず、回線が stall するとスレッドが永久ブロックし
            # 「インストール中」から戻れなくなる。timeout 付き urlopen + ストリームコピーで
            # 確実に打ち切れるようにする
            with _open(url, timeout=30) as res, open(path, "wb") as f:
                shutil.copyfileobj(res, f)

            # 配布物の検証はメモリに 1 回読み込んだバイト列に対して行う（サイズ・SHA256・署名で共用）。
            # Ed25519 検証は全メッセージを必要とするため（標準 Ed25519 はストリーミング不可）。
            with open(path, "rb") as f:
                data = f.read()

            # 0) サイズ照合（途中で切れたダウンロードを最初に弾く）
            size = info.get("size")
            if isinstance(size, int) and len(data) != size:
                raise ValueError(
                    f"インストーラのサイズがリリースの記載と一致しません（{len(data)} / {size} バイト）"
                )

            # 1) 固定公開鍵による Ed25519 署名検証 = 改竄・MITM 対策の主防御。
            #    リリース（exe と digest）を改竄されても、秘密鍵を持たない第三者は
            #    正規署名を作れない。SHA256 は GitHub を信頼できる場合の破損検出にすぎない。
            if UPDATE_PUBLIC_KEY_ED25519:
                sig_url = info.get("signature_url")
                if not sig_url:
                    raise ValueError("リリースに署名ファイルがないため検証できません")
                with _open(sig_url, timeout=30) as res:
                    signature = res.read().decode("utf-8-sig").strip()
                if not verify_ed25519(UPDATE_PUBLIC_KEY_ED25519, signature, data):
                    raise ValueError("インストーラの署名が検証できません（改竄の可能性）")
            else:
                # 公開鍵が未設定のビルド（移行期の保険）。署名検証ができないため警告だけ残す。
                logger.warning("更新の公開鍵が未設定のため署名検証をスキップします（SHA256 のみ）")

            # 2) SHA256（破損検出の補助）。GitHub が資産ごとに出す digest（"sha256:<hex>"）と照合する。
            #    digest が無い古いリリースでは署名検証だけに任せる（公開鍵も無いならインストールしない）
            digest = str(info.get("digest") or "")
            if digest.startswith("sha256:"):
                if hashlib.sha256(data).hexdigest().lower() != digest[len("sha256:"):].lower():
                    raise ValueError("インストーラの SHA256 がリリースの digest と一致しません")
            elif not UPDATE_PUBLIC_KEY_ED25519:
                raise ValueError("署名も SHA256 も無いため検証できません")

            # 「検証したバイト列」と「実行するファイル」の同一性を保証する（TOCTOU 対策）。
            # ダウンロード先は予測可能な名前なので、検証後・起動前に同権限のプロセスが
            # 中身を差し替えられる。検証済みの data を排他生成のランダム名へ書き出し、
            # そちらを起動する（差し替えの隙を無くす）。
            fd, run_path = tempfile.mkstemp(prefix="voicekey-setup-", suffix=".exe")
            with os.fdopen(fd, "wb") as f:
                f.write(data)

            # サイレント更新: Inno Setup が実行中の旧プロセスを閉じて上書きし、
            # [Run] セクションで新版を自動再起動する
            logger.info(f"インストーラを起動します: {run_path}")
            subprocess.Popen(
                [run_path, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/CLOSEAPPLICATIONS", "/NORESTART"],
                close_fds=True,
            )
            # 終了はメインスレッド側（app._quit_app）に委ねる（クリーンアップを通すため）
            self.quit_requested.emit()
        except Exception as e:
            logger.error(f"アップデートのインストールに失敗: {e}")
            self._installing = False
            self.update_failed.emit(str(e))
