"""
LLM（Groq / Gemini）のモデル一覧・おすすめ・モデル別パラメータ・廃止モデルの判定（Mac 版 LLMModelCatalog.swift の移植）

2026-10 に Groq が llama 系を API から廃止し、Mac では整形・翻訳が毎回失敗していた。
Windows も既定の整形モデルが llama-3.1-8b-instant のままだったので、同じ表をここに集約し、
- 既知モデルの静的一覧（表示名・一言説明・おすすめ）
- 推論モデルの思考を抑えるモデル別パラメータ
- 入力の長さに応じた出力上限と、途中で切れた応答（finish_reason=length）の判定
- 「提供終了／存在しない」による失敗の判定と、おすすめへの差し替え
をまとめて持つ。通信はしない（一覧の API 取得は Mac だけが持つ。Windows は静的一覧で足りる）。
"""

import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

GROQ = "groq"
GEMINI = "gemini"

# Groq のおすすめ。2026-10-11 実測（翻訳・無料枠・3 回中央値）で最速、質も良好（Mac と同じ）
RECOMMENDED_GROQ = "qwen/qwen3.8-27b"
# Gemini のおすすめ。無料枠の対象・速い（Mac の字幕の既定と同じ）
RECOMMENDED_GEMINI = "gemini-3.5-flash-lite"


@dataclass(frozen=True)
class LLMModelInfo:
    """既知モデル 1 件（設定画面の表示用）。"""

    id: str
    name: str
    note: str
    recommended: bool = False


# Groq の既知モデル（先頭がおすすめ）。速度は 2026-10-11 の翻訳実測（Mac と同じ表）
GROQ_MODELS: Tuple[LLMModelInfo, ...] = (
    LLMModelInfo(RECOMMENDED_GROQ, "Qwen3.8 27B", "最速（約 220ms）で質も良好", True),
    LLMModelInfo("openai/gpt-oss-120b", "gpt-oss 120B", "最も自然な訳・少し遅い（約 400ms）"),
    LLMModelInfo("openai/gpt-oss-20b", "gpt-oss 20B", "速い（約 280〜460ms）"),
    LLMModelInfo("allam-2-7b", "ALLaM 2 7B", "アラビア語向け。日本語は崩れやすい"),
)

# Gemini の既知モデル（先頭がおすすめ。generateContent 対応の flash 系）
GEMINI_MODELS: Tuple[LLMModelInfo, ...] = (
    LLMModelInfo(RECOMMENDED_GEMINI, "Gemini 3.5 Flash-Lite", "無料枠の対象・速い", True),
    LLMModelInfo("gemini-3.8-flash", "Gemini 3.8 Flash", "最新世代の Flash"),
    LLMModelInfo("gemini-3.7-flash", "Gemini 3.7 Flash", "Flash"),
    LLMModelInfo("gemini-3.6-flash", "Gemini 3.6 Flash", "Flash"),
    LLMModelInfo("gemini-3.5-flash", "Gemini 3.5 Flash", "Flash"),
    LLMModelInfo("gemini-2.5-flash", "Gemini 2.5 Flash", "旧世代の Flash"),
    LLMModelInfo("gemini-2.5-flash-lite", "Gemini 2.5 Flash-Lite", "旧世代の Lite"),
)

# 出力トークン上限の天井（長い独り言でも 1 回の整形・翻訳で返し切れる量）
MAX_COMPLETION_TOKENS_CEILING = 8192


def recommended(provider: str) -> str:
    """プロバイダーのおすすめモデル。"""
    return RECOMMENDED_GROQ if provider == GROQ else RECOMMENDED_GEMINI


def known(provider: str) -> Tuple[LLMModelInfo, ...]:
    """プロバイダーの既知モデル一覧（先頭がおすすめ）。"""
    return GROQ_MODELS if provider == GROQ else GEMINI_MODELS


def is_known_retired(model_id: str, provider: str = GROQ) -> bool:
    """廃止と分かっている Groq モデルか（llama 系は 2026-10 に API から廃止。kimi-k2-instruct は以前から 404）。"""
    if provider != GROQ:
        return False
    lower = (model_id or "").strip().lower()
    return lower.startswith("llama") or lower.startswith("meta-llama/") or lower == "moonshotai/kimi-k2-instruct"


def migrated_model(stored: str, provider: str = GROQ) -> Optional[str]:
    """保存値を差し替えるべきなら差し替え先を返す（不要なら None）。

    Groq の廃止と分かっているモデルだけをおすすめへ戻す。Gemini は起動時には触らない
    （提供終了なら実行時の 404 で差し替わる）。Mac の migratedModel と同じ規則。
    """
    model_id = (stored or "").strip()
    if not model_id or provider != GROQ or model_id == recommended(provider):
        return None
    return recommended(provider) if is_known_retired(model_id, provider) else None


def extra_parameters(model: str) -> Dict[str, Any]:
    """リクエストに足すモデル別パラメータ（Groq の chat/completions 用）。

    推論モデルは既定のままだと思考トークンを吐いて遅く・高くなる（整形・翻訳には不要）。
    gpt-oss は "none" を受け付けないので "low"＋思考を応答に含めない、qwen3 は思考そのものを切る。
    """
    lower = (model or "").lower()
    if lower.startswith("openai/gpt-oss"):
        return {"reasoning_effort": "low", "include_reasoning": False}
    if lower.startswith("qwen/qwen3"):
        return {"reasoning_effort": "none"}
    return {}


def max_completion_tokens(input_characters: int) -> int:
    """入力の長さに応じた出力トークン上限（入力文字数 × 3 ＋ 思考ぶん 512、天井 8192）。

    固定 512 だと推論モデルが考えるだけで使い切り、長い文の整形・翻訳が途中で切れる。
    """
    return min(max(0, int(input_characters)) * 3 + 512, MAX_COMPLETION_TOKENS_CEILING)


def _json(data: Any) -> Optional[dict]:
    """bytes / str / dict を dict にする（壊れていれば None）。"""
    if isinstance(data, dict):
        return data
    try:
        if isinstance(data, (bytes, bytearray)):
            data = data.decode("utf-8", errors="replace")
        parsed = json.loads(data)
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def is_truncated(response_json: Any) -> bool:
    """OpenAI 互換の chat/completions 応答が上限で途中で切れたか（finish_reason == "length"）。

    切れた訳文・整形文を貼ると発話の後半が黙って消えるので、呼び出し側は原文を返す。
    """
    root = _json(response_json)
    if not root:
        return False
    choices = root.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return False
    return choices[0].get("finish_reason") == "length"


def is_model_unavailable(status: int, data: Any) -> bool:
    """「モデルが提供終了／存在しない」による失敗か。

    Groq は 400 model_decommissioned / 404 model_not_found、Gemini は 404 NOT_FOUND。
    それ以外（レート制限・キー不正・一般の 400）は差し替えても直らないので False。
    """
    if status not in (400, 404):
        return False
    root = _json(data)
    if not root:
        return False
    error = root.get("error")
    if not isinstance(error, dict):
        return False
    code = error.get("code")
    if isinstance(code, str) and code in ("model_decommissioned", "model_not_found"):
        return True
    if status == 404 and error.get("status") == "NOT_FOUND":
        return True
    message = str(error.get("message") or "").lower()
    return "decommissioned" in message


def options(provider: str, current: str) -> List[Tuple[str, str]]:
    """設定画面のモデル選択肢 (モデル ID, 表示名) の並び（おすすめ → 既知 → 一覧に無い保存値）。"""
    result: List[Tuple[str, str]] = []
    for info in known(provider):
        result.append((info.id, f"{info.name}（おすすめ）" if info.recommended else info.name))
    current = (current or "").strip()
    if current and all(model_id != current for model_id, _ in result):
        suffix = "（利用不可）" if is_known_retired(current, provider) else ""
        result.append((current, current + suffix))
    return result


def detail(provider: str, model_id: str) -> str:
    """選択中モデルの一言説明（設定画面で項目名の下に出す）。"""
    if is_known_retired(model_id, provider):
        return f"このモデルは提供終了です。おすすめ（{recommended(provider)}）に切り替えてください。"
    for info in known(provider):
        if info.id == model_id:
            return f"{info.note}（{info.id}）"
    return f"一覧に無いモデルです（{model_id}）"
