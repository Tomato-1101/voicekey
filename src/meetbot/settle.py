"""
Meet の字幕から「確定した発言」を取り出す

Meet の字幕は**書きながら伸びる**（「こんに」→「こんにちは」→「こんにちは今日は」）うえに、
伸びる途中で**書き直される**（"hello everyone" → "Hello everyone, today"、「良いです」→「いいですね」）。
見えたものをそのまま書くと同じ発言が何度も議事録に載るので、書き直しは「同じ発言の改訂」として
追いかけ、「伸びが止まった」「ブロックが画面から消えた（別の発言に置き換わった）」ときだけ 1 行を確定させる。

DOM の読み取り（`scripts.CAPTIONS`）とは分けてある。ここは時刻と文字列だけを見る純粋なロジックなので、
ブラウザ無しで回帰テストできる（Mac 版の `CaptionSettleTracker` と同じ判定）。
"""

import unicodedata
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Set, Tuple

# 字幕欄まるごと（region）の経路で、変化し続けていても途中で確定させる間隔（秒）。
# region は表示欄の生テキストなので、話が途切れないまま古い行が画面から押し出されると
# 一度も確定しないまま失われる。15 秒ごとに「ここまで」を確定して取りこぼしを防ぐ
# （続きは前回確定分との差分だけを書くので、二重にはならない）。
_REGION_FORCE_SECONDS = 15.0

# 話者が取れなかったときの内部キー
_UNKNOWN_SPEAKER = "\u0000unknown"

# 書き直しとみなす編集量の上限（元の文の長さに対する割合）。
# Meet の書き直しは大文字化・句読点・数語の言い直しが中心で、文の 3 割を超えて変わることはまず無い。
_REVISION_RATIO = 0.3

# これより短い文は、正規化した先頭一致のときだけ同じ発言とみなす。
# 「はい」と「では」のような短い文は 1 文字違うだけで割合が 5 割になり、割合での判定が効かない。
_MINIMUM_FUZZY_LENGTH = 4

# 正規化した 1 文字（比較用の文字, 元の文字列での位置）
_Normalized = List[Tuple[str, int]]


@dataclass(frozen=True)
class Line:
    """
    確定した 1 行。

    Attributes:
        speaker: 話者名（取れなかったときは None）
        text: 本文
    """

    speaker: Optional[str]
    text: str


@dataclass
class _Pending:
    """
    確定待ちのブロック。

    Attributes:
        key: 話者のキー
        text: いまの本文
        last_changed: 最後に本文が変わった時刻
        emitted: 同じブロックのうち、もう議事録に書いた部分（確定後に同じブロックが伸び続けたとき、
                 伸びた分だけを書くために持つ）
    """

    key: str
    text: str
    last_changed: float
    emitted: Optional[str]


class CaptionSettleTracker:
    """
    字幕の確定判定。

    時刻は呼び出し側が渡す秒（単調増加ならどの時計でもよい）。
    """

    def __init__(self, settle_seconds: float = 2.5):
        """
        Args:
            settle_seconds: 確定とみなすまでの無変化時間。短すぎると 1 つの発言が細切れになり、
                長すぎると議事録への反映が遅れる。Meet の字幕はおよそ 1〜2 秒おきに伸びるので、
                2.5 秒あれば「言い終わった」と判断できる。
        """
        self._settle_seconds = settle_seconds
        # 確定待ちのブロック（画面に出た順）。同じ話者のブロックが同時に 2 つ見えることがあるので、
        # 話者ごとに 1 枠ではなく、ブロックごとに持つ。
        self._pending: List[_Pending] = []
        # 話者ごとに「確定済みで、直前の読み取りでもまだ画面に見えていた本文」。
        # Meet は言い終わった字幕をしばらく画面に残すので、見え続けているだけなら拾い直さない。
        # 画面から消えたら外すので、同じ言葉を後でもう一度言ったときはちゃんと書かれる。
        self._settled_on_screen: Dict[str, Set[str]] = {}
        # 字幕欄まるごとの経路の確定待ち（本文, 最初に変わった時刻, 最後に変わった時刻）
        self._region_pending: Optional[Tuple[str, float, float]] = None
        # 字幕欄まるごとの経路で、最後に確定させた欄の全文
        self._region_settled_text: Optional[str] = None

    def ingest(self, entries: Sequence[Tuple[str, str]], now: float) -> List[Line]:
        """
        画面の字幕を 1 回分取り込み、確定した行を返す。

        確定するのは次の 2 つだけ:
        - 一定時間変化が無い
        - 同じ話者の字幕は見えているのに、そのブロックがどれにも対応しない（画面から消えて別の発言に置き換わった）

        話者のブロックが 1 つも見えないとき（読み取りの一時的な空振りもある）は時間経過だけで確定する。

        Args:
            entries: いま画面に出ている字幕（話者名, 本文）
            now: 取り込んだ時刻

        Returns:
            確定した行（無ければ空）
        """
        settled: List[Line] = []
        visible: Dict[str, Set[str]] = {}
        matched: Set[int] = set()
        keys_seen: Set[str] = set()

        for raw_speaker, raw_text in entries:
            text = (raw_text or "").strip()
            if not text:
                continue
            speaker = (raw_speaker or "").strip()
            key = speaker or _UNKNOWN_SPEAKER
            visible.setdefault(key, set()).add(text)
            keys_seen.add(key)

            # 確定済みの文がまだ見えているだけ（前のブロックが残っている）なら何もしない。
            # ここで確定待ちを確定させると、2 つのブロックを交互に書く重複になる。
            if text in self._settled_on_screen.get(key, ()):
                continue

            # 同じ話者の確定待ちのうち、まだ今回対応させていないものと照合する。
            # 完全一致を先に探すのは、同じ話者のブロックが 2 つ見えているときに取り違えないため。
            candidates = [
                i for i, block in enumerate(self._pending) if block.key == key and i not in matched
            ]
            same = next((i for i in candidates if self._pending[i].text == text), None)
            if same is not None:
                matched.add(same)  # 変化なし。settle を過ぎたら下で確定する
                continue
            revised = next((i for i in candidates if is_revision(self._pending[i].text, text)), None)
            if revised is not None:
                matched.add(revised)
                self._pending[revised].text = text  # 同じ発言が伸びた・書き直された
                self._pending[revised].last_changed = now
                continue
            self._pending.append(
                _Pending(key, text, now, _emitted_part(text, self._settled_on_screen.get(key)))
            )
            matched.add(len(self._pending) - 1)

        # 確定させるもの: 伸びが止まったもの、または同じ話者の字幕は見えているのにどれにも対応しなかったもの
        remaining: List[_Pending] = []
        for index, block in enumerate(self._pending):
            quiet = now - block.last_changed >= self._settle_seconds
            replaced = index not in matched and block.key in keys_seen
            if quiet or replaced:
                line = self._settle(block)
                if line is not None:
                    settled.append(line)
            else:
                remaining.append(block)
        self._pending = remaining

        # 画面から消えた確定済みの文は忘れる（同じ言葉を後でもう一度言ったら書けるように）
        for key in list(self._settled_on_screen):
            still = self._settled_on_screen[key] & visible.get(key, set())
            if still:
                self._settled_on_screen[key] = still
            else:
                del self._settled_on_screen[key]
        return settled

    def ingest_region(self, text: str, now: float) -> List[Line]:
        """
        字幕欄まるごとの生テキストを 1 回分取り込み、確定した部分を返す（代替経路）。

        話者ごとの字幕ブロックが取れないとき、`scripts.CAPTIONS` は字幕欄の全文を 1 件で返す。
        全文には言い終わった古い行も含まれ続けるので、確定のたびに前回確定した全文から
        新しく増えた末尾だけを書く。

        Args:
            text: 字幕欄の全文
            now: 取り込んだ時刻

        Returns:
            確定した行（話者は取れないので None）
        """
        trimmed = (text or "").strip()
        if trimmed:
            if self._region_pending is not None:
                current_text, first_changed, _ = self._region_pending
                if current_text != trimmed:
                    self._region_pending = (trimmed, first_changed, now)
            elif trimmed != self._region_settled_text:
                self._region_pending = (trimmed, now, now)
        if self._region_pending is None:
            return []
        current_text, first_changed, last_changed = self._region_pending
        quiet = now - last_changed >= self._settle_seconds
        too_long = now - first_changed >= _REGION_FORCE_SECONDS
        if not (quiet or too_long):
            return []
        self._region_pending = None
        return self._settle_region(current_text)

    def flush(self) -> List[Line]:
        """
        確定待ちを全部吐き出す（退出時・終了時に取りこぼさないため）。

        Returns:
            確定した行
        """
        waiting = self._pending
        self._pending = []
        lines = [line for line in (self._settle(block) for block in waiting) if line is not None]
        if self._region_pending is not None:
            text = self._region_pending[0]
            self._region_pending = None
            lines += self._settle_region(text)
        return lines

    def _settle(self, block: _Pending) -> Optional[Line]:
        """1 件を確定させる（既に書いた部分があれば、その続きだけを返す）。"""
        self._settled_on_screen.setdefault(block.key, set()).add(block.text)
        body = block.text
        if block.emitted is not None:
            rest = continuation(block.text, block.emitted)
            body = block.text if rest is None else rest
        if not body:
            return None
        return Line(None if block.key == _UNKNOWN_SPEAKER else block.key, body)

    def _settle_region(self, text: str) -> List[Line]:
        """字幕欄の全文を確定させる（前回確定した全文から増えた末尾だけを返す）。"""
        previous = self._region_settled_text
        self._region_settled_text = text
        body = new_tail(text, previous)
        return [Line(None, body)] if body else []


def _emitted_part(text: str, settled: Optional[Set[str]]) -> Optional[str]:
    """新しく見えた本文が確定済みの文の続きなら、その確定済みの文（一番長いもの）を返す。"""
    matches = [s for s in (settled or ()) if continuation(text, s) is not None]
    return max(matches, key=len) if matches else None


# ============================================
# 書き直しの判定（純粋関数）
# ============================================
def is_revision(old: str, new: str) -> bool:
    """
    `new` が `old` と同じ発言の伸び・書き直しかを返す。

    大文字小文字・句読点・空白を無視して比べ、`old` 全体が `new` の文頭に 3 割以内の編集で
    重なれば同じ発言とみなす。Meet が末尾を少し削って書き直す場合（`new` の方が短い）も、
    6 割以上残っていれば同じ発言とみなす。
    """
    old_chars = [c for c, _ in _normalized(old)]
    new_chars = [c for c, _ in _normalized(new)]
    if not old_chars or not new_chars:
        return False
    if _prefix_alignment(old_chars, new_chars) is not None:
        return True
    if len(new_chars) * 10 < len(old_chars) * 6:
        return False
    return _prefix_alignment(new_chars, old_chars) is not None


def continuation(text: str, prefix: str) -> Optional[str]:
    """
    `text` が `prefix` の続き（書き直しを許す）なら、`prefix` に当たる部分より後ろを返す。

    Returns:
        続きの部分（前後の句読点・空白は落とす。伸びていなければ空文字）。続きでなければ None
    """
    base = [c for c, _ in _normalized(prefix)]
    target = _normalized(text)
    if not base or not target:
        return None
    end = _prefix_alignment(base, [c for c, _ in target])
    if end is None:
        return None
    return _rest(text, target, end)


def new_tail(text: str, previous: Optional[str]) -> str:
    """
    字幕欄の全文 `text` のうち、前回確定した全文 `previous` より後ろに増えた部分を返す。

    字幕欄は古い行が上へ押し出されて消えるので、`previous` の**末尾**（最大 60 文字）が
    `text` のどこにあるかを近似一致で探し、その後ろを返す。見つからなければ全部が新しい。
    """
    if previous is None:
        return text
    target = _normalized(text)
    previous_chars = [c for c, _ in _normalized(previous)]
    if not previous_chars or not target:
        return text
    target_chars = [c for c, _ in target]
    # 前回の頭が押し出されて消えていると長い目印は見つからないので、末尾を短くしながら探す
    # （8 文字より短い目印は偶然の一致が増えるので使わない）
    length = min(len(previous_chars), 60)
    while True:
        end = _substring_alignment(previous_chars[-length:], target_chars)
        if end is not None:
            return _rest(text, target, end)
        if length <= 8:
            return text
        length = max(8, length // 2)


def _is_trimmed(character: str) -> bool:
    """前後から落とす文字（空白・句読点）か。"""
    return character.isspace() or unicodedata.category(character).startswith("P")


def _rest(text: str, target: _Normalized, end: int) -> str:
    """正規化後の位置 `end` より後ろを、元の文字列から切り出す（前後の空白・句読点は落とす）。"""
    cut = 0 if end == 0 else target[end - 1][1] + 1
    tail = text[cut:]
    start, stop = 0, len(tail)
    while start < stop and _is_trimmed(tail[start]):
        start += 1
    while stop > start and _is_trimmed(tail[stop - 1]):
        stop -= 1
    return tail[start:stop]


def _normalized(text: str) -> _Normalized:
    """比較用に、文字と数字だけを小文字で取り出す（元の文字列での位置も持つ）。"""
    return [(character.lower(), offset) for offset, character in enumerate(text) if character.isalnum()]


def _prefix_alignment(base: List[str], text: List[str]) -> Optional[int]:
    """
    `base` 全体を `text` の文頭に重ねたときの最小編集量が許容範囲内なら、`text` 側の終わり位置を返す。

    `text` の末尾は自由（伸びた分は数えない）。同じ編集量なら終わり位置は長い方を選ぶ
    （既に書いた文字を「続き」として二重に書かないため）。
    """
    # 共通の文頭は編集量 0 なので先に飛ばす（長い字幕でも比較を軽く保つ）
    common = 0
    while common < len(base) and common < len(text) and base[common] == text[common]:
        common += 1
    if common == len(base):
        return common
    if len(base) < _MINIMUM_FUZZY_LENGTH:
        return None
    limit = int(len(base) * _REVISION_RATIO)
    distance, end = _alignment(base[common:], text[common:], free_start=False)
    return common + end if distance <= limit else None


def _substring_alignment(pattern: List[str], text: List[str]) -> Optional[int]:
    """`pattern` が `text` のどこかに許容範囲内の編集量で現れれば、その終わり位置を返す（最も後ろの一致）。"""
    limit = 0 if len(pattern) < _MINIMUM_FUZZY_LENGTH else int(len(pattern) * _REVISION_RATIO)
    distance, end = _alignment(pattern, text, free_start=True)
    return end if distance <= limit else None


def _alignment(pattern: List[str], text: List[str], free_start: bool) -> Tuple[int, int]:
    """
    半大域アラインメントの編集距離（`pattern` は全体、`text` は末尾が自由）。

    Args:
        pattern: 全体を合わせる側
        text: 合わせられる側
        free_start: `text` の先頭も自由にする（途中に現れるのを探す）

    Returns:
        最小の編集量と、そのときの `text` 側の終わり位置（同点なら後ろ）
    """
    n, m = len(pattern), len(text)
    if n == 0:
        return 0, 0
    # previous[j] = pattern の先頭 i 文字を text の j 文字目までに合わせる最小編集量
    previous = [0 if free_start else j for j in range(m + 1)]
    for i in range(1, n + 1):
        current = [i] + [0] * m
        for j in range(1, m + 1):
            substitution = previous[j - 1] + (0 if pattern[i - 1] == text[j - 1] else 1)
            current[j] = min(substitution, previous[j] + 1, current[j - 1] + 1)
        previous = current
    best = (previous[0], 0)
    for j in range(1, m + 1):
        if previous[j] <= best[0]:
            best = (previous[j], j)
    return best
