"""
数字表記の正規化ユーティリティ

文字起こし確定テキストの数字表記を半角アラビア数字へ寄せる純関数を提供する。
Whisper prompt（Groq/OpenAI）や Deepgram の smart_format で拾いきれない
「漢数字・全角」を貼付直前に補正する最終防衛。安全側に振り、普通の日本語語彙
（一人・二階・十時・一番・五月雨 等）を壊さないよう最小限の 2 変換のみ行う。
Mac 版 NumeralNormalizer.swift と規則・範囲を完全一致させること。

変換規則:
 1. 全角数字（０-９）・全角英字（Ａ-Ｚ / ａ-ｚ）→ 半角。全角記号・句読点は変換しない。
 2. 位取りを含まない漢数字（〇一二三四五六七八九）が 2 文字以上連続する並びだけ
    アラビア数字へ変換（例: 三五八〇九一 → 358091）。単独の漢数字や位取り
    （十・百・千・万）は変換しない（「一人」「二階」「十時」等の普通の語を守るため）。
"""

# 漢数字 → アラビア数字（位取りの十・百・千・万は含めない＝連続並びの読み上げ数字だけを対象）。
_KANJI_DIGITS = {
    "〇": "0", "一": "1", "二": "2", "三": "3", "四": "4",
    "五": "5", "六": "6", "七": "7", "八": "8", "九": "9",
}


def _to_halfwidth(ch: str) -> str:
    """全角数字・全角英字を半角へ変換する（対象外はそのまま返す）。"""
    code = ord(ch)
    is_fullwidth = (
        0xFF10 <= code <= 0xFF19    # 全角数字 ０-９
        or 0xFF21 <= code <= 0xFF3A  # 全角大文字 Ａ-Ｚ
        or 0xFF41 <= code <= 0xFF5A  # 全角小文字 ａ-ｚ
    )
    return chr(code - 0xFEE0) if is_fullwidth else ch


def normalize(text: str) -> str:
    """
    確定テキストの数字表記を半角へ正規化する（冪等）。

    Args:
        text: 正規化対象のテキスト

    Returns:
        全角数字/英字を半角化し、2 文字以上連続する漢数字をアラビア数字化したテキスト
    """
    if not text:
        return text

    out = []
    run = []  # 連続する漢数字を一時的に溜めるバッファ

    def flush_run():
        # 直前まで溜めた漢数字を確定する。2 文字以上ならアラビア数字化、1 文字ならそのまま。
        if len(run) >= 2:
            out.extend(_KANJI_DIGITS[c] for c in run)
        else:
            out.extend(run)
        run.clear()

    for ch in text:
        if ch in _KANJI_DIGITS:
            run.append(ch)  # 連続漢数字を溜める（確定は非漢数字が来た時）
            continue
        flush_run()
        out.append(_to_halfwidth(ch))
    flush_run()
    return "".join(out)
