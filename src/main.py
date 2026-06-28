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

# .env ファイルから環境変数を読み込み（API キー等）。
# 配布（DIST）ビルドでは読み込まない＝作業ディレクトリの .env で接続先や鍵を
# 汚染されないようにする。override 判定のため secrets を先に取り込む。
from .utils import secrets as _secrets  # noqa: E402

if not _secrets.is_dist_build():
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

        # ブラウザ経由ログインの deep link（voicekey://）を扱う（製品版・段階4）。
        # OS がスキームハンドラとして起動すると argv に URL が載る。2 つ目以降の
        # インスタンスは URL を稼働中インスタンスへ転送して即終了する（単一インスタンス化）。
        from .core import deep_link

        deep_url = deep_link.extract_url_from_argv(sys.argv)
        single = deep_link.SingleInstance()
        if not single.try_become_primary():
            # 既に起動済み: deep link があれば転送して終了（無ければ二重起動を防いで終了）
            if deep_url:
                single.send_to_primary(deep_url)
            return 0

        # macOS では Dock / Cmd+Tab から隠す（メニューバー常駐アプリとして動作）。
        # Windows / Linux ではアダプタが no-op なので副作用なし。
        get_platform_adapter().configure_app_visibility(hide_from_dock=True)

        # 配布（凍結）ビルドのみ: voicekey:// を OS に登録する（他環境は no-op）
        deep_link.register_url_scheme()

        # メインコントローラーを作成
        controller = VoicekeyApp()

        # 稼働中インスタンスへ転送された deep link をログイン処理へ渡す
        single.url_received.connect(controller.handle_deep_link)
        # アプリ未起動の状態でログインから戻ってきた場合（起動引数に URL）
        if deep_url:
            controller.handle_deep_link(deep_url)

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
