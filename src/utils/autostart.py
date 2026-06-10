"""
自動起動（ログイン時起動）モジュール

Windows のログイン時自動起動を、レジストリの Run キー
（HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run）で管理する。

Mac ネイティブ版（Swift）は SMAppService で同等機能を持つため、この
Python 実装は Windows 専用。非対応プラットフォームでは is_supported() が
False を返し、すべての操作は安全に no-op（例外を投げず False を返す）となる。
"""

import sys
from pathlib import Path

from .logger import get_logger

logger = get_logger(__name__)

# レジストリ上のエントリ名（HKCU の Run キー配下の値名）
_APP_NAME = "voicekey"
# ログイン時に実行するプログラムを登録する標準キー
_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def is_supported() -> bool:
    """ログイン時起動の設定がこのプラットフォームで可能か返す。"""
    return sys.platform.startswith("win")


def _launch_command() -> str:
    """
    自動起動で実行するコマンド文字列を組み立てる。

    PyInstaller で凍結済みなら exe を直接、開発実行なら
    pythonw.exe（コンソール無し）+ run.py を起動する。
    パスにスペースが含まれても壊れないよう各要素を引用符で括る。

    Returns:
        レジストリ REG_SZ に格納するコマンド文字列
    """
    if getattr(sys, "frozen", False):
        # 凍結 exe（dist/voicekey/voicekey.exe など）を直接起動
        return f'"{sys.executable}"'

    # 開発実行: コンソールを出さない pythonw でエントリスクリプトを起動
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    interpreter = pythonw if pythonw.exists() else exe
    run_py = Path(__file__).resolve().parents[2] / "run.py"
    return f'"{interpreter}" "{run_py}"'


def is_enabled() -> bool:
    """
    ログイン時起動が有効かどうかを返す。

    Returns:
        有効なら True（非対応プラットフォーム・未登録では False）
    """
    if not is_supported():
        return False
    try:
        import winreg  # type: ignore  # Windows 専用、遅延 import

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, _APP_NAME)
            return bool(value)
    except FileNotFoundError:
        # 値が未登録（= 無効）
        return False
    except OSError as e:
        logger.warning(f"自動起動状態の取得に失敗: {e}")
        return False


def set_enabled(enabled: bool) -> bool:
    """
    ログイン時起動を有効/無効にする。

    Args:
        enabled: True で有効化（Run キーに登録）、False で無効化（削除）

    Returns:
        操作が成功した場合 True（非対応プラットフォームでは False）
    """
    if not is_supported():
        return False
    try:
        import winreg  # type: ignore  # Windows 専用、遅延 import

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            _RUN_KEY,
            0,
            winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE,
        ) as key:
            if enabled:
                winreg.SetValueEx(key, _APP_NAME, 0, winreg.REG_SZ, _launch_command())
            else:
                try:
                    winreg.DeleteValue(key, _APP_NAME)
                except FileNotFoundError:
                    pass  # 既に無効なら何もしない
        logger.info(f"ログイン時起動を{'有効' if enabled else '無効'}にしました")
        return True
    except OSError as e:
        logger.warning(f"自動起動設定の更新に失敗: {e}")
        return False
