"""
文字起こしのローカル保存（議事録）— Mac 版 TranscriptRecorder.swift の移植

ライブ字幕・Meet ボットが確定させたテキストを、その場で Markdown ファイルへ**追記**する。
**保存するのは文字起こしだけ**（訳文は保存しない）。
メモリに貯めて終了時に一括保存しない（会議は数十分〜数時間続き、途中で落ちたら全部消えるため）。

ファイルの区切り方は 2 通り:
- ライブ字幕: 5 分以上あいたら別の会議とみなして新しいファイルにする。
- Meet ボット: `begin_meeting` から `end_session` まで（参加から退出まで）を必ず 1 ファイルにする。

保存先は `Documents/voicekey/transcripts/YYYY-MM-DD_HHmmss[-会議コード].md`（同じ秒なら -2, -3…）。
書き込みは専用の 1 本のスレッドで順に行う（音声スレッドから呼ばれても待たせない）。
"""

import os
import queue
import re
import sys
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional, Tuple

from ..utils.logger import get_logger

logger = get_logger(__name__)

# この間隔以上あいたら「別の会議」とみなして新しいファイルにする（ライブ字幕のときだけ・Mac と同じ 5 分）
SESSION_GAP_SECONDS = 5 * 60
# 同じ確定文が二重に届いたときに弾く間隔（秒）
DUPLICATE_WINDOW_SECONDS = 10


def documents_dir() -> Path:
    """ユーザーの「ドキュメント」フォルダ（OneDrive へ移されていても実際の場所を返す）。"""
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            buf = ctypes.create_unicode_buffer(wintypes.MAX_PATH)
            CSIDL_PERSONAL = 5
            if ctypes.windll.shell32.SHGetFolderPathW(None, CSIDL_PERSONAL, None, 0, buf) == 0 and buf.value:
                return Path(buf.value)
        except Exception:
            pass
    return Path.home() / "Documents"


def transcripts_dir() -> Path:
    """議事録の保存先（Documents/voicekey/transcripts）。"""
    return documents_dir() / "voicekey" / "transcripts"


def file_base_name(when: datetime, code: Optional[str]) -> str:
    """ファイル名の本体（`2026-10-11_140512` / 会議なら `2026-10-11_140512-abc-defg-hij`）。

    会議コードは URL から来るので、ファイル名に使えない文字を落として長さも抑える。
    """
    stamp = when.strftime("%Y-%m-%d_%H%M%S")
    safe = re.sub(r"[^A-Za-z0-9-]", "", code or "")[:40]
    return f"{stamp}-{safe}" if safe else stamp


def format_line(text: str, speaker: Optional[str], when: datetime) -> str:
    """1 行分の Markdown（`- `14:32:05` **話者**: 本文`）。"""
    time_text = when.strftime("%H:%M:%S")
    if speaker:
        return f"- `{time_text}` **{speaker}**: {text}\n"
    return f"- `{time_text}` {text}\n"


def latest_file(directory: Optional[Path] = None) -> Optional[Path]:
    """保存先にある一番新しい議事録（無ければ None）。"""
    folder = directory or transcripts_dir()
    try:
        files = [p for p in folder.iterdir() if p.suffix == ".md" and not p.name.startswith(".")]
    except Exception:
        return None
    if not files:
        return None
    return max(files, key=lambda p: p.stat().st_mtime)


@dataclass(frozen=True)
class SessionSummary:
    """記録を区切ったときの集計（それまでに頼んだ書き込みが全部終わった後の値）。"""

    path: Optional[Path]
    lines: int
    error: Optional[str]


# 書き込み 1 回の結果: ("written", パス, 行数) / ("failed", 理由, 0)
WriteOutcome = Tuple[str, object, int]


class TranscriptRecorder:
    """確定文をファイルへ追記する記録係（書き込みは専用スレッドで順に行う）。

    Attributes:
        on_write: 書き込みのたびに (結果, 会議の番号) を受け取る（**記録用スレッドから**呼ぶ）。
            以前の Mac 版は失敗を握り潰して画面が「記録中」のままだった。保存できないときは知らせる。
    """

    def __init__(self, directory: Optional[Path] = None) -> None:
        self.directory = Path(directory) if directory is not None else transcripts_dir()
        self._queue: "queue.Queue" = queue.Queue()
        self._current: Optional[Path] = None
        self._last_write_at: Optional[datetime] = None
        self._last_text: Optional[str] = None
        self._last_speaker: Optional[str] = None
        self._lines = 0
        self._last_error: Optional[str] = None
        # 会議（Meet ボット）として記録中なら (code, context, language, tag)
        self._meeting: Optional[Tuple[Optional[str], str, str, int]] = None
        self.on_write: Optional[Callable[[WriteOutcome, int], None]] = None
        self._thread = threading.Thread(target=self._run, daemon=True, name="TranscriptWriter")
        self._thread.start()

    # --- 外から使う口（どのスレッドからでも呼べる） ---

    def begin_meeting(self, code: Optional[str], context: str, language: str,
                      now: Optional[datetime] = None, tag: int = 0) -> None:
        """会議（Meet ボット）の記録を始める（その場でヘッダ付きのファイルを作る）。

        以後は `end_session` まで、間隔が何分あいても同じファイルに追記する。
        """
        self._queue.put(("begin", code, context, language, now or datetime.now(), tag))

    def record(self, text: str, speaker: Optional[str] = None, context: Optional[str] = None,
               language: str = "", now: Optional[datetime] = None) -> None:
        """確定文を 1 件記録する（本文はログに出さない）。"""
        trimmed = (text or "").strip()
        if not trimmed:
            return
        self._queue.put(("record", trimmed, speaker or None, context, language, now or datetime.now()))

    def end_session(self, on_closed: Optional[Callable[[SessionSummary], None]] = None) -> None:
        """記録を区切る（次の追記は新しいファイルになる）。

        on_closed は、それまでに頼んだ書き込みが全部終わった後に**記録用スレッドから**呼ぶ。
        """
        self._queue.put(("end", on_closed))

    def wait_until_written(self, timeout: float = 5.0) -> bool:
        """ここまでに頼んだ書き込みが全部終わるまで待つ（アプリ終了の直前に呼ぶ）。"""
        done = threading.Event()
        self._queue.put(("barrier", done))
        return done.wait(timeout)

    @property
    def current_path(self) -> Optional[Path]:
        return self._current

    @property
    def line_count(self) -> int:
        return self._lines

    @property
    def last_error(self) -> Optional[str]:
        return self._last_error

    # --- 記録用スレッド ---

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            kind = item[0]
            try:
                if kind == "begin":
                    self._do_begin(*item[1:])
                elif kind == "record":
                    self._do_record(*item[1:])
                elif kind == "end":
                    self._do_end(item[1])
                elif kind == "barrier":
                    item[1].set()
            except Exception as e:  # 想定外でも記録係は止めない
                logger.error(f"議事録の処理でエラー: {type(e).__name__}: {e}")

    def _do_begin(self, code, context, language, now, tag) -> None:
        self._close_current()
        self._meeting = (code, context, language, tag)
        # 前の記録の失敗を、この会議の結果として持ち越さない
        self._last_error = None
        try:
            path = self._create_file(now, code, context, language)
            self._notify(("written", path, 0), tag)
        except Exception as e:
            self._report_failure(e)

    def _do_record(self, text, speaker, context, language, now) -> None:
        # 同じ確定文の重複（認識器が同一テキストを再送する / ボットの DOM 再読込）を弾く。
        # 話者も比べる（別の人が続けて「はい」と言ったのは別の発言なので残す）
        if (self._last_text == text and self._last_speaker == speaker and self._last_write_at is not None
                and (now - self._last_write_at).total_seconds() < DUPLICATE_WINDOW_SECONDS):
            return
        try:
            path = self._file_for(now, context, language)
            self._append(path, format_line(text, speaker, now))
            self._last_write_at = now
            self._last_text = text
            self._last_speaker = speaker
            self._lines += 1
            self._last_error = None
            self._notify(("written", path, self._lines), self._tag())
        except Exception as e:
            self._report_failure(e)

    def _do_end(self, on_closed) -> None:
        summary = SessionSummary(self._current, self._lines, self._last_error)
        self._close_current()
        self._meeting = None
        if on_closed is not None:
            try:
                on_closed(summary)
            except Exception:
                pass

    def _tag(self) -> int:
        return self._meeting[3] if self._meeting else 0

    def _notify(self, outcome: WriteOutcome, tag: int) -> None:
        callback = self.on_write
        if callback is not None:
            try:
                callback(outcome, tag)
            except Exception:
                pass

    def _report_failure(self, error: Exception) -> None:
        message = str(error) or type(error).__name__
        self._last_error = message
        logger.error(f"議事録を保存できません: {message}")
        self._notify(("failed", message, 0), self._tag())

    def _close_current(self) -> None:
        if self._current is not None:
            logger.info(f"議事録を閉じました: {self._current.name}")
        self._current = None
        self._last_write_at = None
        self._last_text = None
        self._last_speaker = None
        self._lines = 0

    def _file_for(self, now: datetime, context: Optional[str], language: str) -> Path:
        """追記先のファイルを決める（会議中は同じファイル・ライブ字幕は 5 分あいたら新しいファイル）。"""
        if self._meeting is not None:
            if self._current is not None:
                return self._current
            # 開始時のファイル作成に失敗していたときだけここへ来る（書けるようになったら作る）
            code, ctx, lang, _ = self._meeting
            return self._create_file(now, code, ctx, lang)
        if (self._current is not None and self._last_write_at is not None
                and (now - self._last_write_at).total_seconds() < SESSION_GAP_SECONDS):
            return self._current
        self._close_current()
        return self._create_file(now, None, context, language)

    def _create_file(self, now: datetime, code: Optional[str], context: Optional[str], language: str) -> Path:
        """新しいファイルを作ってヘッダを書く（同名があれば -2, -3 と付ける）。"""
        self.directory.mkdir(parents=True, exist_ok=True)
        header = (
            f"# 議事録 {now.strftime('%Y-%m-%d %H:%M')}\n\n"
            f"- 記録元: {context or 'システム音声'}\n"
            f"- 言語: {language}\n\n"
        ).encode("utf-8")
        base = file_base_name(now, code)
        for attempt in range(1, 101):
            name = base if attempt == 1 else f"{base}-{attempt}"
            path = self.directory / f"{name}.md"
            # 「無ければ作る」を 1 回の操作で行う（字幕とボットが同じ秒に始めても同じファイルを掴まない）
            try:
                fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0))
            except FileExistsError:
                continue
            with os.fdopen(fd, "wb") as handle:
                handle.write(header)
            self._current = path
            self._lines = 0
            logger.info(f"議事録を開始しました: {path.name}")
            return path
        raise OSError("同じ名前の議事録ファイルが多すぎます")

    @staticmethod
    def _append(path: Path, line: str) -> None:
        """ファイル末尾へ 1 行追記する（記録中に消されたら作り直して書く）。"""
        with open(path, "ab") as handle:
            handle.write(line.encode("utf-8"))
