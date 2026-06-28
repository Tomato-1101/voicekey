"""
voicekey - macOS/Windows対応の音声文字起こしアプリケーション

Groq/OpenAI APIを使用した高速音声認識に対応した、
プライバシー重視の音声入力ツール（VADはローカル実行）。
"""

# バージョンは constants.APP_VERSION を単一ソースとする（#27）。
# ここでハードコードすると Info.plist / インストーラー / 更新 API / README と
# 二重管理になり食い違う（実際 2.0.0 と実配布 1.2.0 がズレていた）。
from .config.constants import APP_VERSION as __version__

__author__ = "voicekey Team"

from .app import VoicekeyApp
from .main import main

__all__ = [
    "VoicekeyApp",  # メインアプリケーションクラス
    "main",         # エントリーポイント関数
    "__version__",  # バージョン番号
]
