"""
認識結果 → 翻訳 の流量制御（Mac 版 TranslationCoordinator.swift の移植）

音声認識の確定断片はしばしば短すぎる（"So," / "and then"）。そのまま毎回 API を叩くと
コストも遅延も無駄なので、文末か一定文字数までバッファしてから 1 回投げる。
翻訳中に次が来たらキューに積み、同時実行は 1 本に絞る（順序が入れ替わると字幕が壊れるため）。

**確定した文は必ず全部訳して順番どおり出す**（恒久要件）。キューが伸びても捨てない。
Windows には Apple の端末内翻訳が無いので、Mac の「Apple の訳を先に出してクラウドで差し替える」
経路と「溢れた行を Apple で訳す」経路は持たない。クラウドの翻訳が失敗した行は原文のまま出す。

区切りポリシーの値は Mac と同じ（変えるときは両方を同時に変える）:
上限 40 字 / 節区切りを探し始める長さ 24 字 / 無音での強制送出 0.25 秒。
**判定順は「長さ → 文末 → 節区切り」**（この順序が肝。音声認識は 40 語超の 1 文をまるごと
1 件の確定として渡してくるので、文末判定を先に置くと刻みが一切効かない）。
"""

import threading
from collections import deque
from dataclasses import dataclass
from typing import Callable, Deque, List, Optional, Tuple

from ..utils.logger import get_logger

logger = get_logger(__name__)

# 文末とみなす記号
SENTENCE_ENDINGS = frozenset(".!?。！？")

# 文末が来なくてもこの文字数を超えたら送る（ハード上限）。
# 英語 40 字 ≒ 日本語 20 字前後 ≒ 字幕 1 行に収まる量（Mac と同じ値・2026-10-12 に速さ重視で 48 → 40）
MAX_BUFFERED_CHARACTERS = 40

# 節区切りを探し始める長さ。これ未満で切ると「単語だけの行」が並んで逆に読みにくい
CLAUSE_BREAK_MINIMUM_CHARACTERS = 24

# 節の切れ目とみなす目印（見つけた目印の**直後**で切る）
CLAUSE_BREAK_MARKERS = (
    ", ", " and ", " but ", " so ", " because ", " then ", " or ", " while ", " when ",
)

# 新しい確定が来ないまま経過したら送る（言い切りで止まった時に字幕が出ないのを防ぐ）
IDLE_FLUSH_INTERVAL = 0.25

# 文脈として持たせる直近ペア数
CONTEXT_PAIR_COUNT = 4


@dataclass(frozen=True)
class TranslationExchange:
    """直近の「原文 → 訳文」ペア（翻訳時の文脈として使う）。"""

    source: str
    translated: str


class SegmentBuffer:
    """確定断片を溜めて、送ってよい区切りを切り出す純ロジック（時刻を持たない）。

    1 回の取り込みで複数の節が出ることがある（長い確定が一度に来たとき）。
    残った端切れは次の確定か `flush()` で出るので、**1 文字も落とさない**。
    """

    def __init__(self) -> None:
        self.buffer = ""

    def submit(self, text: str) -> List[str]:
        """確定テキストを足し、いま送ってよい区切りを返す（残りは溜めたまま）。"""
        cleaned = (text or "").strip()
        if not cleaned:
            return []
        self.buffer = cleaned if not self.buffer else f"{self.buffer} {cleaned}"
        return self._drain()

    def flush(self) -> List[str]:
        """短くても残り全部を出す（停止時・言い切りで黙ったとき）。"""
        segments = self._drain()
        rest = self.buffer.strip()
        self.buffer = ""
        if rest:
            segments.append(rest)
        return segments

    @property
    def has_pending(self) -> bool:
        return bool(self.buffer.strip())

    def reset(self) -> None:
        self.buffer = ""

    # --- 内部 ---

    def _drain(self) -> List[str]:
        segments = []
        while True:
            segment = self._take_flushable_segment()
            if segment is None:
                break
            segments.append(segment)
        if not self.buffer.strip():
            self.buffer = ""
        return segments

    def _take_flushable_segment(self) -> Optional[str]:
        """バッファの先頭から「いま送出してよい部分」を切り出す（溜めておくなら None）。"""
        text = self.buffer.strip()
        if not text:
            return None
        # 1. 上限より長いものは、文末で終わっていても刻む（長さの判定を文末判定より先に行う）
        if len(text) > MAX_BUFFERED_CHARACTERS:
            cut = clause_break_index(text, MAX_BUFFERED_CHARACTERS)
            if cut is None:
                cut = word_break_index(text, MAX_BUFFERED_CHARACTERS)
            if cut is not None:
                return self._split(text, cut)
            # 切れ目がまったく無い（1 単語が異常に長い等）ときだけ、そのまま出す
            self.buffer = ""
            return text
        # 2. 上限内で文末まで来ていれば 1 文としてそのまま出す
        if text[-1] in SENTENCE_ENDINGS:
            self.buffer = ""
            return text
        # 3. ある程度の長さになったら、節の切れ目で先に出す（文末を待たない）
        if len(text) < CLAUSE_BREAK_MINIMUM_CHARACTERS:
            return None
        cut = clause_break_index(text, MAX_BUFFERED_CHARACTERS)
        if cut is None:
            return None
        return self._split(text, cut)

    def _split(self, text: str, cut: int) -> Optional[str]:
        """指定位置で切り、前半を返して後半をバッファに残す。"""
        head = text[:cut].strip()
        if not head:
            return None
        self.buffer = text[cut:].strip()
        return head


def clause_break_index(text: str, limit: int) -> Optional[int]:
    """節の切れ目（目印の直後の位置）を探す。

    候補のうち**上限に収まる範囲でいちばん後ろ**を採る。前寄りで切ると「単語 2〜3 個だけの行」になる。
    """
    best: Optional[int] = None
    for marker in CLAUSE_BREAK_MARKERS:
        start = 0
        while True:
            found = text.find(marker, start)
            if found < 0:
                break
            end = found + len(marker)
            # 短すぎる先頭・上限を超える位置・末尾ちょうど（残りが空）は採らない
            if CLAUSE_BREAK_MINIMUM_CHARACTERS <= end <= limit and end < len(text):
                if best is None or end > best:
                    best = end
            start = end
    return best


def word_break_index(text: str, limit: int) -> Optional[int]:
    """節の切れ目が無いときの保険: 上限内で最後の単語境界（空白の直後）を探す。"""
    best: Optional[int] = None
    for offset, char in enumerate(text):
        if offset > limit:
            break
        if char.isspace() and offset >= CLAUSE_BREAK_MINIMUM_CHARACTERS:
            best = offset + 1
    return best


# 翻訳器の型: (原文, 文脈, 途中経過の通知 or None) -> 訳文。失敗は例外
Translate = Callable[[str, List[TranslationExchange], Optional[Callable[[str], None]]], str]


class TranslationCoordinator:
    """確定文をまとめて翻訳へ流す調停役（スレッド安全）。

    コールバックはすべて翻訳スレッド（または停止を呼んだスレッド）から呼ぶ。UI 側でメインへホップすること。

    Attributes:
        on_source_ready: 区切りがまとまった直後（翻訳前）に (原文)
        on_translated: 翻訳が成功したら (原文, 訳文, 採番)
        on_streaming_delta: ストリーミング翻訳の途中経過 (原文, ここまでの訳, 採番)
        on_failed: 翻訳が失敗したら (原文, ユーザー向け説明)。失敗しても止めない（原文だけ出し続ける）
    """

    def __init__(
        self,
        translator_provider: Callable[[], Optional[Translate]],
        sequence_provider: Optional[Callable[[], int]] = None,
        idle_flush_interval: float = IDLE_FLUSH_INTERVAL,
    ) -> None:
        """
        Args:
            translator_provider: 1 文ごとに翻訳器を返す（設定変更を次の文から効かせるため）
            sequence_provider: 表示順の採番（既定は単調増加）
            idle_flush_interval: 無音での強制送出までの秒数（テストで短くする）
        """
        self._translator_provider = translator_provider
        self._sequence_provider = sequence_provider or self._default_sequence
        self._idle_interval = idle_flush_interval
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._segments = SegmentBuffer()
        self._pending: Deque[Tuple[str, int]] = deque()
        self._history: List[TranslationExchange] = []
        self._idle_timer: Optional[threading.Timer] = None
        self._closed = False
        self._busy = False
        self._seq = 0
        self.on_source_ready: Optional[Callable[[str], None]] = None
        self.on_translated: Optional[Callable[[str, str, int], None]] = None
        self.on_streaming_delta: Optional[Callable[[str, str, int], None]] = None
        self.on_failed: Optional[Callable[[str, str], None]] = None
        self._worker = threading.Thread(target=self._run, daemon=True, name="CaptionTranslate")
        self._worker.start()

    def _default_sequence(self) -> int:
        self._seq += 1
        return self._seq

    # --- 外から使う口 ---

    def submit(self, final_text: str) -> None:
        """確定した認識テキストを渡す。"""
        if not (final_text or "").strip():
            return
        with self._lock:
            if self._closed:
                return
            self._cancel_idle_locked()
            ready = self._segments.submit(final_text)
            for segment in ready:
                self._emit_locked(segment)
            if self._segments.has_pending:
                self._schedule_idle_locked()

    def flush(self) -> None:
        """バッファに残っている分を強制的に流す（停止時など）。"""
        with self._lock:
            self._flush_locked()

    def reset(self) -> None:
        """状態を初期化する（停止 → 再開で前の文脈を引きずらないように）。"""
        with self._lock:
            self._cancel_idle_locked()
            self._segments.reset()
            self._pending.clear()
            self._history.clear()

    def close(self) -> None:
        """翻訳スレッドを止める（残っている翻訳待ちは捨てずに最後まで訳すため、先に flush すること）。"""
        with self._lock:
            self._cancel_idle_locked()
            self._closed = True
            self._cond.notify_all()

    def wait_idle(self, timeout: float) -> bool:
        """翻訳待ちが空になるまで待つ（テスト・停止時用）。"""
        with self._cond:
            return self._cond.wait_for(
                lambda: not self._pending and not self._busy and not self._segments.has_pending,
                timeout=timeout,
            )

    @property
    def history(self) -> List[TranslationExchange]:
        with self._lock:
            return list(self._history)

    # --- 内部（_lock を持って呼ぶ） ---

    def _flush_locked(self) -> None:
        self._cancel_idle_locked()
        for segment in self._segments.flush():
            self._emit_locked(segment)

    def _emit_locked(self, text: str) -> None:
        sequence = self._sequence_provider()
        callback = self.on_source_ready
        if callback is not None:
            try:
                callback(text)
            except Exception:
                pass
        self._pending.append((text, sequence))
        self._cond.notify_all()

    def _schedule_idle_locked(self) -> None:
        self._cancel_idle_locked()
        timer = threading.Timer(self._idle_interval, self._on_idle)
        timer.daemon = True
        self._idle_timer = timer
        timer.start()

    def _cancel_idle_locked(self) -> None:
        if self._idle_timer is not None:
            self._idle_timer.cancel()
            self._idle_timer = None

    def _on_idle(self) -> None:
        # 言い切って黙ったときは短くても出す（無言で待たせない）
        with self._lock:
            if self._closed:
                return
            self._idle_timer = None
            self._flush_locked()

    # --- 翻訳スレッド ---

    def _run(self) -> None:
        while True:
            with self._cond:
                while not self._pending and not self._closed:
                    self._cond.wait()
                if self._closed and not self._pending:
                    return
                text, sequence = self._pending.popleft()
                context = list(self._history[-CONTEXT_PAIR_COUNT:])
                self._busy = True
            try:
                self._translate_one(text, sequence, context)
            finally:
                with self._cond:
                    self._busy = False
                    self._cond.notify_all()

    def _translate_one(self, text: str, sequence: int, context: List[TranslationExchange]) -> None:
        translator = None
        try:
            translator = self._translator_provider()
        except Exception:
            translator = None
        if translator is None:
            self._notify_failed(text, "翻訳エンジンが使えません")
            return
        delta_cb = self.on_streaming_delta

        def on_delta(so_far: str) -> None:
            if delta_cb is not None:
                try:
                    delta_cb(text, so_far, sequence)
                except Exception:
                    pass

        try:
            translated = translator(text, context, on_delta if delta_cb is not None else None)
        except Exception as e:
            message = getattr(e, "user_message", None) or str(e) or type(e).__name__
            logger.warning(f"字幕の翻訳に失敗（原文のまま出します）: {message}")
            self._notify_failed(text, message)
            return
        translated = (translated or "").strip()
        if not translated:
            self._notify_failed(text, "訳文が空でした")
            return
        with self._lock:
            self._history.append(TranslationExchange(text, translated))
            if len(self._history) > CONTEXT_PAIR_COUNT * 2:
                del self._history[: len(self._history) - CONTEXT_PAIR_COUNT * 2]
        callback = self.on_translated
        if callback is not None:
            try:
                # 確定した文の訳は必ず出す。次の行が来ていても捨てない
                callback(text, translated, sequence)
            except Exception:
                pass

    def _notify_failed(self, text: str, message: str) -> None:
        callback = self.on_failed
        if callback is not None:
            try:
                callback(text, message)
            except Exception:
                pass
