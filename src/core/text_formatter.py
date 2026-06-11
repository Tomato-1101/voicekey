"""
LLM テキスト整形モジュール

文字起こし確定テキストを貼り付ける直前に、Groq の高速 LLM
（Chat Completions API、OpenAI 互換）で 1 回だけ整形する。

絶対条件（発話を失わない）:
- 入力テキストが空白のみなら API を呼ばずそのまま返す
- API キー未設定・タイムアウト・HTTP 非 200・JSON 構造不正・応答が空・
  あらゆる例外時は警告ログを出して原文をそのまま返す。例外を呼び出し元へ投げない
"""

import os
import time

import httpx

from ..utils import secrets
from ..utils.logger import get_logger

logger = get_logger(__name__)

# Groq Chat Completions エンドポイント（OpenAI 互換）
_API_URL = "https://api.groq.com/openai/v1/chat/completions"

# API リクエストのタイムアウト（秒）。音声入力の待ち時間を無制限に伸ばさない
_TIMEOUT_SEC = 10.0

# 整形モデルの既定値（グローバル設定 format_model で変更可能）
DEFAULT_FORMAT_MODEL = "llama-3.1-8b-instant"

# 全モード共通フッター。出力形式の逸脱（前置き・引用符等）を防ぐため、
# すべてのシステムプロンプト末尾に空行を挟んで必ず連結する
_COMMON_FOOTER = "出力は整形後のテキストのみを返す。前置き・説明・引用符・コードブロックを付けない。入力と同じ言語で出力する。元の発言にない情報を追加しない。"

# 整形モード識別子 → システムプロンプト本文（custom はユーザー定義のため含めない）
_MODE_PROMPTS = {
    "clean": "あなたは音声入力の整形エンジンです。文字起こしテキストから「えーと」「あの」「まあ」「えっと」「なんか」「um」「uh」などのフィラー語と無意味な繰り返しを取り除き、句読点と改行を自然に整えてください。言い直しがある場合は最終的な発言だけを残してください。",
    "bullets": "あなたは音声入力の整形エンジンです。文字起こしテキストの内容を簡潔な箇条書きに整理してください。各項目は「- 」で始め、フィラー語を取り除き、要点だけを短く書いてください。",
    "polite": "あなたは音声入力の整形エンジンです。文字起こしテキストからフィラー語を取り除き、丁寧な敬語（です・ます調）の自然な文章に整えてください。",
    "casual": "あなたは音声入力の整形エンジンです。文字起こしテキストからフィラー語を取り除き、親しい相手へのチャットのようなくだけた自然な文体に整えてください。",
    "email": "あなたは音声入力の整形エンジンです。文字起こしテキストからフィラー語を取り除き、ビジネスメールの本文として自然な文章に整えてください。宛名・署名・件名は追加しないでください。",
}


def build_system_prompt(mode: str, custom_prompt: str) -> str:
    """
    整形モードからシステムプロンプト全文を組み立てる。

    Args:
        mode: 整形モード識別子（clean/bullets/polite/casual/email/custom）。
              未知のモードは clean 扱い
        custom_prompt: custom モード時に使うユーザー定義プロンプト本文

    Returns:
        モード別本文と共通フッターを空行で連結したシステムプロンプト
    """
    if mode == "custom":
        # カスタムプロンプトが空白のみなら clean の本文にフォールバックする
        body = custom_prompt if custom_prompt.strip() else _MODE_PROMPTS["clean"]
    else:
        body = _MODE_PROMPTS.get(mode, _MODE_PROMPTS["clean"])
    return body + "\n\n" + _COMMON_FOOTER


def format_text(text: str, mode: str, custom_prompt: str, model: str) -> str:
    """
    テキストを Groq の LLM で整形する。失敗時は必ず原文をそのまま返す。

    Args:
        text: 文字起こし確定テキスト
        mode: 整形モード識別子（clean/bullets/polite/casual/email/custom）
        custom_prompt: custom モード時のプロンプト本文
        model: 整形に使う Groq モデル名（例: "llama-3.1-8b-instant"）

    Returns:
        整形後テキスト（前後空白除去済み）。空入力・キー未設定・API 失敗時は原文
    """
    # 空白のみなら整形する意味がない（API を呼ばない）
    if not text.strip():
        return text

    # 文字起こし（api_transcriber）と同じく環境変数フォールバックを持たせ、
    # キーの解決方法をバックエンド間で揃える（Mac 版 Keychain.apiKey と同挙動）
    api_key = secrets.get_api_key(secrets.SERVICE_GROQ) or os.environ.get("GROQ_API_KEY")
    if not api_key:
        logger.warning("テキスト整形をスキップ: Groq の API キーが未設定です")
        return text

    start = time.perf_counter()
    try:
        resp = httpx.post(
            _API_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": build_system_prompt(mode, custom_prompt)},
                    {"role": "user", "content": text},
                ],
                "temperature": 0.2,
            },
            timeout=_TIMEOUT_SEC,
        )
        if resp.status_code != 200:
            logger.warning(
                f"テキスト整形に失敗（HTTP {resp.status_code}）、原文を使用: {resp.text[:200]}"
            )
            return text
        # 応答構造の不正（choices 欠落等）は下の except でまとめて原文に倒す
        formatted = (resp.json()["choices"][0]["message"]["content"] or "").strip()
    except Exception as e:
        # タイムアウト・接続失敗・JSON 不正など、何があっても原文で続行する
        logger.warning(f"テキスト整形に失敗、原文を使用: {e}")
        return text

    if not formatted:
        logger.warning("テキスト整形の応答が空のため原文を使用します")
        return text

    elapsed_ms = (time.perf_counter() - start) * 1000
    logger.info(
        f"テキスト整形完了 ({mode}): {elapsed_ms:.0f}ms, {len(text)} → {len(formatted)} 文字"
    )
    return formatted
