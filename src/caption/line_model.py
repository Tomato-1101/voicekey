"""
字幕の行の管理（純ロジック）— Mac 版 CaptionLiveLinePolicy.swift と CaptionHUDController の行管理の移植

画面（Qt）から切り離して、時刻を渡して単体テストできるようにしてある。表示側（caption_hud.py）は
ここが返す行を描くだけ。

恒久要件:
- ロール表示（確定 2 行＋ライブ行）。確定行が最低表示時間を満たすまで 3 行目として残っている間は、
  ライブ行を出さずに 3 段に収める（4 段目を出すと映像を隠しすぎる）。
- 確定行は `max(3 秒, 文字数 × 0.15 秒)`（上限 12 秒）の最低表示時間を保証する。読み終わったら余韻 1 秒で自分から退場。
- 鮮度優先で捨ててよいのはライブ行（途中経過）だけ。確定文は捨てない。
- 何も届かないまま 8 秒たったら全体を畳む。
"""

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

# 画面に残す確定行の数（ライブ行と合わせて最大 3 段）
MAX_CONFIRMED_LINES = 2
# 最低表示時間を満たしていない行を待つときの上限行数（際限なく伸びないようにする）
HARD_MAX_CONFIRMED_LINES = 3
# 確定行の最低表示時間の下限・1 文字あたりの読む時間・上限
MINIMUM_LINE_DURATION = 3.0
READING_SECONDS_PER_CHARACTER = 0.15
MAXIMUM_LINE_DURATION = 12.0
# 読み終わってから自分で退場するまでの猶予
SELF_RETIRE_GRACE = 1.0
# 何も届かないまま全体を畳むまで
AUTO_HIDE_DELAY = 8.0
# 画面に出す行数の上限（確定行＋ライブ行）
MAX_VISIBLE_LINES = 3
# 短い確定行を直前の行へまとめるときの長さ
SHORT_LINE_CHARACTERS = 24
MAX_MERGED_CHARACTERS = 48

KIND_CONFIRMED = "confirmed"
KIND_SOURCE_ONLY = "sourceOnly"
KIND_NOTICE = "notice"
KIND_LIVE = "live"

# ライブ行に届く更新の種類
LISTENING = "listening"          # 認識途中の原文
PENDING_SOURCE = "pendingSource"  # 確定して翻訳待ちになった原文
PARTIAL = "partial"              # 認識途中の暫定訳
TRANSCRIPT = "transcript"        # 文字起こしモードの認識途中


def minimum_duration(text: str) -> float:
    """読むのに要る時間の見積り（短い相槌でも 3 秒・長文は上限 12 秒）。"""
    return min(max(MINIMUM_LINE_DURATION, len(text) * READING_SECONDS_PER_CHARACTER), MAXIMUM_LINE_DURATION)


def accepts_live_update(kind: str, japanese: str, source: str,
                        current: Optional[Tuple[str, str]], last_confirmed: Optional[Tuple[str, str]]) -> bool:
    """この更新をライブ行に反映してよいか（巻き戻り・重複の防止・Mac の CaptionLiveLinePolicy.accepts）。

    Args:
        kind: 更新の種類（LISTENING / PENDING_SOURCE / PARTIAL / TRANSCRIPT）
        japanese / source: 更新の中身（訳 / 原文。文字起こしは japanese に本文）
        current: いまのライブ行 (japanese, source)
        last_confirmed: 直前の確定行 (japanese, source)
    """
    # 確定した直後に同じ発話の途中結果が届くと、同じ文が確定行とライブ行に 2 行並ぶ
    if last_confirmed is not None:
        last_japanese, last_source = last_confirmed
        if kind == PARTIAL and japanese == last_japanese:
            return False
        if kind in (LISTENING, PENDING_SOURCE) and source == last_source:
            return False
        if kind == TRANSCRIPT and japanese == last_japanese:
            return False
    if kind == PENDING_SOURCE:
        # 翻訳待ちの原文は「確定が溜まっていた古い文」。新しい話が出ているところへ上書きすると巻き戻る
        return current is None or (not current[0] and not current[1])
    if kind == LISTENING:
        # 暫定訳が出ている間は生の原文で上書きしない（訳→原文→訳 とちらつくため）
        return current is None or not current[0]
    return True


def join_text(first: str, second: str) -> str:
    """2 つの本文をつなぐ（境目が英数字・記号なら空白を挟む。日本語どうしはそのままつなぐ）。"""
    if not first:
        return second
    if not second:
        return first
    tail, head = first[-1], second[0]
    needs_space = (tail.isascii() and not tail.isspace()) or (head.isascii() and not head.isspace())
    return f"{first} {second}" if needs_space else first + second


def should_merge(new_text: str, last_text: Optional[str], active_count: int,
                 oldest_has_met_minimum: bool, max_lines: int = MAX_CONFIRMED_LINES) -> bool:
    """新しい確定行を直前の行へまとめるか（行数の上限で読み終わっていない行を早く押し出さないため）。"""
    if not last_text or not new_text:
        return False
    if active_count < max_lines or oldest_has_met_minimum:
        return False
    if not (len(new_text) < SHORT_LINE_CHARACTERS or len(last_text) < SHORT_LINE_CHARACTERS):
        return False
    return len(join_text(last_text, new_text)) <= MAX_MERGED_CHARACTERS


def composed_text(parts: List[Tuple[str, str]]) -> str:
    """まとめた行の本文を各文の (訳, 原文) から組み立てる（訳が無い文は原文で埋める）。"""
    if not any(japanese for japanese, _ in parts):
        return ""
    text = ""
    for japanese, source in parts:
        text = join_text(text, japanese or source)
    return text


@dataclass
class Line:
    """字幕の 1 行。"""

    kind: str
    japanese: str
    source: str
    shown_at: float = 0.0
    min_duration: float = 0.0
    # まとめた行の各文 (採番, 訳, 原文)
    parts: List[Tuple[Optional[int], str, str]] = field(default_factory=list)

    @property
    def text(self) -> str:
        return self.japanese or self.source


class CaptionLineModel:
    """確定行・ライブ行の積み上げと退場を管理する（時刻は呼び出し側から渡す）。"""

    def __init__(self) -> None:
        self.confirmed: List[Line] = []
        self.live: Optional[Line] = None
        self.last_update_at: Optional[float] = None

    # --- 更新 ---

    def show_listening(self, text: str, now: float) -> bool:
        return bool(text) and self._update_live(LISTENING, "", text, now)

    def show_pending_source(self, text: str, now: float) -> bool:
        return self._update_live(PENDING_SOURCE, "", text, now)

    def show_live_transcript(self, text: str, now: float) -> bool:
        return bool(text) and self._update_live(TRANSCRIPT, text, "", now)

    def show_partial(self, source: str, japanese: str, now: float) -> bool:
        return self._update_live(PARTIAL, japanese, source, now)

    def show_translation(self, source: str, japanese: str, now: float, sequence: Optional[int] = None) -> None:
        self._append_confirmed(KIND_CONFIRMED, japanese, source, sequence, now)

    def show_transcript(self, text: str, now: float) -> None:
        """文字起こしモードの確定文（訳文の行と同じ大きい文字で出したいので japanese に入れる）。"""
        self._append_confirmed(KIND_CONFIRMED, text, "", None, now)

    def show_source_only(self, text: str, now: float) -> None:
        """翻訳できなかった原文だけを確定行として積む（落とさない）。"""
        self._append_confirmed(KIND_SOURCE_ONLY, "", text, None, now)

    def show_notice(self, text: str, now: float) -> None:
        """字幕以外のお知らせ（何をしていて字幕が出ないのか）。"""
        self.confirmed = [Line(KIND_NOTICE, text, "", now, MINIMUM_LINE_DURATION)]
        self.live = None
        self.last_update_at = now

    def clear(self) -> None:
        self.confirmed = []
        self.live = None
        self.last_update_at = None

    # --- 時間経過 ---

    def tick(self, now: float) -> bool:
        """時間経過を反映する（読了した行の退場・溢れた行の押し出し・全体の自動非表示）。変わったら True。"""
        changed = False
        if self.last_update_at is not None and now - self.last_update_at >= AUTO_HIDE_DELAY:
            if self.confirmed or self.live:
                changed = True
            self.clear()
            return changed
        kept = []
        for line in self.confirmed:
            if now - line.shown_at >= line.min_duration + SELF_RETIRE_GRACE:
                changed = True
                continue
            kept.append(line)
        self.confirmed = kept
        if self._evict_overflow(now):
            changed = True
        return changed

    @property
    def visible_lines(self) -> List[Line]:
        """下から順に並ぶ行（最後の要素が最下段）。確定行が 3 行残っている間はライブ行を出さない。"""
        if self.live is not None and len(self.confirmed) < MAX_VISIBLE_LINES:
            return self.confirmed + [self.live]
        return list(self.confirmed)

    @property
    def is_empty(self) -> bool:
        return not self.confirmed and self.live is None

    # --- 内部 ---

    def _update_live(self, kind: str, japanese: str, source: str, now: float) -> bool:
        current = (self.live.japanese, self.live.source) if self.live else None
        last = (self.confirmed[-1].japanese, self.confirmed[-1].source) if self.confirmed else None
        if not accepts_live_update(kind, japanese, source, current, last):
            return False
        if self.live is None:
            self.live = Line(KIND_LIVE, japanese, source, now, 0.0)
        else:
            self.live.japanese = japanese
            self.live.source = source
        self.last_update_at = now
        return True

    def _append_confirmed(self, kind: str, japanese: str, source: str, sequence: Optional[int], now: float) -> None:
        # お知らせだけが出ている状態から字幕に戻るときは、お知らせを片付ける
        self.confirmed = [line for line in self.confirmed if line.kind != KIND_NOTICE]
        # この文のライブ行は確定行に置き換わるので畳む
        self.live = None
        self.last_update_at = now
        text = japanese or source
        if self._merge_into_last(kind, japanese, source, sequence, text, now):
            return
        line = Line(kind, japanese, source, now, minimum_duration(text), [(sequence, japanese, source)])
        self.confirmed.append(line)
        self._evict_overflow(now)

    def _merge_into_last(self, kind, japanese, source, sequence, text, now) -> bool:
        if not self.confirmed:
            return False
        last = self.confirmed[-1]
        oldest = self.confirmed[0]
        if last.kind != kind or kind == KIND_NOTICE:
            return False
        if not should_merge(text, last.text, len(self.confirmed),
                            now - oldest.shown_at >= oldest.min_duration):
            return False
        last.parts.append((sequence, japanese, source))
        # 前の文の訳がまだ空でも、その原文が画面から消えないよう各文から組み直す
        last.japanese = composed_text([(j, s) for _, j, s in last.parts])
        last.source = join_text(last.source, source)
        elapsed = now - last.shown_at
        last.min_duration = max(last.min_duration, elapsed + minimum_duration(text), minimum_duration(last.text))
        return True

    def _evict_overflow(self, now: float) -> bool:
        """行数の上限を超えた分を消す（最低表示時間に満たない行は 1 行だけ上限を超えて残す）。"""
        changed = False
        while len(self.confirmed) > MAX_CONFIRMED_LINES:
            oldest = self.confirmed[0]
            met = now - oldest.shown_at >= oldest.min_duration
            if not met and len(self.confirmed) <= HARD_MAX_CONFIRMED_LINES:
                break
            self.confirmed.pop(0)
            changed = True
        return changed
