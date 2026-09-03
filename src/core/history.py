"""
音声入力履歴モジュール

文字起こし（整形後）のテキストを新しい順に最大 10 件保持し、
設定ウィンドウの「履歴」タブから再コピーできるようにする。
アプリを再起動しても残るよう settings.yaml と同じディレクトリに JSON で保存する。
各エントリには Mac と履歴を共有する history_sync 用に id（uuid4 hex）と
device（"windows"）を持たせる（本モジュール自体は通信を一切行わない）。
"""

import json
import sys
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional

from ..utils.logger import get_logger

logger = get_logger(__name__)

# 保持する最大件数（超過分は古いものから捨てる）。Mac 版 HistoryStore.maxItems と同値
MAX_ITEMS = 10
HISTORY_FILE_NAME = "history.json"


def _default_path() -> Path:
    """履歴ファイルの既定パス（settings.yaml と同じ配置ロジック）。"""
    if getattr(sys, "frozen", False):
        # PyInstaller 実行時は実行ファイルと同じディレクトリ
        base_dir = Path(sys.executable).parent
    else:
        # 開発時はプロジェクトルート
        base_dir = Path(__file__).parent.parent.parent
    return base_dir / HISTORY_FILE_NAME


class HistoryStore:
    """
    音声入力履歴のストア（スレッドセーフ）。

    add() は文字起こしワーカースレッドから、items() / clear() は
    UI スレッドから呼ばれるため、内部状態はロックで保護する。

    Attributes:
        _items: 履歴エントリのリスト（新しい順）。各要素は
            {"id": uuid4 hex, "text": str, "date": ISO 8601 str, "device": "windows"}
    """

    def __init__(
        self,
        file_path: Optional[Path] = None,
        on_add: Optional[Callable[[dict], None]] = None,
    ) -> None:
        """
        履歴ストアを初期化し、保存済みの履歴を読み込む。

        Args:
            file_path: 履歴ファイルのパス（テスト用。省略時は既定パス）
            on_add: 追加成功後に呼ぶコールバック（history_sync.enqueue を想定）。
                履歴ストア自体は通信を知らず、貼り付け経路をブロックしないよう
                保存完了後・ロック外で呼び、例外は握って警告ログのみ出す
                （同期の失敗が貼り付けを壊すことは絶対に避ける）。
        """
        self._path = Path(file_path) if file_path else _default_path()
        self._lock = threading.Lock()
        self._on_add = on_add
        self._items: List[Dict[str, str]] = self._load()

    def add(self, text: str) -> None:
        """
        履歴に 1 件追加して保存する（空テキストは無視）。

        Args:
            text: 貼り付けたテキスト
        """
        if not text or not text.strip():
            return
        entry = {
            "id": uuid.uuid4().hex,
            "text": text,
            "date": datetime.now().astimezone().isoformat(timespec="seconds"),
            "device": "windows",
        }
        with self._lock:
            self._items.insert(0, entry)
            del self._items[MAX_ITEMS:]
            self._save()
        if self._on_add is not None:
            try:
                self._on_add(dict(entry))
            except Exception as e:
                logger.warning(f"履歴追加コールバックに失敗（無視）: {e}")

    def items(self) -> List[Dict[str, str]]:
        """
        履歴のコピーを取得する（新しい順）。

        Returns:
            {"id", "text", "date", "device"} 辞書のリスト
        """
        with self._lock:
            return [dict(entry) for entry in self._items]

    def clear(self) -> None:
        """履歴をすべて消去して保存する（発話内容をディスクに残したくないとき用）。"""
        with self._lock:
            self._items = []
            self._save()

    def _load(self) -> List[Dict[str, str]]:
        """保存済みの履歴を読み込む。壊れていれば空で開始する（クラッシュさせない）。

        history_sync 導入前（id/device が無い）の旧ファイルも読めるよう、
        欠けていれば id は新規採番、device は "windows" を補う。
        """
        try:
            if self._path.exists():
                data = json.loads(self._path.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    return [
                        {
                            "id": str(e.get("id") or uuid.uuid4().hex),
                            "text": str(e.get("text", "")),
                            "date": str(e.get("date", "")),
                            "device": str(e.get("device") or "windows"),
                        }
                        for e in data
                        if isinstance(e, dict) and str(e.get("text", "")).strip()
                    ][:MAX_ITEMS]
        except Exception as e:
            logger.warning(f"履歴の読み込みに失敗（空で開始します）: {e}")
        return []

    def _save(self) -> None:
        """履歴をファイルに保存する（_lock 保持中に呼ぶこと）。"""
        try:
            # 書き込み途中のクラッシュで履歴ファイルが壊れないよう一時ファイル経由で置換
            tmp = self._path.with_name(self._path.name + ".tmp")
            tmp.write_text(
                json.dumps(self._items, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(self._path)
        except Exception as e:
            logger.error(f"履歴の保存に失敗: {e}")
