"""
voicekey - macOS/Windows対応の音声文字起こしアプリケーション

Groq/OpenAI APIを使用した高速音声認識に対応した、
プライバシー重視の音声入力ツール（VADはローカル実行）。
"""

__version__ = "2.0.0"
__author__ = "voicekey Team"

from .app import VoicekeyApp
from .main import main

__all__ = [
    "VoicekeyApp",  # メインアプリケーションクラス
    "main",         # エントリーポイント関数
    "__version__",  # バージョン番号
]
