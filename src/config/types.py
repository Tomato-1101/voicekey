"""
型定義モジュール

アプリケーション設定で使用される列挙型とデータクラスを定義する。
型安全な設定管理のための基盤を提供する。
"""

from enum import Enum
from dataclasses import dataclass
from typing import Any


class HotkeyMode(str, Enum):
    """
    ホットキー動作モード。
    
    Attributes:
        TOGGLE: トグルモード - 1回押して開始、もう1回押して停止
        HOLD: ホールドモード - 押している間録音、離すと停止
    """
    TOGGLE = "toggle"
    HOLD = "hold"


class AppState(str, Enum):
    """
    アプリケーション状態。
    
    UIの表示やシステムトレイアイコンの色に使用される。
    """
    IDLE = "idle"                                    # 待機中
    RECORDING = "recording"                          # 録音中
    RECORDING_AUTO_ENTER = "recording_auto_enter"    # 録音中（auto_enterモード）
    TRANSCRIBING = "transcribing"                    # 文字起こし中


class TranscriptionBackend(str, Enum):
    """
    文字起こしバックエンドタイプ。

    Attributes:
        GROQ: Groq Cloud API（OpenAI 互換 REST、Whisper）
        OPENAI: OpenAI GPT-4o Transcribe API（OpenAI 互換 REST）
        ELEVENLABS: ElevenLabs Scribe API（REST、短文高精度）
        DEEPGRAM: Deepgram Listen API（REST + WebSocket ストリーミング、nova-3）
    """
    GROQ = "groq"
    OPENAI = "openai"
    ELEVENLABS = "elevenlabs"
    DEEPGRAM = "deepgram"


@dataclass
class TranscriptionTask:
    """
    キューに入れる文字起こしタスク。

    Attributes:
        audio_data: 音声データ（NumPy配列）
        slot_id: 使用するホットキースロットID
        timestamp: タスク作成時刻
        auto_enter: 文字起こし後にEnterキーを自動入力するか
        streamer: ストリーミング録音時の Deepgram セッション（非ストリーミング時は None）。
                  ワーカーが finish() を呼んで確定テキストを取得する。型は循環参照を
                  避けるため Any（実体は core.streaming_transcriber.StreamingTranscriber）
    """
    audio_data: Any
    slot_id: int
    timestamp: float
    auto_enter: bool = False
    streamer: Any = None


@dataclass
class TranscriberConfig:
    """
    文字起こしモジュールの共通設定。
    """
    language: str = "ja"
    vad_filter: bool = True
    vad_min_silence_duration_ms: int = 500


@dataclass
class HotkeyConfig:
    """ホットキー設定。"""
    hotkey: str = "<f2>"
    hotkey_mode: str = HotkeyMode.TOGGLE.value


@dataclass
class HotkeySlotConfig:
    """
    個別ホットキースロットの設定。

    各ホットキーに対して、キーバインド、動作モード、
    使用するバックエンドとAPIモデル設定を保持する。

    Attributes:
        hotkey: ホットキー文字列（例: "<shift_r>", "<ctrl>+<space>"）
        hotkey_mode: 動作モード（hold/toggle）
        backend: 使用するバックエンド（groq/openai）
        api_model: APIバックエンド使用時のモデル名
        api_prompt: APIバックエンド使用時のプロンプト
    """
    hotkey: str = "<f2>"
    hotkey_mode: str = HotkeyMode.TOGGLE.value
    backend: str = TranscriptionBackend.OPENAI.value
    api_model: str = ""
    api_prompt: str = ""


@dataclass
class AppConfig:
    """
    アプリケーション全体の設定。
    
    すべての設定項目を一つにまとめたデータクラス。
    """
    # ホットキー設定
    hotkey: str = "<f2>"
    hotkey_mode: str = HotkeyMode.TOGGLE.value
    
    # 共通設定
    language: str = "ja"
    
    # 文字起こし設定
    vad_filter: bool = True
    vad_min_silence_duration_ms: int = 500
