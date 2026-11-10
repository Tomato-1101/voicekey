"""
字幕の翻訳器（Groq / Gemini）— Mac 版 GroqTranslator.swift / GeminiTranslator.swift の移植

クラウド共通ポリシー（恒久要件）:
  - 部分訳では呼ばない（確定文のみ・TranslationCoordinator がまとめた区切りだけ）。高頻度呼び出しをしない。
  - Gemini はユーザーが選んだときだけ使う（Claude が勝手に課金のあるエンジンへ切り替えない）。
  - 失敗したら例外を投げる（呼び出し側は原文のまま字幕に出す）。途中で切れた訳は訳として出さない。
  - API キー・原文・訳文はログに出さない（文字数・HTTP ステータスだけ）。

Groq は OpenAI 互換の chat/completions を SSE ストリーミングで受け、訳が届く途中から字幕のライブ行へ流す。
使ったトークン数は API 使用量（用途「字幕の翻訳」）に記録する。
"""

import json
import threading
from typing import Any, Callable, Iterable, List, Optional

import httpx

from ..config import llm_models
from ..utils import secrets
from ..utils.logger import get_logger
from .translation_coordinator import TranslationExchange

logger = get_logger(__name__)

# API 使用量の用途（Mac の ApiUsagePurpose.captionTranslation と同じ文字列）
PURPOSE_CAPTION_TRANSLATION = "captionTranslation"

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

# 字幕が数十秒固まるより、早く諦めて原文表示に落ちる方がよい（Mac と同じ 20 秒）
_TIMEOUT = httpx.Timeout(20.0, connect=5.0)

# 字幕の英→日の指示（Mac の GeminiTranslator.systemPrompt と同じ文面。Groq も同じものを使う）
SYSTEM_PROMPT = (
    "あなたはライブ配信音声の同時字幕翻訳者です。入力は英語音声の音声認識テキストで、"
    "誤認識・句読点の欠落・語の欠落が含まれることがあります。前後の文脈から自然に補い、"
    "話し言葉として自然な日本語字幕に翻訳してください。\n"
    "出力は日本語訳のみ。前置き・注釈・原文の再掲・引用符は付けないでください。\n"
    "固有名詞や専門用語は無理に訳さず、一般的な表記があればそれを使ってください。"
)


class TranslationError(Exception):
    """翻訳の失敗（user_message は画面に出してよい文言・キーや本文は含まない）。

    Attributes:
        kind: missing_key / unauthorized / rate_limited / network / bad_request / server / bad_response
        user_message: 画面に出す説明
    """

    def __init__(self, kind: str, user_message: str) -> None:
        super().__init__(user_message)
        self.kind = kind
        self.user_message = user_message


def error_for_status(provider: str, status: int) -> TranslationError:
    """HTTP ステータスを翻訳エラーへ写す（Mac の mapError と同じ分け方）。"""
    if status in (401, 403):
        return TranslationError("unauthorized", f"{provider} の API キーが無効です")
    if status == 402:
        return TranslationError("unauthorized", f"{provider} の残高・利用上限が尽きています")
    if status == 429:
        return TranslationError("rate_limited", f"{provider} の利用上限（レート制限）に達しました")
    if 400 <= status < 500:
        return TranslationError("bad_request", f"{provider} がリクエストを受け付けませんでした（HTTP {status}）")
    return TranslationError("server", f"{provider} のサーバーエラー（HTTP {status}）")


# ----------------------------------------------------------------------
# Groq（OpenAI 互換・SSE ストリーミング）
# ----------------------------------------------------------------------

def groq_request_body(model: str, text: str, context: List[TranslationExchange], stream: bool = True) -> dict:
    """chat/completions の本文（純関数・テスト対象）。文脈は user / assistant の交互で渡す。"""
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for exchange in context:
        messages.append({"role": "user", "content": exchange.source})
        messages.append({"role": "assistant", "content": exchange.translated})
    messages.append({"role": "user", "content": text})
    body = {
        "model": model,
        "messages": messages,
        # 固定 512 だと推論モデル（gpt-oss 等）は思考で使い切って途中で切れるので、音声入力と同じ式で確保する
        "max_completion_tokens": llm_models.max_completion_tokens(len(text)),
    }
    # 推論モデルの思考を抑える等、モデル別の指定を足す（整形・翻訳とも同じ表を使う）
    body.update(llm_models.extra_parameters(model))
    if stream:
        body["stream"] = True
        # 最終チャンクにトークン数（usage）を載せてもらう＝ API 使用量の記録に使う
        body["stream_options"] = {"include_usage": True}
    return body


def _sse_payload(line: str) -> Optional[dict]:
    """SSE の 1 行から JSON を取り出す（`data: [DONE]`・空行・壊れた行は None）。"""
    if not line.startswith("data:"):
        return None
    payload = line[len("data:"):].strip()
    if not payload or payload == "[DONE]":
        return None
    try:
        root = json.loads(payload)
    except Exception:
        return None
    return root if isinstance(root, dict) else None


def sse_delta_text(line: str) -> Optional[str]:
    """SSE の 1 行から差分テキスト（choices[0].delta.content）を取り出す。"""
    root = _sse_payload(line)
    if root is None:
        return None
    choices = root.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return None
    delta = choices[0].get("delta")
    if not isinstance(delta, dict):
        return None
    content = delta.get("content")
    return content if isinstance(content, str) else None


def sse_finish_reason(line: str) -> Optional[str]:
    """SSE の 1 行から choices[0].finish_reason を取り出す（無い行は None）。"""
    root = _sse_payload(line)
    if root is None:
        return None
    choices = root.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return None
    reason = choices[0].get("finish_reason")
    return reason if isinstance(reason, str) else None


def completion_error(finish_reason: Optional[str], interrupted: bool) -> Optional[TranslationError]:
    """ストリームの終わり方から、訳が最後まで届いたかを判定する（届いていれば None）。"""
    if interrupted:
        return TranslationError("bad_response", "訳が途中で切れました（通信の切断）")
    if finish_reason == "stop":
        return None
    if finish_reason is not None:
        # "length" は出力上限で打ち切られた＝文の後半が無い
        return TranslationError("bad_response", f"訳が途中で切れました（finish_reason={finish_reason}）")
    return TranslationError("bad_response", "訳が途中で切れました（finish_reason なし）")


def parse_groq_stream(lines: Iterable[str], on_delta: Optional[Callable[[str], None]] = None,
                      on_usage: Optional[Callable[[dict], None]] = None) -> str:
    """SSE の行の並びから訳文を組み立てる（純ロジック・テスト対象）。

    Args:
        lines: SSE の行
        on_delta: 届いた分までの訳文（累積）を渡す
        on_usage: usage を含む最終チャンクの JSON を 1 回だけ渡す

    Raises:
        TranslationError: 途中で切れた・空だった
    """
    accumulated = ""
    finish_reason: Optional[str] = None
    usage_seen = False
    interrupted = False
    try:
        for line in lines:
            if not usage_seen and '"usage"' in line:
                root = _sse_payload(line)
                if root is not None and (root.get("usage") or (root.get("x_groq") or {}).get("usage")):
                    usage_seen = True
                    if on_usage is not None:
                        on_usage(root)
            reason = sse_finish_reason(line)
            if reason is not None:
                finish_reason = reason
            piece = sse_delta_text(line)
            if not piece:
                continue
            accumulated += piece
            if on_delta is not None:
                on_delta(accumulated)
    except httpx.HTTPError:
        if not accumulated:
            raise TranslationError("network", "Groq に接続できませんでした")
        interrupted = True
    error = completion_error(finish_reason, interrupted)
    if error is not None:
        raise error
    result = accumulated.strip()
    if not result:
        raise TranslationError("bad_response", "訳文が空でした")
    return result


class GroqCaptionTranslator:
    """Groq の字幕翻訳器（呼び出し可能オブジェクト。TranslationCoordinator の Translate 型）。"""

    def __init__(
        self,
        model_provider: Callable[[], str],
        key_provider: Callable[[], Optional[str]] = lambda: secrets.api_key("groq"),
        on_model_replaced: Optional[Callable[[str], None]] = None,
        client_factory: Callable[[], httpx.Client] = lambda: httpx.Client(timeout=_TIMEOUT),
    ) -> None:
        """
        Args:
            model_provider: モデル ID を毎回返す（設定変更を再起動なしで反映するため）
            key_provider: API キーを返す（未設定なら None）
            on_model_replaced: モデルが提供終了で差し替わったら新しいモデル ID を渡す（設定の保存は呼び出し側）
            client_factory: HTTP クライアントの作り方（テストで模擬サーバーへ向ける）
        """
        self._model_provider = model_provider
        self._key_provider = key_provider
        self._on_model_replaced = on_model_replaced
        self._client = client_factory()
        self._lock = threading.Lock()
        self.url = GROQ_URL

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass

    def __call__(self, text: str, context: List[TranslationExchange],
                 on_delta: Optional[Callable[[str], None]] = None) -> str:
        trimmed = (text or "").strip()
        if not trimmed:
            return ""
        key = self._key_provider()
        if not key:
            raise TranslationError("missing_key", "Groq の API キーが未設定です（設定 › API キー）")
        model = self._model_provider()
        try:
            return self._stream(model, key, trimmed, context, on_delta)
        except _ModelUnavailable:
            replacement = replacement_model(model)
            if replacement is None:
                raise TranslationError("bad_request", f"モデル {model} は使えません")
            logger.info(f"[モデル] 字幕の翻訳: {model} は提供終了のため {replacement} へ切り替え（自動再試行）")
            if self._on_model_replaced is not None:
                try:
                    self._on_model_replaced(replacement)
                except Exception:
                    pass
            try:
                return self._stream(replacement, key, trimmed, context, on_delta)
            except _ModelUnavailable:
                raise TranslationError("bad_request", f"モデル {replacement} は使えません")

    def _stream(self, model: str, key: str, text: str, context: List[TranslationExchange],
                on_delta: Optional[Callable[[str], None]]) -> str:
        body = groq_request_body(model, text, context, stream=True)
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

        def record(root: dict) -> None:
            try:
                from ..core import api_usage
                api_usage.record_chat_usage(root, "groq", model, PURPOSE_CAPTION_TRANSLATION)
            except Exception:
                pass

        try:
            with self._client.stream("POST", self.url, headers=headers, json=body) as resp:
                if resp.status_code != 200:
                    data = resp.read()
                    if llm_models.is_model_unavailable(resp.status_code, data):
                        raise _ModelUnavailable()
                    raise error_for_status("Groq", resp.status_code)
                return parse_groq_stream(resp.iter_lines(), on_delta, record)
        except (TranslationError, _ModelUnavailable):
            raise
        except httpx.HTTPError:
            raise TranslationError("network", "Groq に接続できませんでした")


class _ModelUnavailable(Exception):
    """モデルが提供終了／存在しないことによる失敗（差し替えて 1 回だけ取り直す）。"""


def replacement_model(failed_model: str) -> Optional[str]:
    """字幕の Groq モデルが使えなかったときの差し替え先（差し替えないなら None）。"""
    for candidate in ("openai/gpt-oss-20b", llm_models.RECOMMENDED_GROQ):
        if candidate != failed_model:
            return candidate
    return None


# ----------------------------------------------------------------------
# Gemini（generateContent・非ストリーミング）
# ----------------------------------------------------------------------

def gemini_request_body(text: str, context: List[TranslationExchange]) -> dict:
    """generateContent の本文（純関数）。直近の確定ペアを user / model 交互で並べ、最後に原文。"""
    contents = []
    for exchange in context:
        contents.append({"role": "user", "parts": [{"text": exchange.source}]})
        contents.append({"role": "model", "parts": [{"text": exchange.translated}]})
    contents.append({"role": "user", "parts": [{"text": text}]})
    return {
        "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": contents,
        "generationConfig": {"maxOutputTokens": llm_models.max_completion_tokens(len(text))},
    }


def gemini_text(payload: Any) -> str:
    """Gemini の応答から訳文を取り出す。途中で切れた訳（MAX_TOKENS）・形の違う応答は TranslationError。"""
    try:
        candidate = payload["candidates"][0]
    except Exception:
        raise TranslationError("bad_response", "Gemini の応答の形が違います")
    if candidate.get("finishReason") == "MAX_TOKENS":
        raise TranslationError("bad_response", "訳が途中で切れました（出力上限）")
    parts = (candidate.get("content") or {}).get("parts") or []
    text = "".join(str(p.get("text", "")) for p in parts if isinstance(p, dict) and not p.get("thought")).strip()
    if not text:
        raise TranslationError("bad_response", "訳文が空でした")
    return text


class GeminiCaptionTranslator:
    """Gemini の字幕翻訳器（ユーザーが選んだときだけ使う）。"""

    def __init__(
        self,
        model_provider: Callable[[], str],
        key_provider: Callable[[], Optional[str]] = lambda: secrets.api_key("gemini"),
        client_factory: Callable[[], httpx.Client] = lambda: httpx.Client(timeout=_TIMEOUT),
    ) -> None:
        self._model_provider = model_provider
        self._key_provider = key_provider
        self._client = client_factory()
        self.url_template = GEMINI_URL

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass

    def __call__(self, text: str, context: List[TranslationExchange],
                 on_delta: Optional[Callable[[str], None]] = None) -> str:
        trimmed = (text or "").strip()
        if not trimmed:
            return ""
        key = self._key_provider()
        if not key:
            raise TranslationError("missing_key", "Gemini の API キーが未設定です（設定 › API キー）")
        model = self._model_provider()
        try:
            # キーは URL に載せずヘッダーで渡す（各種ログに残さない）
            resp = self._client.post(
                self.url_template.format(model=model),
                headers={"x-goog-api-key": key, "Content-Type": "application/json"},
                json=gemini_request_body(trimmed, context),
            )
        except httpx.HTTPError:
            raise TranslationError("network", "Gemini に接続できませんでした")
        if resp.status_code != 200:
            raise error_for_status("Gemini", resp.status_code)
        try:
            payload = resp.json()
        except Exception:
            raise TranslationError("bad_response", "Gemini の応答を読めませんでした")
        try:
            from ..core import api_usage
            api_usage.record_gemini_usage(payload, model, PURPOSE_CAPTION_TRANSLATION)
        except Exception:
            pass
        return gemini_text(payload)
