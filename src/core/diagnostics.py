"""
音声入力の診断ログ（Mac 版 DiagnosticLog.swift の DictationTimeline の移植）

1 回の音声入力（押下〜貼り付け or スキップ）の各時点を記録し、`[計測]` 1 行にまとめる。
どの経路（ライブ / REST / Soniox 再送 / フォールバック）で終わっても、スキップ（短すぎ・無音・
空結果・失敗）でも必ず 1 行出すことで、「入らなかった 1 回」をログから取りこぼさない。

話した内容と API キーは出さない（文字数と貼り付け先のアプリ名だけ）。
キーの並びは Mac と同じ固定順（grep・集計スクリプトが位置に頼れるように）。
"""

import threading
import time
from dataclasses import dataclass
from typing import Optional

from ..utils.logger import get_logger

logger = get_logger(__name__)


class Outcome:
    """結果の表記（ログの検索キーになるので Mac と同じ固定文言）。"""

    PASTED = "貼付"
    PASTE_FAILED = "貼付失敗"
    TOO_SHORT = "短すぎ"
    NO_SPEECH = "無音"
    EMPTY_RESULT = "空結果"
    LIVE_EMPTY_COMPLETED = "ライブ空(正常終了)"
    TRANSCRIBE_FAILED = "文字起こし失敗"
    RECORD_START_FAILED = "録音開始失敗"
    ABANDONED = "打ち切り"
    UNFINISHED = "未完了"


_generation_lock = threading.Lock()
_generation = 0


def next_generation() -> int:
    """録音ごとの世代番号を採番する（1 から）。"""
    global _generation
    with _generation_lock:
        _generation += 1
        return _generation


def _ms(start: Optional[float], end: Optional[float]) -> str:
    """2 時点の差を ms 表記に（どちらか無ければ "-"）。"""
    if start is None or end is None:
        return "-"
    return f"{max(0.0, end - start) * 1000:.0f}ms"


def _ms_value(value: Optional[int]) -> str:
    return "-" if value is None else f"{value}ms"


def route_for(backend: str, had_streamer: bool, streamed_text: bool) -> str:
    """文字起こしの経路を分類する（Mac の DictationTimeline.route と同じ・純関数）。"""
    if had_streamer:
        if streamed_text:
            return "streaming"
        # ライブが空で REST へ回った。Soniox は録音全体の再送、それ以外は REST へのフォールバック
        return "replay" if backend == "soniox" else "fallback"
    return "replay" if backend == "soniox" else "rest"


@dataclass
class DictationTimeline:
    """1 回の音声入力の各時点（時刻は time.monotonic() の秒）。"""

    generation: int
    slot: int
    backend: str
    model: str
    pressed_at: float
    route: Optional[str] = None
    ending: Optional[str] = None
    outcome: str = Outcome.UNFINISHED
    record_started_at: Optional[float] = None
    first_audio_at: Optional[float] = None
    recording_sec: Optional[float] = None
    released_at: Optional[float] = None
    stopped_at: Optional[float] = None
    finalized_at: Optional[float] = None
    stt_ms: Optional[int] = None
    vad_ms: Optional[int] = None
    format_ms: Optional[int] = None
    # 整形を省いた理由（例: ai_destination.SKIPPED_LABEL）。入っていれば整形欄に時間の代わりに出す
    format_skip_note: Optional[str] = None
    translate_ms: Optional[int] = None
    paste_ms: Optional[int] = None
    pasted_at: Optional[float] = None
    characters: Optional[int] = None
    target_app: Optional[str] = None
    auto_enter: bool = False
    _emitted: bool = False

    def summary_line(self) -> str:
        """`[計測]` サマリ 1 行（純関数）。キーの順序は Mac と同じ固定順。"""
        recording = f"{self.recording_sec:.2f}s" if self.recording_sec is not None else "-"
        fields = [
            f"gen={self.generation if self.generation > 0 else '-'}",
            f"slot={self.slot}",
            f"backend={self.backend}",
            f"model={self.model or '-'}",
            f"経路={self.route or '-'}",
            f"終わり方={self.ending or '-'}",
            f"結果={self.outcome}",
            f"押下→録音開始={_ms(self.pressed_at, self.record_started_at)}",
            f"押下→最初の音声={_ms(self.pressed_at, self.first_audio_at)}",
            f"録音={recording}",
            f"離鍵→確定={_ms(self.released_at, self.finalized_at)}",
            f"離鍵→停止={_ms(self.released_at, self.stopped_at)}",
            f"停止→確定={_ms(self.stopped_at, self.finalized_at)}",
            f"STT={_ms_value(self.stt_ms)}",
            f"VAD={_ms_value(self.vad_ms)}",
            f"整形={self.format_skip_note or _ms_value(self.format_ms)}",
            f"翻訳={_ms_value(self.translate_ms)}",
            f"貼付={_ms_value(self.paste_ms)}",
            f"離鍵→貼付完了={_ms(self.released_at, self.pasted_at)}",
            f"押下→貼付完了={_ms(self.pressed_at, self.pasted_at)}",
            f"文字数={self.characters if self.characters is not None else '-'}",
            f"貼付先={self.target_app or '-'}",
            f"auto_enter={'true' if self.auto_enter else 'false'}",
        ]
        return "[計測] " + " ".join(fields)

    def emit(self) -> None:
        """`[計測]` 行をログへ出す（1 回の音声入力につき 1 回だけ）。"""
        if self._emitted:
            return
        self._emitted = True
        logger.info(self.summary_line())


def now() -> float:
    """計測に使う時刻（time.monotonic）。"""
    return time.monotonic()
