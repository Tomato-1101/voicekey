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
# 自社バックエンド（製品版: 短命キー発行・プロキシ）
# ============================================
# 製品版（release）は長期 API キーをアプリに同梱せず、自社サーバーがサブスク
# 有効性を検証して Deepgram の短命トークンを発行し、ElevenLabs/Groq はサーバー
# プロキシ経由で叩く。ここはその接続先定数（ユーザーが変える設定ではないため
# settings.yaml ではなくコード定数として持つ）。実 URL の解決は環境（配布/開発）
# 依存のため utils.secrets.get_server_base_url() で行う。
PRODUCT_SERVER_URL: str = "https://voicekey.vercel.app"  # 配布ビルドの既定接続先
LOCAL_SERVER_URL: str = "http://localhost:3000"          # 開発ビルドの既定接続先
# 自社バックエンドの API パス（get_server_base_url() と結合して使う）
API_EPHEMERAL_PATH: str = "/api/v1/auth/ephemeral"                # Deepgram 短命 JWT 発行
API_ELEVENLABS_PROXY_PATH: str = "/api/v1/transcribe/elevenlabs"  # 正確性モードのプロキシ
API_GROQ_TRANSCRIBE_PROXY_PATH: str = "/api/v1/transcribe/groq"   # 高速モード（普通入力・Groq whisper）のプロキシ
API_FORMAT_PROXY_PATH: str = "/api/v1/format"                     # Groq テキスト整形プロキシ
API_FEEDBACK_PATH: str = "/api/v1/feedback"                       # アプリ内フィードバック送信
# ブラウザ経由ログイン（段階4）
AUTH_APP_PATH: str = "/auth/app"                                  # ブラウザで開くログイン入口（state付き）
API_AUTH_EXCHANGE_PATH: str = "/api/v1/auth/exchange"            # ワンタイムコード→トークン交換
API_AUTH_REFRESH_PATH: str = "/api/v1/auth/refresh"             # refresh_token→トークン更新
# アクティベーションキー（段階5）
API_ME_PATH: str = "/api/v1/me"                                   # ログイン中アカウントの状態（email/active/active_until）
API_USAGE_CONFIRM_PATH: str = "/api/v1/usage/confirm"             # 無料体験の消費確定（録音成功後に保留 jti を送る＝段階1）
API_REDEEM_PATH: str = "/api/v1/activation/redeem"               # アクティベーションキー登録（アカウントに紐付け）
# 使用実績のアカウント連携（#10）
API_STATS_SYNC_PATH: str = "/api/v1/stats/sync"                   # この端末の日次・絶対値を upsert
API_STATS_PATH: str = "/api/v1/stats"                            # アカウント横断の日次集計を取得

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

    # LLM テキスト整形に使う Groq モデル（両ホットキー共通）。
    # llama 系は 2026-10 に Groq の API から廃止されたので、Mac と同じおすすめ（Qwen3.8 27B）を既定にする
    "format_model": "qwen/qwen3.8-27b",
    # 整形プロンプト本文（空 = 既定の DEFAULT_FORMAT_PROMPT を使用）
    "format_auto_prompt": "",
    # 貼り付け先が AI（Claude・ChatGPT・ターミナル等。判定は core/ai_destination.py）のときは整形を省くか。
    # AI は言いよどみ入りでも理解できるので Groq の往復を省いて速く・安く入力する（既定 ON・Mac の skipFormatForAI と同義）
    "skip_format_for_ai": True,

    # 最後の文字起こしをもう一度貼り付けるキー（空＝無効）。Mac の既定は ⌃⌘V だが、Windows では
    # Ctrl+Win+V が OS の音声出力の切り替え、Ctrl+Alt+V が Office の「形式を選択して貼り付け」と重なり、
    # pynput はキーを横取りできない（相手のアプリにも届く）ので、既定は未設定にして利用者に選んでもらう
    "repaste_key": "",

    # 翻訳して入力（Mac の DictationTranslation と同じ項目。Apple 翻訳は無いので Groq / Gemini から選ぶ）
    # translate_input_enabled: 常に翻訳して入力するか（全体で 1 つ・スロット単位ではない）
    # translate_input_target: 入力する言語（既定 英語）
    # translate_input_engine: "groq"（既定・無料枠あり）/ "gemini"（有料の場合あり）
    # translate_once_key: 録音中に押すとその回だけ翻訳するキー（空＝無効。既定 右 Shift＝Mac の 右⇧）
    "translate_input_enabled": False,
    "translate_input_target": "en",
    "translate_input_engine": "groq",
    "translate_input_groq_model": "qwen/qwen3.8-27b",
    "translate_input_gemini_model": "gemini-3.5-flash-lite",
    "translate_once_key": "<shift_r>",

    # 音声入力の履歴を保存するか（Mac の historyEnabled と同義・既定 ON。オフなら履歴に残さない）
    "history_enabled": True,

    # API の利用料金の表示モード（"list"＝定価〔無料枠も含む〕/ "paid"＝実際に払った分）
    "api_cost_mode": "list",

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

    # ホットキー1 設定＝メイン（新規インストールの既定: Soniox stt-rt-v5 のライブ型・押している間・整形 OFF）。
    # Mac の新規インストール（Apple ローカルが無い環境）と同じ。保存済みの設定がある人は保存値が優先される
    "hotkey1": {
        "hotkey": "<f2>",
        "hotkey_mode": HotkeyMode.HOLD.value,
        "backend": "soniox",
        "api_model": "",
        "api_prompt": "",
        "format_enabled": False,        # ライブ型は速度全振りのため既定 OFF（Mac と同じ）
    },

    # ホットキー2 設定＝サブ（既定: Groq whisper-large-v3-turbo・トグル）。
    # toggle 録音では app._maybe_handsfree_slot が内部で ElevenLabs(scribe_v1) へ自動切替する
    # （長時間録音の精度対策。保存値 groq は変えない）。
    "hotkey2": {
        "hotkey": "<f3>",
        "hotkey_mode": HotkeyMode.TOGGLE.value,
        "backend": "groq",
        "api_model": "",
        "api_prompt": "",
        "format_enabled": True,         # 製品版は裏でテキスト整形（既定オン）
    },

    # 旧版が保存していたバックエンド別の既定モデル。2026-10-19 からは使わない
    # （既定モデルは config/backends.py の一覧が正本。保存済みの古い値で既定が上書きされないようにするため）。
    # 既存の settings.yaml に残っていても無害なので、読み書きの互換のためだけに空で残す
    "default_api_models": {},

    # ストリーミング文字起こし（Deepgram）。バックエンドが deepgram のホットキーで、
    # 録音と並行して確定テキストを受信し、離した瞬間に全文を一括入力する（即時入力）。
    # オフにすると従来どおり録音後に REST でまとめて変換する
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

    ライブ型（Soniox / OpenAI ライブ / Deepgram）は速度全振りのため既定 OFF、
    録音後に送る型（Groq / ElevenLabs / OpenAI / Microsoft MAI）は既定 ON。
    実体は config/backends.py にあり、ここは旧来の呼び出し口として残している。

    Args:
        backend: バックエンド識別子（"soniox" / "groq" 等）

    Returns:
        そのエンジンの整形既定
    """
    from .backends import default_format_enabled as _default

    return _default(backend)
