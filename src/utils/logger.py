"""
ロギングモジュール

アプリケーション全体で使用されるロギング機能を提供する。
コンソールとファイルへの同時出力に対応。
"""

import logging
import logging.handlers
import os
import sys
from pathlib import Path
from typing import Dict, Optional

# デフォルトのログフォーマット
LOG_FORMAT: str = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"

# ログの保持日数（日付が変わるたびにローテートし、これより古い分は自動削除する）
LOG_RETENTION_DAYS: int = 14

# シングルトンロガーインスタンス
_loggers: Dict[str, logging.Logger] = {}
_is_configured: bool = False


def default_log_dir() -> Path:
    """OS 標準のログ保存ディレクトリを返す。

    ログ先を作業ディレクトリ（cwd）に置くと、ログイン時自動起動やショートカット起動では
    cwd が C:\\Windows\\System32 等になり、相対パスのログが書けない／想定外の場所に
    散らばる。OS 標準のユーザー書き込み可能ディレクトリに固定してこれを避ける。

    - Windows: %LOCALAPPDATA%\\voicekey\\logs
    - macOS:   ~/Library/Logs/voicekey
    - その他:  $XDG_STATE_HOME/voicekey/logs（無ければ ~/.local/state/...）
    """
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return Path(base) / "voicekey" / "logs"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Logs" / "voicekey"
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "state"
    )
    return Path(base) / "voicekey" / "logs"


def setup_logger(
    log_file: Optional[str] = "app.log",
    level: int = logging.INFO,
    format_string: str = LOG_FORMAT
) -> None:
    """
    ルートロガーをコンソールとファイルハンドラーで設定する。

    Args:
        log_file: ログファイル名またはパス。Noneでファイル出力を無効化。
            相対名（例 "app.log"）は OS 標準ログディレクトリ配下に置く。
            絶対パスはそのまま使う。
        level: ログレベル（デフォルト: INFO）
        format_string: ログメッセージフォーマット
    """
    global _is_configured

    # 二重設定を防止
    if _is_configured:
        return

    handlers = [logging.StreamHandler(sys.stdout)]

    if log_file:
        # 相対名は OS 標準ログディレクトリへ解決（cwd 依存を避ける）。
        # ディレクトリ作成やファイルオープンに失敗してもアプリは止めず、コンソール出力で継続する。
        try:
            path = Path(log_file)
            if not path.is_absolute():
                log_dir = default_log_dir()
                log_dir.mkdir(parents=True, exist_ok=True)
                path = log_dir / path
            # 日付でローテートして LOG_RETENTION_DAYS 日分だけ残す。
            # 以前は mode='w' で起動のたびに上書きしていたため、再起動を挟むと
            # 障害直前の行動が消えて原因を追えなかった（Mac の行動ログと方針を揃える）。
            handlers.append(
                logging.handlers.TimedRotatingFileHandler(
                    path,
                    when='midnight',
                    backupCount=LOG_RETENTION_DAYS,
                    encoding='utf-8',
                )
            )
        except OSError as e:
            print(
                f"ログファイルを作成できませんでした（コンソールのみで継続）: {e}",
                file=sys.stderr,
            )

    logging.basicConfig(
        level=level,
        format=format_string,
        handlers=handlers
    )

    _is_configured = True


def get_logger(name: str) -> logging.Logger:
    """
    名前でロガーインスタンスを取得する（キャッシュ付き）。
    
    Args:
        name: ロガー名（通常は__name__）
        
    Returns:
        設定済みのロガーインスタンス
    """
    if name not in _loggers:
        _loggers[name] = logging.getLogger(name)
    return _loggers[name]
