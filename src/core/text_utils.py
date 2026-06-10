"""
テキスト整形ユーティリティ

文字起こし結果の貼り付け前整形を提供する。現状は Deepgram が日本語出力に
挿入する単語間スペースの除去のみ（Mac 版 StreamingTranscriber.stripCJKSpaces と
同じ規則を Python へ移植したもの）。
"""


def _is_cjk(ch: str) -> bool:
    """
    1 文字がひらがな・カタカナ・漢字（および全角/半角形）かを判定する。

    Args:
        ch: 判定対象の 1 文字

    Returns:
        CJK 文字なら True
    """
    code = ord(ch)
    return (
        0x3040 <= code <= 0x30FF       # ひらがな・カタカナ
        or 0x3400 <= code <= 0x4DBF    # CJK 拡張A
        or 0x4E00 <= code <= 0x9FFF    # CJK 統合漢字
        or 0xF900 <= code <= 0xFAFF    # CJK 互換漢字
        or 0xFF00 <= code <= 0xFFEF    # 全角・半角形
    )


def strip_cjk_spaces(text: str) -> str:
    """
    「前後どちらかが CJK 文字」のスペースだけを除去する。

    Deepgram は日本語出力に単語間スペースを挿入するため、そのまま貼り付けると
    「今日 は 晴れ」のように不自然になる。英単語間のスペースは残すので
    "GPT 4" や "Claude Code" のような英語表記は壊さない。

    Args:
        text: 整形対象のテキスト

    Returns:
        CJK 隣接スペースを除去したテキスト
    """
    if " " not in text:
        return text

    chars = text
    out = []
    n = len(chars)
    for i, ch in enumerate(chars):
        if ch == " ":
            prev_cjk = i > 0 and _is_cjk(chars[i - 1])
            next_cjk = i + 1 < n and _is_cjk(chars[i + 1])
            if prev_cjk or next_cjk:
                continue  # CJK 隣接のスペースは除去
        out.append(ch)
    return "".join(out)
