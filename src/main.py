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
from .utils.logger import default_log_dir, get_logger, setup_logger
from .utils.single_instance import SingleInstance

# .env から環境変数を読む（API キー等のフォールバック）。固めた実行ファイルでは読まない
# （起動した場所にたまたまある .env を拾って、意図しないキーで動くのを避ける）
if not getattr(sys, "frozen", False):
    load_dotenv()

# ロギング設定。行動ログの置き場所は README「ログの場所」と同じ
# %LOCALAPPDATA%\voicekey\logs\app.log（旧名 startup_log.txt は README・Mac 版の
# 行動ログの案内とずれていたため改名。ローテートは logger.setup_logger 側）
setup_logger(log_file="app.log")
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

        # 二重起動防止: 既に起動済みなら何もせず終了する（ホットキーの二重監視を防ぐ）
        single = SingleInstance()
        if not single.try_become_primary():
            return 0

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

    # 詳細なエラー情報をファイルに書き出し。書き先は app.log と同じ OS 標準ログ
    # ディレクトリ（Windows は %LOCALAPPDATA%\voicekey\logs）に固定する。凍結ビルドの
    # 起動時 CWD は Program Files 等の書けない場所もありうるため、相対パスの open だと
    # PermissionError でクラッシュ報告自体が未処理例外になる（＋ログが散らばる）。
    try:
        log_dir = default_log_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        with open(log_dir / "error_log.txt", "w", encoding="utf-8") as f:
            traceback.print_exc(file=f)
    except OSError as e:
        # 書けない環境ではファイル出力を諦め、標準エラー出力にだけ残す
        print(f"error_log.txt を書き出せませんでした: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)

    print(error_msg)


if __name__ == "__main__":
    sys.exit(main())
