"""
整形に使う LLM モデルのカタログ（Groq）

選べるモデル・表示名・注記と、モデルごとに要るリクエストの追加パラメータをまとめる。
中身は Mac 版 `LLMModelCatalog.swift` と同じ（表示名と注記は Mac の文言のまま）。
"""

from dataclasses import dataclass
from typing import Any, Dict, Tuple


@dataclass(frozen=True)
class LLMModel:
    """
    整形モデル 1 つ分の定義。

    Attributes:
        id: Groq に渡すモデル名
        name: 表示名
        note: 選ぶときの目安（速さ・向き不向き）
        recommended: おすすめ（既定）か
    """

    id: str
    name: str
    note: str
    recommended: bool = False


# 整形の既定モデル
RECOMMENDED_MODEL: str = "qwen/qwen3.8-27b"

MODELS: Tuple[LLMModel, ...] = (
    LLMModel(RECOMMENDED_MODEL, "Qwen3.8 27B", "最速（約 220ms）で質も良好", recommended=True),
    LLMModel("openai/gpt-oss-120b", "gpt-oss 120B", "最も自然な訳・少し遅い（約 400ms）"),
    LLMModel("openai/gpt-oss-20b", "gpt-oss 20B", "速い（約 280〜460ms）"),
    LLMModel("allam-2-7b", "ALLaM 2 7B", "アラビア語向け。日本語は崩れやすい"),
)

# 1 回の応答で受け取る出力トークンの上限
_MAX_COMPLETION_TOKENS_CAP: int = 8192


def is_retired(model: str) -> bool:
    """
    Groq での提供が終わったモデルかを返す。

    残したままだと整形が毎回失敗して原文のまま貼られ続けるので、設定の読み込み時に
    おすすめへ読み替える判定に使う（Mac 版 isKnownRetired と同じ条件）。

    Args:
        model: モデル名

    Returns:
        提供が終わっていれば True
    """
    lower = model.lower()
    return (
        lower.startswith("llama")
        or lower.startswith("meta-llama/")
        or lower == "moonshotai/kimi-k2-instruct"
    )


def normalize_model(model: Any) -> str:
    """
    保存されていた整形モデル名を、いま使える名前に読み替える。

    空と提供の終わったモデルはおすすめへ寄せ、それ以外（利用者が選んだ別のモデル）は残す。

    Args:
        model: 保存されていたモデル名

    Returns:
        使うモデル名
    """
    text = str(model or "").strip()
    if not text or is_retired(text):
        return RECOMMENDED_MODEL
    return text


def extra_parameters(model: str) -> Dict[str, Any]:
    """
    リクエストに足すモデル別のパラメータを返す。

    推論モデルは既定のままだと考える過程のトークンを出して遅く・高くなる（整形には要らない）。
    gpt-oss は "none" を受け付けないので "low" にして考える過程を応答に含めず、qwen3 は考える動作そのものを切る。

    Args:
        model: モデル名

    Returns:
        リクエスト本体へ混ぜる辞書（要らないモデルは空）
    """
    lower = model.lower()
    if lower.startswith("openai/gpt-oss"):
        return {"reasoning_effort": "low", "include_reasoning": False}
    if lower.startswith("qwen/qwen3"):
        return {"reasoning_effort": "none"}
    return {}


def max_completion_tokens(input_chars: int) -> int:
    """
    入力の長さから出力トークンの上限を決める。

    上限を付けないとモデルが暴走したときに待ち時間と料金が膨らむ。整形は入力とほぼ同じ長さしか
    返さないので、文字数の 3 倍に余裕 512 を足した値で足りる。

    Args:
        input_chars: 入力の文字数

    Returns:
        max_completion_tokens に渡す値
    """
    return min(max(0, input_chars) * 3 + 512, _MAX_COMPLETION_TOKENS_CAP)
