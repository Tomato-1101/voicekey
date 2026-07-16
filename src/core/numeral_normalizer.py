"""
数字表記の正規化ユーティリティ（v2）

文字起こし確定テキストの数字表記を半角アラビア数字へ寄せる純関数を提供する。
Whisper prompt（Groq/OpenAI）や Deepgram の smart_format で拾いきれない
「漢数字・全角」を貼付直前に補正する最終防衛。LLM を使わず決定的・マイクロ秒で動く。
Mac 版 NumeralNormalizer.swift と規則・範囲を完全一致させること。

変換規則（v2）:
 0. 全角英数字（０-９Ａ-Ｚａ-ｚ）→ 半角。全角記号・句読点は変換しない。
 1. 数字漢字集合 N = { 〇 零 一 二 三 四 五 六 七 八 九 十 百 千 万 億 兆 }。
 2. 保護リスト W（数字漢字で始まる語）を、数字ランの先頭にアンカーして照合する。
    ラン先頭 i で text[i:i+len(W)] == W なら、そのランは変換せずそのまま出す
    （部分一致でなく先頭アンカー＝「十二人」の中の「二人」で「十二」を壊さないため）。
 3. N の極大連続ラン R を走査:
    - 長さ>=2 かつ全て裸数字（〇零一〜九）→ 各桁を置換（二〇二六→2026）。
    - 長さ>=2 で位取りを含む → 日本語数詞としてパースして整数化（千二百三十四→1234）。
      パース不能ならそのまま出力（安全側）。
    - 長さ1（単独漢数字）→ convert_counter=True かつ直後 1 文字が助数詞なら変換
      （三時→3時・十時→10時）。該当しなければそのまま（千葉・六本木 を守る）。
"""

from typing import Iterable, List, Optional

# 裸の数字漢字 → 桁値（〇零＝0。位取り・グループ乗数は含めない）。
_BARE_DIGITS = {
    "〇": 0, "零": 0, "一": 1, "二": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}

# 位取り（万未満グループ内の副乗数）: 十=10・百=100・千=1000。
_SMALL_UNITS = {"十": 10, "百": 100, "千": 1000}

# グループ乗数: 万=10^4・億=10^8・兆=10^12。
_BIG_UNITS = {"万": 10_000, "億": 100_000_000, "兆": 1_000_000_000_000}

# 単独漢数字（長さ1・助数詞つき）の値。裸数字＋位取り＋グループ乗数を単体の値として扱う。
_SINGLE_VALUES = {
    "〇": 0, "零": 0, "一": 1, "二": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
    "十": 10, "百": 100, "千": 1000,
    "万": 10_000, "億": 100_000_000, "兆": 1_000_000_000_000,
}

# 数字漢字集合 N（裸数字 ∪ 位取り ∪ グループ乗数）。
_NUMERAL_CHARS = set(_SINGLE_VALUES.keys())

# 助数詞集合 COUNTER（安全・高頻度。地名で誤爆しやすい 本/反/条/丁/目/州/国 は除外）。
_COUNTERS = set(
    "時分秒人名個円年月日回歳才度台冊枚杯匹頭件品番位階週泊章話"
)


def _to_halfwidth(ch: str) -> str:
    """全角数字・全角英字を半角へ変換する（対象外はそのまま返す）。"""
    code = ord(ch)
    is_fullwidth = (
        0xFF10 <= code <= 0xFF19    # 全角数字 ０-９
        or 0xFF21 <= code <= 0xFF3A  # 全角大文字 Ａ-Ｚ
        or 0xFF41 <= code <= 0xFF5A  # 全角小文字 ａ-ｚ
    )
    return chr(code - 0xFEE0) if is_fullwidth else ch


def _parse_japanese_numeral(run: List[str]) -> Optional[int]:
    """位取りを含む日本語数詞を整数へパースする（万/億/兆でグループ分けし副乗数を加算）。

    例: 千二百三十四=1234・二十三=23・三万五千=35000。パース不能・曖昧は None。
    """
    total = 0    # 万/億/兆で区切った上位グループの確定和
    section = 0  # 現在のグループ（万未満）の値
    number = 0   # 直前に読んだ裸数字（十百千 の係数）

    for c in run:
        if c in _BARE_DIGITS:
            number = number * 10 + _BARE_DIGITS[c]
        elif c in _SMALL_UNITS:
            # 十/百/千: 直前の裸数字が無ければ 1（十＝10）
            section += (number if number != 0 else 1) * _SMALL_UNITS[c]
            number = 0
        elif c in _BIG_UNITS:
            # 万/億/兆: グループを確定して乗じる（単独なら万＝10000）
            section += number
            total += (section if section != 0 else 1) * _BIG_UNITS[c]
            section = 0
            number = 0
        else:
            return None  # N 以外は来ない想定だが安全側

    result = total + section + number
    return None if result == 0 else result


def normalize(
    text: str,
    enabled: bool = True,
    convert_counter: bool = True,
    protect_words: Optional[Iterable[str]] = None,
) -> str:
    """確定テキストの数字表記を半角へ正規化する（冪等・純関数）。

    Args:
        text: 正規化対象のテキスト
        enabled: マスタースイッチ（False なら完全パススルー）
        convert_counter: 単独漢数字＋助数詞ルールのゲート（位取り>=2 はマスターのみで常時変換）
        protect_words: 変換しない語（数字漢字で始まる語だけをラン先頭にアンカー照合）

    Returns:
        正規化後のテキスト
    """
    # マスター OFF は一切触らず完全パススルー（全角半角化もしない）
    if not enabled or not text:
        return text

    # 数字漢字で始まる保護語だけを list 化して前処理
    patterns: List[List[str]] = []
    for word in (protect_words or []):
        chars = list(word)
        if chars and chars[0] in _NUMERAL_CHARS:
            patterns.append(chars)

    chars = list(text)
    n = len(chars)
    out: List[str] = []
    i = 0

    while i < n:
        c = chars[i]
        # 数字漢字でなければ全角→半角だけ掛けて通す
        if c not in _NUMERAL_CHARS:
            out.append(_to_halfwidth(c))
            i += 1
            continue

        # 数字漢字の極大連続ラン [i, j)
        j = i
        while j < n and chars[j] in _NUMERAL_CHARS:
            j += 1
        run = chars[i:j]

        # 保護: ラン先頭にアンカーして保護語と前方一致したらラン全体をそのまま出す
        if _is_protected(chars, i, patterns):
            out.extend(run)
            i = j
            continue

        if len(run) >= 2:
            if all(ch in _BARE_DIGITS for ch in run):
                # 全て裸数字 → 各桁を置換（読み上げ数字列）
                out.extend(str(_BARE_DIGITS[ch]) for ch in run)
            else:
                value = _parse_japanese_numeral(run)
                if value is not None:
                    out.append(str(value))
                else:
                    out.extend(run)  # パース不能はそのまま（安全側）
        else:
            # 長さ1（単独漢数字）: convert_counter かつ直後が助数詞のときだけ変換
            single = run[0]
            if convert_counter and j < n and chars[j] in _COUNTERS:
                out.append(str(_SINGLE_VALUES[single]))
            else:
                out.append(single)
        i = j

    return "".join(out)


def _is_protected(chars: List[str], i: int, patterns: List[List[str]]) -> bool:
    """ラン先頭 i にアンカーして、いずれかの保護語と前方一致するか判定する。"""
    n = len(chars)
    for w in patterns:
        end = i + len(w)
        if end <= n and chars[i:end] == w:
            return True
    return False
