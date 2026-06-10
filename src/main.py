"""
アプリケーションエントリーポイントモジュール

アプリケーションの起動処理を行う。
環境変数の読み込み、ロギング設定、Qtアプリケーションの初期化を行う。
"""

import sys
import traceback

from dotenv import load_dotenv
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from .app import VoicekeyApp
from .platform import get_platform_adapter
from .utils.logger import get_logger, setup_logger

# .envファイルから環境変数を読み込み（APIキー等）
load_dotenv()

# ロギング設定
setup_logger(log_file="startup_log.txt")
logger = get_logger(__name__)


def main() -> int:
    """
    アプリケーションのメインエントリーポイント。
    
    Returns:
        終了コード（0: 成功、非0: 失敗）
    """
    try:
        # 高DPIディスプレイサポートを設定
        _configure_high_dpi()
        
        # Qtアプリケーションを作成
        app = QApplication(sys.argv)
        app.setQuitOnLastWindowClosed(False)  # システムトレイ動作のため

        # macOS では Dock / Cmd+Tab から隠す（メニューバー常駐アプリとして動作）。
        # Windows / Linux ではアダプタが no-op なので副作用なし。
        get_platform_adapter().configure_app_visibility(hide_from_dock=True)

        # メインコントローラーを作成
        controller = VoicekeyApp()
        
        # イベントループを実行
        return app.exec()
        
    except Exception as e:
        _handle_critical_error(e)
        return 1


def _configure_high_dpi() -> None:
    """高DPIディスプレイサポートを設定する。"""
    if hasattr(Qt.ApplicationAttribute, 'AA_EnableHighDpiScaling'):
        QApplication.setAttribute(Qt.ApplicationAttribute.AA_EnableHighDpiScaling, True)
    if hasattr(Qt.ApplicationAttribute, 'AA_UseHighDpiPixmaps'):
        QApplication.setAttribute(Qt.ApplicationAttribute.AA_UseHighDpiPixmaps, True)


def _handle_critical_error(error: Exception) -> None:
    """
    致命的エラーをログに記録しファイルに書き出す。
    
    Args:
        error: 発生した例外
    """
    error_msg = f"致命的エラー: {error}"
    logger.critical(error_msg, exc_info=True)
    
    # 詳細なエラー情報をファイルに書き出し
    with open("error_log.txt", "w", encoding="utf-8") as f:
        traceback.print_exc(file=f)
    
    print(error_msg)


if __name__ == "__main__":
    sys.exit(main())
