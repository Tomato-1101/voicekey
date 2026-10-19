"""
Meet の字幕から「確定した発言」を取り出す（Mac 版 CaptionSettleTracker.swift の移植・純ロジック）

Meet の字幕は**書きながら伸びる**（「こんに」→「こんにちは」→「こんにちは今日は」）うえに、
伸びる途中で**書き直される**（"hello everyone" → "Hello everyone, today"、「良いです」→「いいですね」）。
見えたものをそのまま書くと同じ発言が何度も議事録に載るので、書き直しは「同じ発言の改訂」として
追いかけ、「伸びが止まった」「ブロックが画面から消えた（別の発言に置き換わった）」ときだけ 1 行を確定させる。

DOM の読み取り（scripts.CAPTION_SCRIPT）とは分けてある。時刻と文字列だけを見るのでブラウザ無しでテストできる。
"""

import unicodedata
from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple

# 字幕欄まるごと（region）の経路で、変化し続けていても途中で確定させる間隔（秒）
REGION_FORCE_SECONDS = 15.0
# 話者が取れなかったときの内部キー
_UNKNOWN_SPEAKER = "\u0000unknown"
# 書き直しとみなす編集量の上限（元の文の長さに対する割合）
REVISION_RATIO = 0.3
# これより短い文は、正規化した先頭一致のときだけ同じ発言とみなす
MINIMUM_FUZZY_LENGTH = 4


@dataclass(frozen=True)
class Line:
    """確定した 1 行（話者が取れなかったら speaker は None）。"""

    speaker: Optional[str]
    text: str


@dataclass(frozen=True)
class Entry:
    """画面から読んだ 1 件。"""

    speaker: str
    text: str


@dataclass
class _Pending:
    key: str
    text: str
    last_changed: float
    # 同じブロックのうち、もう議事録に書いた部分（伸びた分だけを書くため）
    emitted: Optional[str]


def _is_alnum_char(ch: str) -> bool:
    """文字か数字か（Swift の CharacterSet.alphanumerics と同じく結合文字も含める）。"""
    return ch.isalnum() or unicodedata.category(ch).startswith("M")


def _normalized(text: str) -> List[Tuple[str, int]]:
    """比較用に、文字と数字だけを小文字で取り出す（元の文字列での位置も持つ）。"""
    return [(ch.lower(), offset) for offset, ch in enumerate(text) if _is_alnum_char(ch)]


def _is_strippable(ch: str) -> bool:
    return ch.isspace() or unicodedata.category(ch).startswith("P")


def _strip_punct(text: str) -> str:
    start, end = 0, len(text)
    while start < end and _is_strippable(text[start]):
        start += 1
    while end > start and _is_strippable(text[end - 1]):
        end -= 1
    return text[start:end]


def _alignment(pattern: List[str], text: List[str], free_start: bool) -> Tuple[int, int]:
    """半大域アラインメントの編集距離（pattern は全体、text は末尾が自由）。同点なら後ろの終わり位置。"""
    n, m = len(pattern), len(text)
    if n == 0:
        return 0, 0
    previous = [0] * (m + 1) if free_start else list(range(m + 1))
    current = [0] * (m + 1)
    for i in range(1, n + 1):
        current[0] = i
        pc = pattern[i - 1]
        for j in range(1, m + 1):
            substitution = previous[j - 1] + (0 if pc == text[j - 1] else 1)
            current[j] = min(substitution, previous[j] + 1, current[j - 1] + 1)
        previous, current = current, previous
    best = (10 ** 9, 0)
    for j in range(m + 1):
        if previous[j] <= best[0]:
            best = (previous[j], j)
    return best


def _prefix_alignment(base: List[str], text: List[str]) -> Optional[int]:
    """base 全体を text の文頭に重ねたときの最小編集量が許容範囲なら、text 側の終わり位置を返す。"""
    common = 0
    while common < len(base) and common < len(text) and base[common] == text[common]:
        common += 1
    if common == len(base):
        return common
    if len(base) < MINIMUM_FUZZY_LENGTH:
        return None
    limit = int(len(base) * REVISION_RATIO)
    distance, end = _alignment(base[common:], text[common:], free_start=False)
    if distance > limit:
        return None
    return common + end


def _substring_alignment(pattern: List[str], text: List[str]) -> Optional[int]:
    """pattern が text のどこかに許容範囲の編集量で現れれば、その終わり位置（最も後ろ）を返す。"""
    limit = 0 if len(pattern) < MINIMUM_FUZZY_LENGTH else int(len(pattern) * REVISION_RATIO)
    distance, end = _alignment(pattern, text, free_start=True)
    return end if distance <= limit else None


def _rest(text: str, target: List[Tuple[str, int]], end: int) -> str:
    """正規化後の位置 end より後ろを、元の文字列から切り出す（前後の句読点・空白は落とす）。"""
    cut = 0 if end == 0 else target[end - 1][1] + 1
    return _strip_punct(text[cut:])


def is_revision(old: str, new: str) -> bool:
    """new が old と同じ発言の伸び・書き直しか（大文字小文字・句読点・空白を無視して比べる）。"""
    old_chars = [c for c, _ in _normalized(old)]
    new_chars = [c for c, _ in _normalized(new)]
    if not old_chars or not new_chars:
        return False
    if _prefix_alignment(old_chars, new_chars) is not None:
        return True
    # Meet が末尾を少し削って書き直す場合（new の方が短い）も、6 割以上残っていれば同じ発言
    if len(new_chars) * 10 < len(old_chars) * 6:
        return False
    return _prefix_alignment(new_chars, old_chars) is not None


def continuation(text: str, prefix: str) -> Optional[str]:
    """text が prefix の続き（書き直しを許す）なら、prefix に当たる部分より後ろを返す（続きでなければ None）。"""
    base = [c for c, _ in _normalized(prefix)]
    target = _normalized(text)
    if not base or not target:
        return None
    end = _prefix_alignment(base, [c for c, _ in target])
    if end is None:
        return None
    return _rest(text, target, end)


def new_tail(text: str, previous: Optional[str]) -> Optional[str]:
    """字幕欄の全文 text のうち、前回確定した全文 previous より後ろに増えた部分を返す。

    字幕欄は古い行が上へ押し出されて消えるので、previous の**末尾**（最大 60 文字）が text の
    どこにあるかを近似一致で探し、その後ろを返す。見つからなければ全部が新しい。
    """
    if previous is None:
        return text
    target = _normalized(text)
    previous_chars = [c for c, _ in _normalized(previous)]
    if not previous_chars or not target:
        return text
    target_chars = [c for c, _ in target]
    length = min(len(previous_chars), 60)
    while True:
        anchor = previous_chars[-length:]
        end = _substring_alignment(anchor, target_chars)
        if end is not None:
            return _rest(text, target, end)
        if length <= 8:
            return text
        length = max(8, length // 2)


class CaptionSettleTracker:
    """字幕の確定判定（Mac の CaptionSettleTracker と同じ規則）。"""

    def __init__(self, settle_seconds: float = 2.5) -> None:
        self.settle_seconds = settle_seconds
        self._pending: List[_Pending] = []
        # 話者ごとに「確定済みで、直前の読み取りでもまだ画面に見えていた本文」
        self._settled_on_screen: Dict[str, Set[str]] = {}
        # 字幕欄まるごとの経路の確定待ち (text, first_changed, last_changed)
        self._region_pending: Optional[Tuple[str, float, float]] = None
        self._region_settled_text: Optional[str] = None

    def ingest(self, entries: List[Entry], now: float) -> List[Line]:
        """画面の字幕を 1 回分取り込み、確定した行を返す。"""
        settled: List[Line] = []
        visible: Dict[str, Set[str]] = {}
        matched: Set[int] = set()
        keys_seen: Set[str] = set()

        for entry in entries:
            text = (entry.text or "").strip()
            if not text:
                continue
            speaker = (entry.speaker or "").strip()
            key = speaker or _UNKNOWN_SPEAKER
            visible.setdefault(key, set()).add(text)
            keys_seen.add(key)
            # 確定済みの文がまだ見えているだけ（前のブロックが残っている）なら何もしない
            if text in self._settled_on_screen.get(key, set()):
                continue
            candidates = [i for i, p in enumerate(self._pending) if p.key == key and i not in matched]
            exact = next((i for i in candidates if self._pending[i].text == text), None)
            if exact is not None:
                matched.add(exact)  # 変化なし。settle を過ぎたら下で確定する
                continue
            revised = next((i for i in candidates if is_revision(self._pending[i].text, text)), None)
            if revised is not None:
                matched.add(revised)
                self._pending[revised].text = text  # 同じ発言が伸びた・書き直された
                self._pending[revised].last_changed = now
                continue
            self._pending.append(_Pending(key, text, now, self._emitted_part(text, self._settled_on_screen.get(key))))
            matched.add(len(self._pending) - 1)

        remaining: List[_Pending] = []
        for index, block in enumerate(self._pending):
            quiet = now - block.last_changed >= self.settle_seconds
            replaced = index not in matched and block.key in keys_seen
            if quiet or replaced:
                line = self._settle(block.key, block.text, block.emitted)
                if line is not None:
                    settled.append(line)
            else:
                remaining.append(block)
        self._pending = remaining

        # 画面から消えた確定済みの文は忘れる（同じ言葉を後でもう一度言ったら書けるように）
        for key in list(self._settled_on_screen.keys()):
            still = self._settled_on_screen[key] & visible.get(key, set())
            if still:
                self._settled_on_screen[key] = still
            else:
                del self._settled_on_screen[key]
        return settled

    def ingest_region(self, text: str, now: float) -> List[Line]:
        """字幕欄まるごとの生テキストを取り込み、前回確定分から増えた末尾だけを返す（代替経路）。"""
        trimmed = (text or "").strip()
        if trimmed:
            if self._region_pending is not None:
                current_text, first, _ = self._region_pending
                if current_text != trimmed:
                    self._region_pending = (trimmed, first, now)
            elif trimmed != self._region_settled_text:
                self._region_pending = (trimmed, now, now)
        if self._region_pending is None:
            return []
        current_text, first, last = self._region_pending
        quiet = now - last >= self.settle_seconds
        too_long = now - first >= REGION_FORCE_SECONDS
        if not (quiet or too_long):
            return []
        self._region_pending = None
        return self._settle_region(current_text)

    def flush(self) -> List[Line]:
        """確定待ちを全部吐き出す（退出時・終了時に取りこぼさないため）。"""
        waiting, self._pending = self._pending, []
        lines = [line for line in (self._settle(p.key, p.text, p.emitted) for p in waiting) if line is not None]
        if self._region_pending is not None:
            region_text = self._region_pending[0]
            self._region_pending = None
            lines += self._settle_region(region_text)
        return lines

    # --- 内部 ---

    def _settle(self, key: str, text: str, emitted: Optional[str]) -> Optional[Line]:
        self._settled_on_screen.setdefault(key, set()).add(text)
        body = text
        if emitted is not None:
            rest = continuation(text, emitted)
            body = text if rest is None else rest
        if not body:
            return None
        return Line(None if key == _UNKNOWN_SPEAKER else key, body)

    def _settle_region(self, text: str) -> List[Line]:
        previous = self._region_settled_text
        self._region_settled_text = text
        body = new_tail(text, previous)
        return [Line(None, body)] if body else []

    @staticmethod
    def _emitted_part(text: str, settled: Optional[Set[str]]) -> Optional[str]:
        """新しく見えた本文が確定済みの文の続きなら、その確定済みの文を返す（いちばん長いもの）。"""
        if not settled:
            return None
        matches = [s for s in settled if continuation(text, s) is not None]
        return max(matches, key=len) if matches else None
