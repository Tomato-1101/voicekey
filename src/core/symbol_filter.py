"""
入力文から「残す記号」以外の記号を消す純関数（副作用なし・決定的）

音声認識や翻訳の結果に混じる ！？「」（）… などを貼付直前に落とす。
Mac 版 SymbolFilter.swift と規則・グループ定義を完全一致させること。

規則:
 - 削除候補は下の ``GROUPS`` に載っている文字だけ。それ以外（- / @ # ~ 〜 ー など）は一切触らない。
 - 「残す記号」に選ばれたグループの文字は消さない。
 - 壊さないための例外: ASCII 英数字に挟まれた . , :（3.5・1,000・10:30・example.com）と、
   ASCII 英字に挟まれた ' ’（don't・it's）は消さない。
 - 削除で半角スペースが 2 つ並んだら 1 つに詰め、行頭・行末に残った半角スペースは落とす。
   削除と無関係なスペース・改行・その他の整形はしない。
"""

from typing import Iterable, Optional

# 記号グループ（設定画面の表示順）: (id, 表示名, 含む文字)
GROUPS = (
    ("touten", "読点", "、"),
    ("kuten", "句点", "。"),
    ("comma", "カンマ", ",，"),
    ("period", "ドット", ".．"),
    ("exclamation", "感嘆符", "!！"),
    ("question", "疑問符", "?？"),
    ("nakaguro", "中黒", "・"),
    ("ellipsis", "三点リーダー", "…‥"),
    ("colon", "コロン", ":："),
    ("semicolon", "セミコロン", ";；"),
    ("kagikakko", "かぎかっこ", "「」『』"),
    ("parentheses", "丸かっこ", "()（）"),
    ("brackets", "角かっこ", "[]［］【】〔〕"),
    ("quotes", "引用符", "\"“”'‘’"),
)

# 既定で残すグループ（読点・句点・カンマ・ドット）
DEFAULT_KEEP_GROUPS = ["comma", "period", "touten", "kuten"]

# 英数字に挟まれていたら残す ASCII 記号 / 英字に挟まれていたら残すアポストロフィ
_INNER_KEPT = frozenset(".,:")
_APOSTROPHES = frozenset("'’")


def _is_ascii_letter(ch: Optional[str]) -> bool:
    return ch is not None and ("a" <= ch <= "z" or "A" <= ch <= "Z")


def _is_ascii_alnum(ch: Optional[str]) -> bool:
    return ch is not None and (_is_ascii_letter(ch) or "0" <= ch <= "9")


def filter_symbols(
    text: str,
    enabled: bool = True,
    keep_groups: Optional[Iterable[str]] = None,
    protect: Optional[Iterable[str]] = None,
) -> str:
    """記号を消す。``enabled`` が False なら完全に素通し。

    Args:
        text: 入力文
        enabled: マスタースイッチ
        keep_groups: 残すグループ id（None なら既定の4つ）
        protect: この文字列が現れた範囲は消さない（辞書置換でわざと入れた記号を、
            訳文にかけ直すときも守るため）
    """
    if not enabled or not text:
        return text
    keep = set(DEFAULT_KEEP_GROUPS if keep_groups is None else keep_groups)
    remove = set()
    for gid, _name, chars in GROUPS:
        if gid not in keep:
            remove.update(chars)
    if not remove:
        return text

    # 守る範囲（protect の各文字列が現れた位置）を文字単位の印にする
    guarded = [False] * len(text)
    for word in protect or ():
        if not word:
            continue
        start = text.find(word)
        while start != -1:
            for j in range(start, start + len(word)):
                guarded[j] = True
            start = text.find(word, start + len(word))

    out = []
    # 直前の出力以降に削除があったか（スペースの詰め・落としの判断に使う）
    deleted = False

    def at_line_start() -> bool:
        return not out or out[-1] in ("\n", "\r")

    def trim_trailing_spaces() -> None:
        while out and out[-1] == " ":
            out.pop()

    n = len(text)
    for i, ch in enumerate(text):
        if ch in remove and not guarded[i]:
            prev = text[i - 1] if i > 0 else None
            nxt = text[i + 1] if i + 1 < n else None
            keep_inner = ch in _INNER_KEPT and _is_ascii_alnum(prev) and _is_ascii_alnum(nxt)
            keep_apos = ch in _APOSTROPHES and _is_ascii_letter(prev) and _is_ascii_letter(nxt)
            if not (keep_inner or keep_apos):
                deleted = True
                continue
        if ch == " ":
            if deleted and (at_line_start() or out[-1] == " "):
                continue
        elif ch in ("\n", "\r"):
            if deleted:
                trim_trailing_spaces()
        out.append(ch)
        deleted = False
    if deleted:
        trim_trailing_spaces()
    return "".join(out)
