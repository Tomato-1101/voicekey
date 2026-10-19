"""
「翻訳して入力」— 文字起こしの最終テキストを訳してから貼り付ける（Mac 版 DictationTranslator.swift の Windows 版）

本命の用途は「日本語で話す → 英語が入力される」。どのエンジンの結果にも同じように効かせるため、
貼り付け直前の最終テキストに **1 回だけ** 適用する（部分訳はしない＝クラウドを高頻度に叩かない）。

Mac は Apple のオンデバイス翻訳（既定）と Groq の 2 択だが、Windows には Apple 翻訳が無いので
Groq（既定・無料枠あり）と Gemini（有料の場合あり）から選ぶ。Gemini は利用者が選んだときだけ使い、
勝手に切り替えない（課金を勝手に発生させない方針）。

失敗したら必ず原文を返す（テキストを失わないことが最優先）。途中で切れた訳（出力上限）は
訳として貼らずに失敗扱いにする。API キーと訳文・原文はログに出さない（文字数だけ）。
"""

import threading
from dataclasses import dataclass
from typing import Any, Callable, Optional

import httpx

from ..config import llm_models
from ..utils import secrets
from ..utils.logger import get_logger

logger = get_logger(__name__)

ENGINE_GROQ = "groq"
ENGINE_GEMINI = "gemini"

# 出力言語の選択肢（Mac の DictationTranslation.targetLanguages と同じ）
TARGET_LANGUAGES = [
    ("en", "英語"),
    ("zh-Hans", "中国語（簡体）"),
    ("ko", "韓国語"),
    ("es", "スペイン語"),
    ("ja", "日本語"),
]

_GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
_GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
# 待ちすぎない（落ちたら原文を入力する）
_TIMEOUT = httpx.Timeout(20.0, connect=5.0)

_client: Optional[httpx.Client] = None
_client_lock = threading.Lock()

# 翻訳のモデルが提供終了で差し替えられたときの通知先（app が設定の保存と HUD 通知を受け持つ）。
# 引数は (設定キー, 新モデル)
on_model_replaced: Optional[Callable[[str, str], None]] = None
_notified: set = set()


@dataclass
class TranslationResult:
    """翻訳結果（原文フォールバックしたかが呼び出し側で分かるようにする）。

    Attributes:
        text: 貼り付けるテキスト（失敗時は原文そのまま）
        did_translate: 実際に訳せたか
        failure_reason: 訳せなかった理由（ログ用。成功時は None。空入力は "empty"）
    """

    text: str
    did_translate: bool
    failure_reason: Optional[str] = None


def label_for(code: str) -> str:
    """出力言語コードの表示名。"""
    for c, label in TARGET_LANGUAGES:
        if c == code:
            return label
    return code


def target_language(config) -> str:
    """設定の出力言語（選択肢に無い値は英語）。"""
    stored = str(_get(config, "translate_input_target", "en") or "en")
    return stored if any(code == stored for code, _ in TARGET_LANGUAGES) else "en"


def engine(config) -> str:
    """設定の翻訳エンジン（不明な値は Groq）。"""
    value = str(_get(config, "translate_input_engine", ENGINE_GROQ) or ENGINE_GROQ)
    return value if value in (ENGINE_GROQ, ENGINE_GEMINI) else ENGINE_GROQ


def system_prompt(target_code: str) -> str:
    """音声入力向けのシステムプロンプト（訳文だけを返させる・Mac と同じ文面）。"""
    language = label_for(target_code)
    return (
        f"あなたは音声入力の翻訳エンジンです。入力された文（何語かは決まっていません）を{language}へ自然に翻訳し、"
        "訳文だけを出力してください。説明・注釈・引用符・前置きは一切付けないでください。"
        f"入力が既に{language}ならそのまま出力してください。"
    )


def groq_request_body(model: str, prompt: str, text: str) -> dict:
    """Groq 翻訳リクエストの本文（純関数・テスト対象）。出力上限は入力の長さに応じた値。"""
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": text},
        ],
        "max_completion_tokens": llm_models.max_completion_tokens(len(text)),
    }
    body.update(llm_models.extra_parameters(model))
    return body


def gemini_request_body(prompt: str, text: str) -> dict:
    """Gemini 翻訳リクエストの本文（純関数・テスト対象）。"""
    return {
        "systemInstruction": {"parts": [{"text": prompt}]},
        "contents": [{"role": "user", "parts": [{"text": text}]}],
        "generationConfig": {
            "maxOutputTokens": llm_models.max_completion_tokens(len(text)),
            "temperature": 0.2,
        },
    }


def groq_translated_text(payload: Any) -> str:
    """Groq の応答から訳文を取り出す。途中で切れた訳・形の違う応答は ValueError。"""
    if llm_models.is_truncated(payload):
        raise ValueError("出力が上限で途中で切れました")
    try:
        content = payload["choices"][0]["message"]["content"]
    except Exception:
        raise ValueError("応答の形が違います")
    return str(content or "").strip()


def gemini_translated_text(payload: Any) -> str:
    """Gemini の応答から訳文を取り出す。途中で切れた訳（MAX_TOKENS）・形の違う応答は ValueError。"""
    try:
        candidate = payload["candidates"][0]
    except Exception:
        raise ValueError("応答の形が違います")
    if candidate.get("finishReason") == "MAX_TOKENS":
        raise ValueError("出力が上限で途中で切れました")
    parts = (candidate.get("content") or {}).get("parts") or []
    # 思考の部分（thought=True）は訳文ではないので外す
    return "".join(str(p.get("text", "")) for p in parts if isinstance(p, dict) and not p.get("thought")).strip()


def translate(text: str, config) -> TranslationResult:
    """最終テキストを 1 回だけ訳す。失敗したら必ず原文を返す。

    Args:
        text: 文字起こし・整形・置換まで済んだ最終テキスト
        config: 設定（ConfigManager または dict。エンジン・出力言語・モデルを読む）
    """
    if not (text or "").strip():
        return TranslationResult(text, False, "empty")
    target = target_language(config)
    use_engine = engine(config)
    try:
        if use_engine == ENGINE_GEMINI:
            translated = _translate_with_gemini(text.strip(), target, config)
        else:
            translated = _translate_with_groq(text.strip(), target, config)
    except Exception as e:
        logger.warning(f"翻訳に失敗したため原文を入力します（{use_engine}）: {e}")
        return TranslationResult(text, False, str(e) or type(e).__name__)
    if not translated:
        return TranslationResult(text, False, "empty-result")
    logger.info(f"翻訳完了（{use_engine}・→{target}）: {len(text)} → {len(translated)} 文字")
    return TranslationResult(translated, True, None)


# ----------------------------------------------------------------------
# エンジン別
# ----------------------------------------------------------------------

def _get(config, key: str, default=None):
    getter = getattr(config, "get", None)
    if callable(getter):
        return getter(key, default)
    return default


def _get_client() -> httpx.Client:
    global _client
    with _client_lock:
        if _client is None:
            _client = httpx.Client(timeout=_TIMEOUT)
        return _client


def _replace_unavailable(setting_key: str, provider: str, failed_model: str) -> Optional[str]:
    """提供終了で失敗したモデルをおすすめへ差し替える（差し替え先。差し替えないなら None）。"""
    replacement = llm_models.recommended(provider)
    if failed_model == replacement:
        return None
    logger.info(f"[モデル] 翻訳して入力: {failed_model} は提供終了のため {replacement} へ切り替え（自動再試行）")
    marker = f"{setting_key}/{failed_model}"
    if marker not in _notified:
        _notified.add(marker)
        handler = on_model_replaced
        if handler is not None:
            try:
                handler(setting_key, replacement)
            except Exception:
                pass
    return replacement


def _record(provider: str, payload: Any, model: str) -> None:
    """トークン数を API 使用量へ記録する（数量だけ。失敗は無視）。"""
    try:
        from . import api_usage
        if provider == ENGINE_GEMINI:
            api_usage.record_gemini_usage(payload, model, api_usage.PURPOSE_TRANSLATION)
        else:
            api_usage.record_chat_usage(payload, "groq", model, api_usage.PURPOSE_TRANSLATION)
    except Exception:
        pass


def _json(resp) -> Any:
    try:
        return resp.json()
    except Exception:
        return None


def _translate_with_groq(text: str, target: str, config) -> str:
    """Groq（OpenAI 互換）の LLM 翻訳（非ストリーミング・入力長に応じた出力上限）。"""
    key = secrets.api_key("groq")
    if not key:
        raise ValueError("Groq の API キーが未設定です")
    model = str(_get(config, "translate_input_groq_model", "") or "").strip() or llm_models.RECOMMENDED_GROQ
    prompt = system_prompt(target)

    def send(use_model: str):
        return _get_client().post(
            _GROQ_URL,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=groq_request_body(use_model, prompt, text),
        )

    resp = send(model)
    # モデルの提供終了だけは、その 1 回をおすすめで取り直して設定も書き換える（Mac と同じ）
    if resp.status_code != 200 and llm_models.is_model_unavailable(resp.status_code, _json(resp)):
        replacement = _replace_unavailable("translate_input_groq_model", llm_models.GROQ, model)
        if replacement:
            model = replacement
            resp = send(model)
    if resp.status_code != 200:
        raise ValueError(f"Groq HTTP {resp.status_code}")
    payload = _json(resp)
    _record(ENGINE_GROQ, payload, model)
    return groq_translated_text(payload)


def _translate_with_gemini(text: str, target: str, config) -> str:
    """Gemini の翻訳（キーは URL に載せずヘッダーで渡す＝各種ログに残さない）。"""
    key = secrets.api_key("gemini")
    if not key:
        raise ValueError("Gemini の API キーが未設定です")
    model = str(_get(config, "translate_input_gemini_model", "") or "").strip() or llm_models.RECOMMENDED_GEMINI
    prompt = system_prompt(target)

    def send(use_model: str):
        return _get_client().post(
            _GEMINI_URL.format(model=use_model),
            headers={"x-goog-api-key": key, "Content-Type": "application/json"},
            json=gemini_request_body(prompt, text),
        )

    resp = send(model)
    if resp.status_code != 200 and llm_models.is_model_unavailable(resp.status_code, _json(resp)):
        replacement = _replace_unavailable("translate_input_gemini_model", llm_models.GEMINI, model)
        if replacement:
            model = replacement
            resp = send(model)
    if resp.status_code != 200:
        raise ValueError(f"Gemini HTTP {resp.status_code}")
    payload = _json(resp)
    _record(ENGINE_GEMINI, payload, model)
    return gemini_translated_text(payload)
