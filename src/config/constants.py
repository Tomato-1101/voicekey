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
APP_VERSION: str = "1.8.0"

# ============================================
# 自動アップデートの署名検証（Windows 配布版）
# ============================================
# 配布物（setup.exe）を開発者だけが持つ Ed25519 秘密鍵で署名し、ここに埋め込んだ
# 固定公開鍵で検証する。version.json のホスティングが改竄・MITM されても、秘密鍵を
# 持たない第三者は正規の署名を作れないため不正な更新を実行できない（SHA256 はフィードを
# 信頼できる場合の破損検出にすぎない）。Mac 版は Sparkle EdDSA（SUPublicEDKey）で同等。
# 秘密鍵は ~/.voicekey/voicekey_update_ed25519（コミット禁止）。鍵生成は
# scripts/build/generate_update_key.py、配布物への署名は scripts/build/sign_update.py。
# 空文字にすると署名検証をスキップ（SHA256 のみ）するため、配布ビルドでは必ず設定すること。
UPDATE_PUBLIC_KEY_ED25519: str = "TgVQf5ozPf1BwPfL2L7O9Dy4XU/Bowup38lgjEJPUx4="

# ============================================
# フィードバック送信先
# ============================================
# トレイメニュー「フィードバックを送る…」の送信先（匿名・ログイン不要）。
# ユーザーが変える設定ではないため settings.yaml ではなくコード定数として持つ。
FEEDBACK_URL: str = "https://voicekey.vercel.app/api/v1/feedback"

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
    # 長文を無音区間で分割し API へ並列送信する（既定オン。録音しながら送るエンジンは対象外）
    "split_parallel_enabled": True,

    # LLM テキスト整形に使う Groq モデル（両ホットキー共通。一覧は core/llm_catalog.py）
    "format_model": "qwen/qwen3.8-27b",
    # 整形プロンプト本文（空 = スロットごとの「整え方」の本文を使用）
    "format_auto_prompt": "",

    # ユーザー辞書（確定置換）。文字起こし・整形が終わった最終テキストに対し、
    # 貼り付け直前で from→to を機械置換する（API を通さないので遅延ゼロ）。
    # 各要素: {"from": 置換元, "to": 置換先, "enabled": 有効か}
    "replacements": [],

    # 数字入力の正規化（numeral_normalizer・Mac 版 ConfigStore と同義）。
    # numeral_normalize_enabled: マスター（False で完全パススルー）
    # numeral_convert_counter: 単独漢数字＋助数詞（三時→3時）を変換するか（位取り>=2 はマスターのみで常時変換）
    # numeral_protect_words: 変換しない語（数字漢字で始まる語をラン先頭にアンカー照合）。
    #   既定シードは「一人／二人＝ひとり・ふたり」「十分＝じゅうぶん」等の誤変換を守る。
    "numeral_normalize_enabled": True,
    "numeral_convert_counter": True,
    "numeral_protect_words": [
        "一時的", "一時停止", "一人", "二人", "十分", "一日中", "一部始終", "一石二鳥",
        # いちばん（＝最も）・いちど（もう一度）を守る（番・度は助数詞なので既定では 1番/1度 になる）
        "一番", "一度",
    ],

    # ホットキー1 設定＝普通入力（既定: Soniox・押している間。しゃべりながら送り、離した瞬間に入力）。
    # backend はエンジン id（core/engines.py の一覧・Mac 版と同じ文字列）。
    # api_model が空ならそのエンジンの既定モデルを使う。
    "hotkey1": {
        "hotkey": "<f2>",
        "hotkey_mode": HotkeyMode.HOLD.value,
        "backend": "soniox",
        "api_model": "",
        "api_prompt": "",
        "format_enabled": False,        # Soniox は速さ優先のため整形は既定オフ
        "format_preset": "standard",    # 整え方（standard / punctuation / clean / bullets）
    },

    # ホットキー2 設定＝ハンズフリー（既定: Groq whisper-large-v3-turbo・トグル）。
    # toggle 録音では app._maybe_handsfree_slot が内部で ElevenLabs(scribe_v1) へ自動切替する
    # （長時間録音の精度対策。保存値 groq は変えない）。
    "hotkey2": {
        "hotkey": "<f3>",
        "hotkey_mode": HotkeyMode.TOGGLE.value,
        "backend": "groq",
        "api_model": "",
        "api_prompt": "",
        "format_enabled": True,
        "format_preset": "standard",
    },

    # ストリーミング文字起こし。録音しながら送るエンジン（Soniox・OpenAI ライブ）のホットキーで、
    # 録音と並行して確定テキストを受信し、離した瞬間に全文を一括入力する。
    "streaming_enabled": True,

    # 録音中の HUD（画面下部中央の小型ピル）を表示するか
    "hud_enabled": True,

    # 待機中も小型ピルを常時表示するか（Mac 版 hudAlwaysVisible と同義・既定 OFF）。
    # hud_enabled と違い _force_always_on の対象にはしない（ユーザーが自由に ON/OFF する項目）。
    "hud_always_visible": False,

    # 操作音（録音開始/停止の効果音）を鳴らすか（Mac 版 soundEffectsEnabled と同義・既定 ON）
    "sound_effects_enabled": True,

    # 録音中は他アプリ（メディア）の音量を下げるか（Mac 版 duckMediaEnabled と同義・既定 ON）
    "duck_media_enabled": True,

    # 画面端のサイドノッチ（履歴スリット）を表示するか（Mac 版 sideNotchEnabled と同義・既定 ON）
    "side_notch_enabled": True,

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

    # 初回起動オンボーディング（Phase 5）を完了/スキップしたか。
    # False かつ「起動時に settings.yaml が存在しなかった（＝完全な初回起動）」ときだけ
    # セットアップウィンドウを出す。既存ユーザー（ファイルが既にあった）は補完して出さない。
    "did_complete_onboarding": False,

    # Mac と履歴を共有する Cloudflare Worker（sync-worker/）。トークンは keyring（voicekey.SyncToken）に置き、設定ファイルには書かない
    "history_sync": {
        "enabled": False,
        "url": "",
    },
}

# ============================================
# ファイル名
# ============================================
SETTINGS_FILE_NAME: str = "settings.yaml"


def default_format_enabled(backend: str) -> bool:
    """エンジン別のテキスト整形の既定 ON/OFF（Mac 版 Backend.defaultFormatEnabled と一致）。

    録音しながら送るエンジン（Soniox・OpenAI ライブ）は速さ優先のため既定 OFF、
    録音後に送るエンジンは既定 ON。正本は core/engines.py のカタログ。

    Args:
        backend: エンジン id（旧い値・知らない値は読み替えてから判定する）

    Returns:
        そのエンジンの整形既定
    """
    # config パッケージの読み込み中に core を読むと循環するので、呼ばれたときに読む
    from ..core import engines
    return engines.engine(engines.normalize_id(backend)).default_format_enabled
