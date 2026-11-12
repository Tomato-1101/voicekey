"""
音声入力 1 回分の計測モジュール

キーを押してから貼り付けが終わるまでの各段の時間を集め、ログへ `[計測]` で始まる 1 行を出す。
「遅い」と感じたときに、どの段（録音の停止・文字起こし・整形・翻訳・貼り付け）で時間を使ったかを
ログだけで切り分けるためのもの。本文と API キーは一切入れない（文字数と時間だけ）。

項目の名前と並びは Mac 版 `DictationTimeline.summaryLine()` と同じにしてある
（同じ集計スクリプト・同じ grep で両方のログを読めるようにするため）。
Windows で取れない項目は `-` を出す。
"""

from dataclasses import dataclass
from typing import Optional

# 結果の表記（Mac 版 DictationTimeline.Outcome と同じ文言）
OUTCOME_PASTED = "貼付"
OUTCOME_PASTE_FAILED = "貼付失敗"
OUTCOME_TOO_SHORT = "短すぎ"
OUTCOME_NO_SPEECH = "無音"
OUTCOME_EMPTY_RESULT = "空結果"
OUTCOME_TRANSCRIBE_FAILED = "文字起こし失敗"
OUTCOME_NO_CONTEXT = "コンテキストなし"
OUTCOME_UNFINISHED = "未完了"


def _ms(value: Optional[float]) -> str:
    """ミリ秒の値を `123ms` の形にする（無ければ `-`）。"""
    return "-" if value is None else f"{int(value)}ms"


def _span(start: Optional[float], end: Optional[float]) -> str:
    """2 つの時刻（秒）の差を `123ms` の形にする（どちらか無ければ `-`）。"""
    if start is None or end is None:
        return "-"
    return f"{int((end - start) * 1000)}ms"


def route(backend: str, had_streamer: bool, streamed_text: bool) -> str:
    """
    文字起こしの経路を分類する（Mac 版 DictationTimeline.route と同じ分け方）。

    Args:
        backend: 録音開始時のエンジン id
        had_streamer: 録音しながら送る接続を張れていたか
        streamed_text: その接続の確定が空でなかったか

    Returns:
        "streaming"（録音しながら送って確定）/ "replay"（Soniox へ録音全体を送り直し）/
        "fallback"（ライブが空で録音後にまとめて送った）/ "rest"（録音後にまとめて送る）
    """
    if had_streamer:
        if streamed_text:
            return "streaming"
        return "replay" if backend == "soniox" else "fallback"
    return "replay" if backend == "soniox" else "rest"


@dataclass
class DictationTimeline:
    """
    音声入力 1 回分の計測値。

    時刻はすべて time.perf_counter() の秒。取れなかった項目は None のままにする
    （0 を入れると「0ms だった」と読めてしまうため）。

    Attributes:
        generation: 録音の通し番号（0 は不明）
        slot: ホットキースロット ID
        backend: エンジン id
        model: モデル名
        pressed_at: 録音キーを押した時刻
        route: 文字起こしの経路（route() の戻り値）
        ending: ライブ接続の終わり方（"completed" / "failed" / "timeout"。ライブでなければ None）
        outcome: 結果（OUTCOME_* のどれか）
        record_started_at: マイクが実際に録り始めた時刻
        recording_sec: 録音の長さ（秒）
        released_at: 録音キーを離した（録音の停止を頼んだ）時刻
        stopped_at: 録音が止まって音声が手元に揃った時刻
        finalized_at: 確定テキストが手元に揃った時刻
        stt_ms: 文字起こしにかかった時間
        vad_ms: 発話区間の検出にかかった時間
        format_ms: 整形にかかった時間
        translate_ms: 翻訳にかかった時間
        paste_ms: 貼り付けにかかった時間
        pasted_at: 貼り付けが終わった時刻
        characters: 貼り付けた文字数
        target: 貼り付け先アプリ（実行ファイル名。本文やウィンドウ名は入れない）
        auto_enter: 貼り付け後に Enter を送る回か
    """

    generation: int = 0
    slot: int = 0
    backend: str = ""
    model: str = ""
    pressed_at: Optional[float] = None
    route: Optional[str] = None
    ending: Optional[str] = None
    outcome: str = OUTCOME_UNFINISHED
    record_started_at: Optional[float] = None
    recording_sec: Optional[float] = None
    released_at: Optional[float] = None
    stopped_at: Optional[float] = None
    finalized_at: Optional[float] = None
    stt_ms: Optional[float] = None
    vad_ms: Optional[float] = None
    format_ms: Optional[float] = None
    translate_ms: Optional[float] = None
    paste_ms: Optional[float] = None
    pasted_at: Optional[float] = None
    characters: Optional[int] = None
    target: Optional[str] = None
    auto_enter: bool = False

    def summary_line(self) -> str:
        """
        `[計測]` の 1 行を作る（副作用なし）。

        項目の順番は固定（grep や集計が位置に頼れるように）。

        Returns:
            ログへ出す 1 行
        """
        recording = "-" if self.recording_sec is None else f"{self.recording_sec:.2f}s"
        fields = [
            f"gen={self.generation if self.generation else '-'}",
            f"slot={self.slot}",
            f"backend={self.backend}",
            f"model={self.model or '-'}",
            f"経路={self.route or '-'}",
            f"終わり方={self.ending or '-'}",
            f"結果={self.outcome}",
            f"押下→録音開始={_span(self.pressed_at, self.record_started_at)}",
            # 最初の音声が届いた時刻は Windows の録音部から取っていない
            "押下→最初の音声=-",
            f"録音={recording}",
            f"離鍵→確定={_span(self.released_at, self.finalized_at)}",
            f"離鍵→停止={_span(self.released_at, self.stopped_at)}",
            f"停止→確定={_span(self.stopped_at, self.finalized_at)}",
            f"STT={_ms(self.stt_ms)}",
            f"VAD={_ms(self.vad_ms)}",
            f"整形={_ms(self.format_ms)}",
            f"翻訳={_ms(self.translate_ms)}",
            f"貼付={_ms(self.paste_ms)}",
            f"離鍵→貼付完了={_span(self.released_at, self.pasted_at)}",
            f"押下→貼付完了={_span(self.pressed_at, self.pasted_at)}",
            f"文字数={'-' if self.characters is None else self.characters}",
            f"貼付先={self.target or '-'}",
            f"auto_enter={'true' if self.auto_enter else 'false'}",
        ]
        return "[計測] " + " ".join(fields)
