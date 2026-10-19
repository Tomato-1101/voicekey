"""
壊れたデータファイルの退避モジュール（Mac 版 CorruptFileQuarantine.swift の移植）

設定・履歴・実績・API 使用量のファイルが読めなかったとき、黙って空で始めると次の保存で
元の内容を上書きして消してしまう。読めなかったファイルは同じフォルダの
`<名前>.corrupt-<日時>` へ移して残し、本人が気づけるよう HUD で 1 回だけ知らせる。
移せなかったときは呼び出し側が「保存しない」ことで元ファイルを守る。
"""

import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional

from .logger import get_logger

logger = get_logger(__name__)

_lock = threading.Lock()
_handler: Optional[Callable[[str], None]] = None
# 画面の準備より先に起きた通知（ストアはアプリの画面より先に作られるので溜めておいて後で渡す）
_pending: List[str] = []

# ファイルの移動（テストで「移せなかった」を差し込むために差し替えられるようにしてある）
move_file: Callable[[str, str], None] = os.replace


def quarantine(path, label: str = "ファイル", now: Optional[datetime] = None) -> Optional[Path]:
    """読めなかったファイルを `<名前>.corrupt-<日時>` へ移す。

    Args:
        path: 壊れていたファイル
        label: 通知に出すファイルの呼び名（例「履歴ファイル」）
        now: 日時（テスト用）

    Returns:
        移動先。移せなかったときは None（その場合、呼び出し側は**保存しない**こと。
        空で始めたまま保存すると、残した元ファイルを空の内容で上書きして消してしまう）
    """
    src = Path(path)
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    base = f"{src.name}.corrupt-{stamp}"
    destination = src.with_name(base)
    # 同じ秒に 2 回壊れても前の退避を潰さない
    suffix = 2
    while destination.exists():
        destination = src.with_name(f"{base}-{suffix}")
        suffix += 1
    try:
        move_file(str(src), str(destination))
    except Exception as e:
        logger.warning(f"読めないファイルを退避できませんでした: {src.name} ({e})")
        _notify(f"{label}が壊れていて退避できませんでした。上書きせずに残しています: {src.name}")
        return None
    logger.warning(f"読めないファイルを退避: {destination.name}")
    _notify(f"{label}が壊れていたので退避しました: {destination.name}")
    return destination


def set_notice_handler(handler: Callable[[str], None]) -> None:
    """通知の受け口を設定する（溜まっていた通知もここで渡す）。"""
    global _handler
    with _lock:
        _handler = handler
        queued = list(_pending)
        _pending.clear()
    for message in queued:
        _safe_call(handler, message)


def _notify(message: str) -> None:
    with _lock:
        handler = _handler
        if handler is None:
            _pending.append(message)
            return
    _safe_call(handler, message)


def _safe_call(handler: Callable[[str], None], message: str) -> None:
    try:
        handler(message)
    except Exception:
        pass


def _reset_for_tests() -> None:
    """テスト用: 受け口と溜まった通知を消す。"""
    global _handler
    with _lock:
        _handler = None
        _pending.clear()
