"""
データファイルの置き場所モジュール

settings.yaml・history.json・stats.json と同じ場所（凍結ビルドは実行ファイルの隣、
開発実行はリポジトリ直下）を返す。新しく増えるデータファイル（API 使用量・Soniox の
接続先の記憶など）も同じ場所に置き、ユーザーが 1 か所を見れば全部あるようにする。
"""

import sys
from pathlib import Path


def data_dir() -> Path:
    """データファイルを置くディレクトリ（settings.yaml と同じ配置ロジック）。

    Returns:
        凍結ビルドは実行ファイルのあるディレクトリ、開発実行はプロジェクトルート
    """
    if getattr(sys, "frozen", False):
        # PyInstaller 実行時は実行ファイルと同じディレクトリ
        return Path(sys.executable).parent
    # 開発時はプロジェクトルート（src の親）
    return Path(__file__).parent.parent.parent
