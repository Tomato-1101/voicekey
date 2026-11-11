"""
API 単価表と料金計算モジュール（API 利用料金の見える化の単一ソース）

単価はここ 1 ファイルにだけ置く。記録側（api_usage）は数量だけを保存し、
料金は表示のたびにこの表から計算する＝単価を直せば過去の使用量にもそのまま反映される。
公式の料金ページで確認できなかった単価は推測で埋めず None（＝単価未確認）にする。
金額はすべて米ドル。Mac 版 Core/ApiPricing.swift と表・計算式を一致させること。
"""

from dataclasses import dataclass
from typing import Optional

# 単価を公式ページで確認した日（画面の注記に出す）。Mac 版 ApiPricing.checkedOn と同じ
CHECKED_ON = "2026-10-02"

# 課金元のプロバイダー（記録と表示のキー）→ 画面に出す表示名（実プロバイダー名）
PROVIDER_LABELS = {
    "soniox": "Soniox",
    "openai": "OpenAI",
    "microsoft": "Microsoft",
    "elevenlabs": "ElevenLabs",
    "groq": "Groq",
    "gemini": "Gemini",
    "deepgram": "Deepgram",
    "apple": "Apple",
}

# API を何のために呼んだか（内訳の表示用）→ 表示名
PURPOSE_LABELS = {
    "transcription": "文字起こし",
    "formatting": "テキスト整形",
    "translation": "翻訳して入力",
    "captionTranslation": "字幕の翻訳",
    "meetingSummary": "議事録の要約",
}

# 文字起こしエンジン id（Mac の Backend の rawValue と同じ文字列）→ 課金元。
# openai_live も請求は OpenAI。apple_local は Windows に無いが、受けたら無料として扱う
_ENGINE_PROVIDERS = {
    "openai": "openai",
    "openai_live": "openai",
    "groq": "groq",
    "elevenlabs": "elevenlabs",
    "deepgram": "deepgram",
    "apple_local": "apple",
    "soniox": "soniox",
    "azure_mai": "microsoft",
}

# 本人が実際にお金を払っている契約のあるプロバイダー（「実際に払った分」表示の対象）。
# Mac 版 ApiPricing.paidProviders と同じ。契約が変わったらここ 1 か所だけ直す
PAID_PROVIDERS = frozenset({"openai", "soniox", "microsoft", "gemini"})

# 料金の表示モード
MODE_LIST = "list"  # 定価（無料枠で使った分も単価どおりに計上）
MODE_PAID = "paid"  # 実際に払った分（有料契約のプロバイダーだけ計上。それ以外は 0 ドル）


@dataclass(frozen=True)
class Price:
    """
    1 モデルぶんの単価。

    Attributes:
        usd_per_audio_hour: 音声の時間課金（USD / 時間）。トークン課金なら None
        minimum_billed_seconds: 1 リクエストあたりの最低課金秒数
        input_per_million: LLM 入力の単価（USD / 100 万トークン）
        output_per_million: LLM 出力の単価（USD / 100 万トークン）
    """

    usd_per_audio_hour: Optional[float] = None
    minimum_billed_seconds: float = 0.0
    input_per_million: float = 0.0
    output_per_million: float = 0.0


# 無料（Apple のオンデバイス認識・翻訳）
FREE = Price()


def _audio(usd_per_hour: float, minimum_billed_seconds: float = 0.0) -> Price:
    return Price(usd_per_audio_hour=usd_per_hour, minimum_billed_seconds=minimum_billed_seconds)


def _tokens(input_usd: float, output_usd: float) -> Price:
    return Price(input_per_million=input_usd, output_per_million=output_usd)


# 単価表。キーは「プロバイダー/モデル ID（小文字）」。ここに無いモデルは単価未確認（None）。
# 出典と確認日は Mac 版 ApiPricing.swift の各行コメントと同じ。値を推測で足さないこと。
_TABLE = {
    # Soniox リアルタイム $0.12/時間（https://soniox.com/pricing）
    "soniox/stt-rt-v5": _audio(0.12),
    # OpenAI gpt-transcribe $0.0045/分（https://developers.openai.com/api/docs/pricing）
    "openai/gpt-transcribe": _audio(0.0045 * 60),
    # OpenAI gpt-live-transcribe（Realtime）$0.017/分
    "openai/gpt-live-transcribe": _audio(0.017 * 60),
    # Microsoft MAI-Transcribe-2 $0.10/時間（2026 年末までの期間限定価格）
    "microsoft/mai-transcribe-2": _audio(0.10),
    # ElevenLabs Scribe v2 $0.22/時間（scribe_v1 は料金ページに無いので未確認のまま）
    "elevenlabs/scribe_v2": _audio(0.22),
    # Groq whisper-large-v3-turbo $0.04/時間・1 リクエスト最低 10 秒課金
    "groq/whisper-large-v3-turbo": _audio(0.04, minimum_billed_seconds=10),
    # Groq LLM（llama 系は 2026-10 に API から廃止＝表に載せない）
    "groq/openai/gpt-oss-20b": _tokens(0.075, 0.30),
    "groq/openai/gpt-oss-120b": _tokens(0.15, 0.60),
    "groq/qwen/qwen3.8-27b": _tokens(0.80, 4.00),
    # Gemini 3.5 Flash-Lite 有料枠（出力は思考トークン込み）。無料枠かはアプリから分からないので有料枠で見積もる
    "gemini/gemini-3.5-flash-lite": _tokens(0.30, 2.50),
}


def provider_for_engine(engine_id: str) -> Optional[str]:
    """文字起こしエンジン id（Mac の Backend rawValue）から課金元を引く。未知の id は None。"""
    return _ENGINE_PROVIDERS.get(engine_id)


def price(provider: Optional[str], model: str) -> Optional[Price]:
    """単価を引く。None = 単価未確認（画面には「単価未確認」と出す）。Apple は常に無料。"""
    if provider == "apple":
        return FREE
    if not provider:
        return None
    return _TABLE.get(f"{provider}/{(model or '').lower()}")


def billed_seconds(provider: Optional[str], model: str, audio_seconds: float) -> float:
    """1 リクエストぶんの課金対象秒数（最低課金秒数を適用）。単価未確認なら実秒数のまま。"""
    seconds = max(0.0, float(audio_seconds))
    p = price(provider, model)
    if seconds <= 0 or p is None or p.usd_per_audio_hour is None:
        return seconds
    return max(seconds, p.minimum_billed_seconds)


def cost(
    provider: Optional[str],
    model: str,
    billed_audio_seconds: float = 0.0,
    input_tokens: int = 0,
    output_tokens: int = 0,
) -> Optional[float]:
    """
    数量から料金（USD）を計算する。

    Args:
        provider: 課金元（PROVIDER_LABELS のキー）
        model: モデル ID
        billed_audio_seconds: 課金対象秒数（billed_seconds 適用済み）
        input_tokens: LLM の入力トークン数
        output_tokens: LLM の出力トークン数

    Returns:
        料金（USD）。単価未確認なら None
    """
    p = price(provider, model)
    if p is None:
        return None
    if p.usd_per_audio_hour is not None:
        return billed_audio_seconds / 3600 * p.usd_per_audio_hour
    return input_tokens / 1_000_000 * p.input_per_million + output_tokens / 1_000_000 * p.output_per_million


def is_free_tier(mode: str, provider: Optional[str]) -> bool:
    """このモードで無料枠として扱う（0 ドルに落とす）プロバイダーか。定価モードでは常に False。"""
    if mode != MODE_PAID or not provider:
        return False
    return provider not in PAID_PROVIDERS


def format_usd(usd: float) -> str:
    """「$0.12」形式の表示文字列。1 セント未満は桁を増やして 0 に潰さない。"""
    if 0 < usd < 0.01:
        return f"${usd:.4f}"
    return f"${usd:.2f}"
