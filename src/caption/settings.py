"""
ライブ字幕と Meet ボットの設定（Mac 版 CaptionSettings.swift の移植）

値の正本は settings.yaml の `caption:` と `meet_bot:` の 2 つの辞書。ここはその読み出しを
1 か所にまとめ、既定値と不正値の矯正を受け持つ（設定画面・字幕サービス・ボットが同じ解釈をするため）。
秘密情報（API キー）はここに置かない（`utils.secrets` が資格情報マネージャー / 環境変数から読む）。

Windows では Apple の端末内認識・翻訳が無いので、エンジンは API だけ:
- 翻訳エンジン: Soniox（認識と翻訳をまとめて任せる・最速）/ Groq（既定・安い）/ Gemini（選んだときだけ）
- 認識エンジン（Groq / Gemini で訳すとき・文字起こしモード）: Soniox（既定）/ OpenAI ライブ
"""

from typing import Any, Dict, Optional

from ..config import llm_models

# --- 字幕の動作モード ---
MODE_TRANSLATE = "translate"
MODE_TRANSCRIBE = "transcribe"
MODE_LABELS = {
    MODE_TRANSLATE: "翻訳（英語 → 日本語）",
    MODE_TRANSCRIBE: "文字起こし（訳さない）",
}

# --- 文字起こしの言語 ---
LANG_JA = "ja"
LANG_EN = "en"
LANGUAGE_LABELS = {LANG_JA: "日本語", LANG_EN: "英語"}

# --- 翻訳エンジン ---
ENGINE_SONIOX = "soniox"
ENGINE_GROQ = "groq"
ENGINE_GEMINI = "gemini"
ENGINES = (ENGINE_SONIOX, ENGINE_GROQ, ENGINE_GEMINI)

# --- 認識エンジン ---
RECOGNIZER_SONIOX = "soniox"
RECOGNIZER_OPENAI = "openai_live"
RECOGNIZERS = (RECOGNIZER_SONIOX, RECOGNIZER_OPENAI)
RECOGNIZER_LABELS = {
    RECOGNIZER_SONIOX: "Soniox（約 $0.12/時）",
    RECOGNIZER_OPENAI: "OpenAI ライブ（約 $1.02/時）",
}
# 認識に使うモデル（API 使用量の記録にも使う。音声入力のライブと同じもの）
RECOGNIZER_MODELS = {
    RECOGNIZER_SONIOX: "stt-rt-v5",
    RECOGNIZER_OPENAI: "gpt-live-transcribe",
}

# --- キャプチャ対象 ---
SCOPE_FRONTMOST = "frontmost"
SCOPE_ALL = "all"

# --- Meet ボットの文字起こしの取り方 ---
MEET_SOURCE_CAPTIONS = "meetCaptions"
# Mac の localSpeech（端末内認識）の代わり。Windows はローカル認識を持たないので API で文字起こしする
MEET_SOURCE_API = "apiSpeech"
MEET_SOURCE_LABELS = {
    MEET_SOURCE_CAPTIONS: "Meet の字幕を使う（話者名つき）",
    MEET_SOURCE_API: "会議の音声を API で文字起こし",
}
MEET_SOURCE_SUMMARIES = {
    MEET_SOURCE_CAPTIONS: "Meet の字幕",
    MEET_SOURCE_API: "会議の音声（API）",
}

# 字幕の Groq の既定モデル。
# Mac の字幕の Groq の既定はおすすめ（qwen/qwen3.8-27b）だが、Windows は本人の指示「翻訳は安い API がいい」に従い、
# 単価表（ApiPricing.swift / api_usage._TABLE）で一番安い gpt-oss-20b を既定にする。
# 出力上限は Mac と同じ式（llm_models.max_completion_tokens）・推論を抑える指定も同じ表（extra_parameters）を使う。
DEFAULT_GROQ_MODEL = "openai/gpt-oss-20b"
DEFAULT_GEMINI_MODEL = llm_models.RECOMMENDED_GEMINI

# 字幕の大きさ倍率の範囲（70〜200%）
MIN_SCALE = 0.7
MAX_SCALE = 2.0

# settings.yaml の既定（constants.DEFAULT_CONFIG からも参照する）
DEFAULT_CAPTION: Dict[str, Any] = {
    "mode": MODE_TRANSLATE,
    "language": LANG_JA,
    "engine": ENGINE_GROQ,
    "recognizer": RECOGNIZER_SONIOX,
    "groq_model": DEFAULT_GROQ_MODEL,
    "gemini_model": DEFAULT_GEMINI_MODEL,
    "save_transcript": True,
    "show_source": True,
    "hud_scale": 1.0,
    "scope": SCOPE_FRONTMOST,
    "auto_start": False,
    "ever_started": False,
}

DEFAULT_MEET_BOT: Dict[str, Any] = {
    "source": MEET_SOURCE_CAPTIONS,
    # 空なら字幕の「認識する言語」に合わせる（Mac の meetBotLanguage と同じ）
    "language": "",
    "recognizer": RECOGNIZER_SONIOX,
    # ログイン状態の記録（次に起動したときも設定画面に出せるように。値は名前と期限から判定した結果だけ）
    "logged_in": False,
    "login_checked_at": "",
}


def _section(config, key: str) -> Dict[str, Any]:
    """config（ConfigManager / dict）から辞書を 1 つ取り出す（壊れていれば空）。"""
    getter = getattr(config, "get", None)
    value = getter(key, {}) if callable(getter) else None
    return value if isinstance(value, dict) else {}


class CaptionConfig:
    """字幕の設定を読む窓口（不正値は既定へ矯正して返す）。

    Attributes:
        raw: settings.yaml の `caption:` 辞書（読み取り専用として扱う）
    """

    def __init__(self, raw: Optional[Dict[str, Any]] = None) -> None:
        self.raw = dict(raw or {})

    @classmethod
    def from_config(cls, config) -> "CaptionConfig":
        """ConfigManager か dict から作る。"""
        return cls(_section(config, "caption"))

    def _get(self, key: str):
        value = self.raw.get(key)
        return DEFAULT_CAPTION[key] if value is None else value

    @property
    def mode(self) -> str:
        value = str(self._get("mode"))
        return value if value in MODE_LABELS else MODE_TRANSLATE

    @property
    def language(self) -> str:
        """文字起こしモードで認識する言語（既定 日本語）。"""
        value = str(self._get("language"))
        return value if value in LANGUAGE_LABELS else LANG_JA

    @property
    def recognition_language(self) -> str:
        """実際に認識する言語。翻訳は英→日の一方向なので英語で固定（Mac と同じ）。"""
        return LANG_EN if self.mode == MODE_TRANSLATE else self.language

    @property
    def recognition_language_name(self) -> str:
        """議事録のヘッダ・状態表示に出す言語名。"""
        return LANGUAGE_LABELS[self.recognition_language]

    @property
    def engine(self) -> str:
        """翻訳エンジン（既定 Groq）。Claude が勝手に切り替えない＝保存値をそのまま使う。"""
        value = str(self._get("engine"))
        return value if value in ENGINES else ENGINE_GROQ

    @property
    def recognizer(self) -> str:
        """Groq / Gemini で訳すとき・文字起こしモードの認識エンジン（既定 Soniox）。"""
        value = str(self._get("recognizer"))
        return value if value in RECOGNIZERS else RECOGNIZER_SONIOX

    @property
    def uses_soniox_translation(self) -> bool:
        """Soniox に認識と翻訳をまとめて任せるか（翻訳モードで翻訳エンジンが Soniox）。"""
        return self.mode == MODE_TRANSLATE and self.engine == ENGINE_SONIOX

    @property
    def effective_recognizer(self) -> str:
        """実際に音声を流す先（Soniox 翻訳なら Soniox 固定）。"""
        return RECOGNIZER_SONIOX if self.uses_soniox_translation else self.recognizer

    @property
    def translator(self) -> Optional[str]:
        """確定文を訳す翻訳器（"groq" / "gemini"）。訳さない・Soniox が訳すときは None。"""
        if self.mode != MODE_TRANSLATE or self.engine == ENGINE_SONIOX:
            return None
        return self.engine

    @property
    def groq_model(self) -> str:
        value = str(self.raw.get("groq_model") or "").strip()
        return value or DEFAULT_GROQ_MODEL

    @property
    def gemini_model(self) -> str:
        value = str(self.raw.get("gemini_model") or "").strip()
        return value or DEFAULT_GEMINI_MODEL

    @property
    def saves_transcript(self) -> bool:
        """字幕の文字起こしを保存するか（Meet ボットには効かない）。"""
        return bool(self._get("save_transcript"))

    @property
    def shows_source(self) -> bool:
        return bool(self._get("show_source"))

    @property
    def hud_scale(self) -> float:
        return clamp_scale(self._get("hud_scale"))

    @property
    def scope(self) -> str:
        value = str(self._get("scope"))
        return value if value in (SCOPE_FRONTMOST, SCOPE_ALL) else SCOPE_FRONTMOST

    @property
    def auto_start(self) -> bool:
        return bool(self._get("auto_start"))

    @property
    def ever_started(self) -> bool:
        return bool(self._get("ever_started"))

    @property
    def should_auto_start_now(self) -> bool:
        """起動時に自動開始してよいか（設定 ON かつ一度でも開始したことがある・Mac と同じ）。"""
        return self.auto_start and self.ever_started

    def session_key(self) -> tuple:
        """張り直しが要る設定の組（動作中にこれが変わったらセッションを作り直す）。"""
        return (self.mode, self.recognition_language, self.effective_recognizer,
                self.uses_soniox_translation, self.scope)


class MeetBotConfig:
    """Meet ボットの設定を読む窓口。"""

    def __init__(self, raw: Optional[Dict[str, Any]] = None, caption: Optional[CaptionConfig] = None) -> None:
        self.raw = dict(raw or {})
        self._caption = caption or CaptionConfig()

    @classmethod
    def from_config(cls, config) -> "MeetBotConfig":
        return cls(_section(config, "meet_bot"), CaptionConfig.from_config(config))

    @property
    def source(self) -> str:
        value = str(self.raw.get("source") or MEET_SOURCE_CAPTIONS)
        return value if value in MEET_SOURCE_LABELS else MEET_SOURCE_CAPTIONS

    @property
    def language(self) -> str:
        """会議の言語（未設定なら字幕の「認識する言語」に合わせる）。"""
        value = str(self.raw.get("language") or "")
        return value if value in LANGUAGE_LABELS else self._caption.language

    @property
    def recognizer(self) -> str:
        value = str(self.raw.get("recognizer") or RECOGNIZER_SONIOX)
        return value if value in RECOGNIZERS else RECOGNIZER_SONIOX

    @property
    def logged_in(self) -> bool:
        return bool(self.raw.get("logged_in", False))

    @property
    def login_checked_at(self) -> str:
        return str(self.raw.get("login_checked_at") or "")


def clamp_scale(value: Any) -> float:
    """字幕の大きさ倍率を 0.7〜2.0 に収める（壊れた値は 1.0）。"""
    try:
        scale = float(value)
    except (TypeError, ValueError):
        return 1.0
    if scale != scale:  # NaN
        return 1.0
    return max(MIN_SCALE, min(MAX_SCALE, scale))


def engine_title(engine: str, model: str = "") -> str:
    """閉じたポップアップに出す翻訳エンジンの名前（提供元 · モデル）。"""
    if engine == ENGINE_SONIOX:
        return "Soniox（リアルタイム）"
    provider = llm_models.GEMINI if engine == ENGINE_GEMINI else llm_models.GROQ
    name = next((info.name for info in llm_models.known(provider) if info.id == model), model)
    return f"{'Gemini' if engine == ENGINE_GEMINI else 'Groq'} · {name}"
