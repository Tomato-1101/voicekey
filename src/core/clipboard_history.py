"""
クリップボード履歴モジュール（Mac 版 ClipboardHistoryStore.swift の移植）

Windows 標準のクリップボード履歴（Win+V）とは別に、コピーしたテキストを新しい順に保持し、
サイドノッチの履歴パネルから再コピーできるようにする。
変更の検出は Qt の QClipboard.dataChanged（OS の変更通知）で行い、Mac のようなポーリングはしない。
保存先は音声入力の履歴（history.json）と同じディレクトリの clipboard_history.json。

記録しないもの（Mac の ignoredTypes に当たるもの）:
- 「監視・履歴に残さないで」の印が付いたコピー（パスワード管理アプリ・voicekey 自身の貼り付け）。
  Windows では ExcludeClipboardContentFromMonitorProcessing / CanIncludeInClipboardHistory=0 /
  Clipboard Viewer Ignore の登録形式が印になる（Microsoft の Clipboard Formats の文書に載っている取り決め）。
- 空白だけのテキスト・テキスト以外（画像・ファイル）。
"""

import json
import re
import struct
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from ..utils import corrupt_file
from ..utils.logger import get_logger
from .history import _default_path as _history_default_path

logger = get_logger(__name__)

CLIPBOARD_HISTORY_FILE_NAME = "clipboard_history.json"
# 既定の保持件数（Mac の ClipboardHistoryStore の既定と同じ）
DEFAULT_LIMIT = 200

# Qt が Windows の登録形式を見せるときの MIME（application/x-qt-windows-mime;value="形式名"）
_QT_WINDOWS_MIME = re.compile(r'^application/x-qt-windows-mime;value="(.*)"$', re.IGNORECASE)
# 付いていたら記録しない形式（小文字で比較する）
_EXCLUDE_NAMES = {
    "excludeclipboardcontentfrommonitorprocessing",
    "clipboard viewer ignore",
}
# 値が 0 なら記録しない形式（DWORD。1 なら残してよいという意味）
_CAN_INCLUDE_NAME = "canincludeinclipboardhistory"

# 印の形式を Qt の QMimeData に付けるときの MIME（サイドノッチからのコピーで使う）
EXCLUDE_MIME = 'application/x-qt-windows-mime;value="ExcludeClipboardContentFromMonitorProcessing"'
CAN_INCLUDE_MIME = 'application/x-qt-windows-mime;value="CanIncludeInClipboardHistory"'


def _format_name(fmt: str) -> str:
    """Qt の MIME 表記なら中の形式名を、そうでなければそのままを返す。"""
    match = _QT_WINDOWS_MIME.match(fmt or "")
    return match.group(1) if match else (fmt or "")


def is_excluded(
    formats: Iterable[str],
    read_data: Optional[Callable[[str], Optional[bytes]]] = None,
) -> bool:
    """このコピーを記録しないかを決める（純関数・テスト対象）。

    Args:
        formats: クリップボードにある形式（Qt の MIME 表記 or 登録形式名）
        read_data: 形式（formats の要素そのまま）の中身を読む関数。
            CanIncludeInClipboardHistory の値を見るのに使う

    Returns:
        記録しないなら True
    """
    for fmt in formats:
        name = _format_name(fmt).lower()
        if name in _EXCLUDE_NAMES:
            return True
        if name == _CAN_INCLUDE_NAME:
            data = None
            if read_data is not None:
                try:
                    data = read_data(fmt)
                except Exception:
                    data = None
            # 値が読めないときは記録しない側に倒す（パスワードを残す方が害が大きい）
            if not data or len(data) < 4 or struct.unpack("<I", bytes(data[:4]))[0] == 0:
                return True
    return False


class ClipboardHistoryStore:
    """
    クリップボード履歴のストア（スレッドセーフ）。

    記録は UI スレッド（dataChanged）から、items() / clear() も UI スレッドから呼ばれるが、
    設定の再読み込みが別スレッドから limit を変えるのでロックで保護する。

    Attributes:
        _items: 履歴エントリのリスト（新しい順）。各要素は
            {"id": uuid4 hex, "text": str, "date": ISO 8601 str, "app_id": str|None, "app_name": str|None}
    """

    def __init__(
        self,
        file_path: Optional[Path] = None,
        limit: int = DEFAULT_LIMIT,
        app_provider: Optional[Callable[[], Tuple[Optional[str], Optional[str]]]] = None,
    ) -> None:
        """
        ストアを初期化し、保存済みの履歴を読み込む（記録は start() まで始めない）。

        Args:
            file_path: 保存先（テスト用。省略時は history.json と同じディレクトリ）
            limit: 保持する最大件数
            app_provider: 前面アプリの (実行ファイル名, 表示名) を返す関数（コピー元の近似）
        """
        self._path = Path(file_path) if file_path else _history_default_path().parent / CLIPBOARD_HISTORY_FILE_NAME
        self._lock = threading.Lock()
        self._limit = max(0, int(limit))
        self._app_provider = app_provider
        self._clipboard = None
        # 壊れたファイルを退避できなかった（＝元ファイルが残っている）ので保存しない
        self._save_blocked = False
        self._items: List[Dict[str, object]] = self._load()

    # ---- 件数 ----
    @property
    def limit(self) -> int:
        return self._limit

    @limit.setter
    def limit(self, value: int) -> None:
        """件数を変える。下げたら超過分を古い方から捨てて即保存する（Mac と同じ）。"""
        value = max(0, int(value))
        with self._lock:
            if value == self._limit:
                return
            self._limit = value
            if len(self._items) > value:
                del self._items[value:]
                self._save()

    # ---- 記録の開始・停止 ----
    @property
    def is_running(self) -> bool:
        """記録中か（サイドノッチの空状態の文言が見る）。"""
        return self._clipboard is not None

    def start(self, clipboard=None) -> None:
        """記録を始める（UI スレッドから呼ぶ）。dataChanged は接続後の変更だけ届くので、今の中身は記録しない。

        Args:
            clipboard: 監視する QClipboard（テスト用。省略時は QGuiApplication.clipboard()）
        """
        if self._clipboard is not None:
            return
        if clipboard is None:
            from PySide6.QtGui import QGuiApplication

            clipboard = QGuiApplication.clipboard()
        if clipboard is None:
            return
        clipboard.dataChanged.connect(self._on_data_changed)
        self._clipboard = clipboard

    def stop(self) -> None:
        """記録を止める（既存の履歴は消さない）。"""
        clipboard, self._clipboard = self._clipboard, None
        if clipboard is None:
            return
        try:
            clipboard.dataChanged.disconnect(self._on_data_changed)
        except (RuntimeError, TypeError):
            pass

    def _on_data_changed(self) -> None:
        """クリップボードが変わったら中身を見て記録する。"""
        clipboard = self._clipboard
        if clipboard is None:
            return
        try:
            mime = clipboard.mimeData()
            if mime is None or not mime.hasText():
                return
            formats = list(mime.formats())
            text = mime.text()
            app_id, app_name = (None, None)
            # 印付き（パスワード等）なら前面アプリも調べない（無駄な Win32 呼び出しを省く）
            if is_excluded(formats, lambda fmt: bytes(mime.data(fmt))):
                return
            if self._app_provider is not None:
                try:
                    app_id, app_name = self._app_provider()
                except Exception:
                    app_id, app_name = (None, None)
            self.record(text, formats=(), app_id=app_id, app_name=app_name)
        except Exception as e:
            # 本文はログに出さない（エラーの種類だけ）
            logger.warning(f"クリップボード履歴の記録に失敗: {type(e).__name__}")

    # ---- 記録（テストの入口） ----
    def record(
        self,
        text: str,
        formats: Iterable[str] = (),
        read_data: Optional[Callable[[str], Optional[bytes]]] = None,
        app_id: Optional[str] = None,
        app_name: Optional[str] = None,
    ) -> bool:
        """
        コピーされたテキスト 1 件を記録する（実際の OS クリップボードには触れない）。

        Args:
            text: コピーされたテキスト
            formats: クリップボードにあった形式（印の判定に使う）
            read_data: 形式の中身を読む関数（CanIncludeInClipboardHistory の値を見る）
            app_id: コピー元アプリの識別子（実行ファイル名）
            app_name: コピー元アプリの表示名

        Returns:
            記録したら True
        """
        if not text or not text.strip():
            return False
        if is_excluded(formats, read_data):
            return False
        with self._lock:
            # 先頭と同じなら何もしない（貼り付け後に元の中身を書き戻したときに先頭が繰り上がらないように）
            if self._items and self._items[0].get("text") == text:
                return False
            entry = {
                "id": uuid.uuid4().hex,
                "text": text,
                "date": datetime.now().astimezone().isoformat(timespec="seconds"),
                "app_id": app_id,
                "app_name": app_name,
            }
            # 途中にある同じテキストは消して先頭へ入れ直す（同じ内容を何行も並べない）
            self._items = [e for e in self._items if e.get("text") != text]
            self._items.insert(0, entry)
            del self._items[self._limit:]
            self._save()
        return True

    def items(self) -> List[Dict[str, object]]:
        """履歴のコピーを取得する（新しい順）。"""
        with self._lock:
            return [dict(entry) for entry in self._items]

    def clear(self) -> None:
        """全件消去して保存する。"""
        with self._lock:
            self._items = []
            self._save()

    # ---- 保存 ----
    def _load(self) -> List[Dict[str, object]]:
        """保存済みの履歴を読み込む。壊れていれば退避して空で開始する（history.py と同じ方針）。"""
        try:
            if self._path.exists():
                data = json.loads(self._path.read_text(encoding="utf-8"))
                if not isinstance(data, list):
                    raise ValueError("クリップボード履歴ファイルの中身が一覧の形になっていません")
                return [
                    {
                        "id": str(e.get("id") or uuid.uuid4().hex),
                        "text": str(e.get("text", "")),
                        "date": str(e.get("date", "")),
                        "app_id": e.get("app_id") or None,
                        "app_name": e.get("app_name") or None,
                    }
                    for e in data
                    if isinstance(e, dict) and str(e.get("text", "")).strip()
                ][: self._limit]
        except Exception as e:
            logger.warning(f"クリップボード履歴の読み込みに失敗（退避して空で開始します）: {e}")
            # 空のまま保存すると元の履歴を上書きして消すので退避する。退避できなければ保存しない
            if corrupt_file.quarantine(self._path, label="クリップボード履歴ファイル") is None:
                self._save_blocked = True
        return []

    def _save(self) -> None:
        """履歴をファイルに保存する（_lock 保持中に呼ぶこと）。"""
        if self._save_blocked:
            return
        try:
            # 書き込み途中のクラッシュでファイルが壊れないよう一時ファイル経由で置換
            tmp = self._path.with_name(self._path.name + ".tmp")
            tmp.write_text(
                json.dumps(self._items, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(self._path)
        except Exception as e:
            # 本文はログに出さない（エラーの種類だけ）
            logger.error(f"クリップボード履歴の保存に失敗: {type(e).__name__}")
