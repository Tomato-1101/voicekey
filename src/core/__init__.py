"""
コアビジネスロジックモジュール

音声録音、文字起こし、VAD、テキスト入力など、
アプリケーションの中核機能を提供するモジュール群。
"""

from .api_transcriber import ApiTranscriber, GroqTranscriber, OpenAITranscriber, TranscriptionError
from .audio_recorder import AudioRecorder
from .input_handler import InputHandler
from .vad import SileroVad

__all__ = [
    "ApiTranscriber",       # API 文字起こし共通基盤
    "AudioRecorder",        # 音声録音
    "GroqTranscriber",      # Groq API 文字起こし
    "OpenAITranscriber",    # OpenAI API 文字起こし
    "TranscriptionError",   # 文字起こし失敗例外
    "InputHandler",         # テキスト入力
    "SileroVad",            # 発話検出（ONNX 版 Silero VAD）
]
