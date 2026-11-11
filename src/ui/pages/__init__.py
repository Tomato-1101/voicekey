"""
設定ウィンドウのページ（1 ファイル 1 ページ）

各ページは PageContext を受け取る QWidget。ウィンドウ（settings_window.SettingsWindow）が
開いたときに初めて作るので、ページの import・構築は軽く保つ。
"""

from .context import PageContext

__all__ = ["PageContext"]
