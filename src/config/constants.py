"""
アプリケーション定数・デフォルト設定モジュール

アプリケーション全体で使用される定数値と、
settings.yamlが存在しない場合のデフォルト設定を定義する。
"""

from typing import Any, Dict

from .types import HotkeyMode

# ============================================
# アプリケーションメタデータ
# ============================================
APP_NAME: str = "voicekey"
APP_VERSION: str = "1.1.0"

# ============================================
# 音声設定
# ============================================
SAMPLE_RATE: int = 16000      # サンプリングレート（Hz）
AUDIO_CHANNELS: int = 1       # チャンネル数（モノラル）
AUDIO_DTYPE: str = "float32"  # 音声データ型

# ============================================
# タイミング設定
# ============================================
CONFIG_CHECK_INTERVAL_SEC: int = 1          # 設定ファイル監視間隔（秒）
# ============================================
# デフォルト設定
# ============================================
# settings.yamlが存在しない場合や、キーが欠けている場合に使用される
DEFAULT_CONFIG: Dict[str, Any] = {
    # グローバル設定（両ホットキー共通）
    "language": "ja",
    "vad_filter": True,
    "vad_min_silence_duration_ms": 500,
    "audio_input_device": "default",

    # ハンズフリー切替キー（この切替キー＋ホットキーで toggle 録音。空＝無効）
    "handsfree_key": "",
    # 長文を無音区間で分割し API へ並列送信する（既定オン。Deepgram ストリーミングは対象外）
    "split_parallel_enabled": True,

    # LLM テキスト整形に使う Groq モデル（両ホットキー共通）
    "format_model": "llama-3.1-8b-instant",
    # 整形プロンプト本文（空 = 既定の DEFAULT_FORMAT_PROMPT を使用）
    "format_auto_prompt": "",

    # ホットキー1 設定（製品版の既定: 高速リアルタイム = Deepgram nova-3）
    "hotkey1": {
        "hotkey": "<f2>",
        "hotkey_mode": HotkeyMode.TOGGLE.value,
        "backend": "deepgram",
        "api_model": "",
        "api_prompt": "",
        "format_enabled": True,         # 製品版は裏でテキスト整形（既定オン）
    },

    # ホットキー2 設定（製品版の既定: 正確性 = ElevenLabs scribe_v1）
    "hotkey2": {
        "hotkey": "<f3>",
        "hotkey_mode": HotkeyMode.TOGGLE.value,
        "backend": "elevenlabs",
        "api_model": "",
        "api_prompt": "",
        "format_enabled": True,         # 製品版は裏でテキスト整形（既定オン）
    },

    # APIモデルデフォルト値（バックエンド別、ベンチ実測 2026-06-10 に基づく既定）
    "default_api_models": {
        "groq": "whisper-large-v3-turbo",   # REST 最速
        "openai": "gpt-4o-mini-transcribe",
        "elevenlabs": "scribe_v1",          # 日本語 REST 最高精度（v2 は長文後退）
        "deepgram": "nova-3",               # ストリーミング/REST とも最良
    },

    # リアルタイムストリーミング（Deepgram）。バックエンドが deepgram の
    # ホットキーで、話しながら HUD に文字を表示し離した瞬間に確定する。
    # オフにすると従来どおり録音後に REST でまとめて変換する
    "streaming_enabled": True,

    # 録音中の HUD（画面下部中央の小型ピル）を表示するか
    "hud_enabled": True,

    # 開発者モード - 出力を引用符で囲み、タイミングをファイルに記録
    "dev_mode": False,

    # 起動時プリロード - 起動時にVADを事前ロードして最初の文字起こしを高速化
    "preload_on_startup": True,

    # ダブルタップ Auto-Enter: テキスト挿入後からEnter押下までの待機時間（ms）
    # 一部アプリは即座のEnterに反応しないため調整可能にする
    "auto_enter_delay_ms": 50,

    # 音声前処理（API送信前）
    # volume_normalize: Peak+RMS ハイブリッド正規化（目標 -20 dBFS、ピーク -3 dBFS）
    # ノイズ対策は API モデル側に任せるため、ここでは音量のみ調整する
    "audio_preprocess": {
        "volume_normalize": True,
    },
}

# ============================================
# ファイル名
# ============================================
SETTINGS_FILE_NAME: str = "settings.yaml"
