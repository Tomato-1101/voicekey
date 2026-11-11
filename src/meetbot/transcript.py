"""
議事録の保存と読み戻し

会議の発言を Markdown で `Documents\\voicekey\\transcripts\\` へ**1 行ずつ追記**する。
メモリに貯めて終了時に一括保存しない（途中でアプリが落ちたら全部消えるため）。

ファイルの形は Mac 版と同じ（Mac で保存した議事録を Windows で開いても、その逆でも読める）:

    # 議事録 2026-11-12 14:05

    - 記録元: Google Meet / 定例会議
    - 言語: Meet の字幕（日本語）
    - `14:05:12` **山田**: おはようございます
    - `14:05:20` 話者が取れなかった発言

ファイル名は `2026-11-12_140512-abc-defg-hij.md`（秒まで＋会議コード。同名があれば `-2` `-3`）。
要約は議事録の隣に `<議事録の名前>.summary.md` で置く。
"""

import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

from ..utils.logger import get_logger

logger = get_logger(__name__)

# 要約ファイルの末尾（`<議事録の名前>.summary.md` で議事録の隣に置く）
SUMMARY_SUFFIX = ".summary.md"

# 同じ本文・同じ話者がこの秒数以内に続いたら重複として書かない（画面の読み直しによる二重書きを弾く）
_DUPLICATE_SECONDS = 10.0


def default_directory() -> Path:
    """
    議事録の保存先（`ドキュメント\\voicekey\\transcripts`）を返す。

    Windows の「ドキュメント」は OneDrive へ移されていることがあるので、決め打ちの
    `~/Documents` ではなく OS に場所を聞く（聞けなければ `~/Documents`）。

    Returns:
        保存先のフォルダ（まだ無いこともある）
    """
    documents = ""
    try:
        from PySide6.QtCore import QStandardPaths

        documents = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation)
    except Exception:
        documents = ""
    base = Path(documents) if documents else Path.home() / "Documents"
    return base / "voicekey" / "transcripts"


# ============================================
# 書く側
# ============================================
def file_base_name(now: datetime, code: Optional[str]) -> str:
    """
    ファイル名の本体を作る（`2026-10-11_140512` / 会議なら `2026-10-11_140512-abc-defg-hij`）。

    Args:
        now: 記録を始めた時刻
        code: 会議コード（URL から来るので、ファイル名に使えない文字を落として長さも抑える）

    Returns:
        拡張子なしのファイル名
    """
    stamp = now.strftime("%Y-%m-%d_%H%M%S")
    safe = "".join(c for c in (code or "") if c.isascii() and (c.isalnum() or c == "-"))[:40]
    return f"{stamp}-{safe}" if safe else stamp


def format_header(now: datetime, context: str, language: str) -> str:
    """
    議事録の先頭に書くヘッダを作る。

    Args:
        now: 記録を始めた時刻
        context: 記録元（「Google Meet / 会議名」など）
        language: 認識言語の表示名

    Returns:
        ヘッダ（末尾は改行 1 つ）
    """
    return f"# 議事録 {now.strftime('%Y-%m-%d %H:%M')}\n\n- 記録元: {context}\n- 言語: {language}\n"


def format_line(text: str, speaker: Optional[str], time: str) -> str:
    """
    1 行分の Markdown を作る。

    Args:
        text: 発言
        speaker: 話者名（取れなかったら None か空）
        time: 時刻（`14:32:05`）

    Returns:
        改行つきの 1 行
    """
    if speaker:
        return f"- `{time}` **{speaker}**: {text}\n"
    return f"- `{time}` {text}\n"


@dataclass(frozen=True)
class SessionSummary:
    """
    記録を閉じた時点の集計。

    Attributes:
        path: 議事録のファイル（作れなかったら None）
        lines: 書いた行数
        error: 最後の書き込みが失敗していれば、その理由
    """

    path: Optional[Path]
    lines: int
    error: Optional[str]


class TranscriptRecorder:
    """
    会議 1 回ぶんの議事録を書く係。

    ボットの作業スレッドだけから使う（同時に 2 か所から呼ばない）。
    書き込みの失敗は例外にせず `last_error` に残す（議事録が書けなくても会議の記録は続け、
    書けるようになったら続きから書く）。

    Attributes:
        path: いま書いているファイル（まだ無ければ None）
        lines: 書いた行数
        last_error: 最後の書き込みが失敗していれば、その理由
    """

    def __init__(self, directory: Optional[Path] = None):
        """
        Args:
            directory: 保存先（省略時は `default_directory()`）
        """
        self._directory = Path(directory) if directory is not None else default_directory()
        self.path: Optional[Path] = None
        self.lines = 0
        self.last_error: Optional[str] = None
        self._code: Optional[str] = None
        self._context = ""
        self._language = ""
        self._last_text: Optional[str] = None
        self._last_speaker: Optional[str] = None
        self._last_write_at: Optional[datetime] = None

    def begin(self, code: Optional[str], context: str, language: str, now: Optional[datetime] = None) -> bool:
        """
        会議の記録を始める。その場でヘッダ付きのファイルを作る（記録中でもすぐ開けるように）。

        Args:
            code: 会議コード（ファイル名に入れる）
            context: 見出しに残す記録元
            language: 認識言語の表示名
            now: 開始時刻（省略時は今）

        Returns:
            ファイルを作れたら True
        """
        self.path = None
        self.lines = 0
        self.last_error = None
        self._code, self._context, self._language = code, context, language
        self._last_text = self._last_speaker = self._last_write_at = None
        try:
            self._create_file(now or datetime.now())
            return True
        except OSError as error:
            self._report_failure(error)
            return False

    def append(self, text: str, speaker: Optional[str] = None, now: Optional[datetime] = None) -> Optional[str]:
        """
        確定した発言を 1 件追記する。

        Args:
            text: 発言
            speaker: 話者名（取れなかったら None）
            now: 記録時刻（省略時は今）

        Returns:
            書いた行の時刻（`14:32:05`）。空・重複・書き込み失敗で書かなかったら None
        """
        trimmed = (text or "").strip()
        if not trimmed:
            return None
        moment = now or datetime.now()
        speaker = speaker or None
        # 同じ確定文の重複（画面の読み直し）を弾く。話者も比べる
        # （別の人が続けて「はい」と言ったのは別の発言なので残す）。
        if (
            self._last_text == trimmed and self._last_speaker == speaker and self._last_write_at is not None
            and (moment - self._last_write_at).total_seconds() < _DUPLICATE_SECONDS
        ):
            return None
        time = moment.strftime("%H:%M:%S")
        try:
            if self.path is None:
                # 開始時のファイル作成に失敗していたときだけここへ来る（書けるようになったら作る）
                self._create_file(moment)
            # 記録中にファイルが消された（エクスプローラーで捨てた等）ときは "a" が作り直す。
            # newline="" は、Windows でも改行を \n のまま書くため（Mac 版と同じ中身にする）。
            with open(self.path, "a", encoding="utf-8", newline="") as file:
                file.write(format_line(trimmed, speaker, time))
        except OSError as error:
            self._report_failure(error)
            return None
        self._last_text, self._last_speaker, self._last_write_at = trimmed, speaker, moment
        self.lines += 1
        self.last_error = None
        return time

    def end(self) -> SessionSummary:
        """
        記録を閉じて集計を返す。

        Returns:
            閉じた時点の集計
        """
        summary = SessionSummary(self.path, self.lines, self.last_error)
        if self.path is not None:
            logger.info(f"議事録を閉じました: {self.path.name}（{self.lines} 行）")
        self.path = None
        self.lines = 0
        self._last_text = self._last_speaker = self._last_write_at = None
        return summary

    def _create_file(self, now: datetime) -> None:
        """
        新しいファイルを作ってヘッダを書く。

        名前は秒まで入れ、それでも同名があれば `-2` `-3` と付ける。「無ければ作る」を 1 回の操作
        （排他作成）で行うので、同じ秒に始まった別の記録と同じファイルを掴まない。

        Raises:
            OSError: フォルダやファイルを作れなかった
        """
        self._directory.mkdir(parents=True, exist_ok=True)
        base = file_base_name(now, self._code)
        for attempt in range(1, 101):
            name = base if attempt == 1 else f"{base}-{attempt}"
            path = self._directory / f"{name}.md"
            try:
                with open(path, "x", encoding="utf-8", newline="") as file:
                    file.write(format_header(now, self._context, self._language))
            except FileExistsError:
                continue
            self.path = path
            self.lines = 0
            logger.info(f"議事録を開始しました: {path.name}")
            return
        raise FileExistsError("同じ名前の議事録が多すぎます")

    def _report_failure(self, error: OSError) -> None:
        """失敗を記録する（理由は画面の状態行に出す）。"""
        self.last_error = error.strerror or str(error)
        logger.error(f"議事録を保存できません: {self.last_error}")


# ============================================
# 読む側（過去の議事録）
# ============================================
@dataclass(frozen=True)
class TranscriptLine:
    """
    議事録の 1 行（1 つの発言）。

    Attributes:
        id: その議事録の中での通し番号（1 始まり）
        time: 時刻（`14:32:05`）
        speaker: 話者名（取れなかった行は None）
        text: 発言
    """

    id: int
    time: str
    speaker: Optional[str]
    text: str


@dataclass(frozen=True)
class TranscriptInfo:
    """
    一覧に出す議事録 1 件。

    Attributes:
        path: 議事録のファイル
        date: 記録を始めた日時（ファイル名から読めなければ更新日時）
        meeting_code: 会議コード（読めなければ None）
        line_count: 発言の行数
        character_count: 発言の文字数（長さの目安）
        has_summary: 要約が保存済みか
    """

    path: Path
    date: datetime
    meeting_code: Optional[str]
    line_count: int
    character_count: int
    has_summary: bool

    @property
    def title(self) -> str:
        """一覧の 1 行目（`2026/11/11 14:05`）。"""
        return self.date.strftime("%Y/%m/%d %H:%M")

    @property
    def detail(self) -> str:
        """一覧の 2 行目（会議コード・行数・長さ）。"""
        parts = [self.meeting_code] if self.meeting_code else []
        parts.append(f"{self.line_count} 行")
        parts.append(f"{self.character_count:,} 字")
        return "・".join(parts)


def is_transcript(path: Path) -> bool:
    """議事録の本体か（要約ファイルや他の種類のファイルは除く）。"""
    name = Path(path).name
    return name.endswith(".md") and not name.endswith(SUMMARY_SUFFIX) and not name.startswith(".")


def summary_path(transcript: Path) -> Path:
    """その議事録の要約の保存先を返す。"""
    transcript = Path(transcript)
    return transcript.with_name(transcript.stem + SUMMARY_SUFFIX)


_STAMP = re.compile(r"^(\d+)(.*)$", re.DOTALL)


def parse_file_name(stem: str) -> Tuple[Optional[datetime], Optional[str]]:
    """
    ファイル名（拡張子なし）から日時と会議コードを読む。

    形は `2026-10-11_140512` / `2026-10-11_140512-abc-defg-hij` / 同名回避の `…-2`。
    以前の Mac 版は分までの `2026-08-28_1432` だったので、それも読む。

    Args:
        stem: 拡張子なしのファイル名

    Returns:
        (日時, 会議コード)。読めなければ (None, None)
    """
    day, separator, tail = stem.partition("_")
    if not separator or len(day) != 10:
        return None, None
    match = _STAMP.match(tail)
    if not match or len(match.group(1)) not in (4, 6):
        return None, None
    digits, rest = match.group(1), match.group(2)
    try:
        date = datetime.strptime(f"{day}_{digits}", "%Y-%m-%d_%H%M%S" if len(digits) == 6 else "%Y-%m-%d_%H%M")
    except ValueError:
        return None, None
    if not rest.startswith("-"):
        return date, None
    pieces = [piece for piece in rest[1:].split("-") if piece]
    # 末尾の `-2` は同名回避の連番。会議コードは英字だけ（abc-defg-hij）なので、
    # 「数字だけ」か「会議コード＋数字」のときに限って落とす
    if pieces and pieces[-1].isdigit() and (len(pieces) == 1 or all(p.isalpha() for p in pieces[:-1])):
        pieces.pop()
    code = "-".join(pieces)
    return date, code or None


def parse_lines(content: str) -> List[TranscriptLine]:
    """
    議事録の本文から発言の行を取り出す。

    行の形は ``- `14:32:05` **山田**: 本文`` か、話者なしの ``- `14:32:05` 本文``。
    見出しやヘッダ（`- 記録元: …`）は発言ではないので飛ばす。

    Args:
        content: 議事録の中身

    Returns:
        発言の行
    """
    lines: List[TranscriptLine] = []
    for raw in content.splitlines():
        if not raw.startswith("- `"):
            continue
        body = raw[3:]
        tick = body.find("`")
        if tick < 0:
            continue
        time = body[:tick]
        rest = body[tick + 1:].lstrip(" ")
        speaker: Optional[str] = None
        if rest.startswith("**"):
            close = rest.find("**: ")
            if close >= 0:
                speaker = rest[2:close]
                rest = rest[close + 4:]
        if not rest:
            continue
        lines.append(TranscriptLine(len(lines) + 1, time, speaker, rest))
    return lines


def list_transcripts(directory: Optional[Path] = None) -> List[TranscriptInfo]:
    """
    保存済みの議事録を新しい順に並べる（ファイルを読むので、件数が多いと時間がかかる）。

    Args:
        directory: 探す場所（省略時は `default_directory()`）

    Returns:
        議事録の一覧（フォルダが無ければ空）
    """
    folder = Path(directory) if directory is not None else default_directory()
    try:
        files = [entry for entry in folder.iterdir() if entry.is_file()]
    except OSError:
        return []
    names = {entry.name for entry in files}
    infos: List[TranscriptInfo] = []
    for path in files:
        if not is_transcript(path):
            continue
        date, code = parse_file_name(path.stem)
        if date is None:
            try:
                date = datetime.fromtimestamp(path.stat().st_mtime)
            except OSError:
                date = datetime.fromtimestamp(0)
        lines = parse_lines(read_text(path) or "")
        infos.append(TranscriptInfo(
            path=path, date=date, meeting_code=code, line_count=len(lines),
            character_count=sum(len(line.text) for line in lines),
            has_summary=summary_path(path).name in names,
        ))
    # 同じ秒に始まった記録も毎回同じ順に並ぶよう、名前でも比べる
    infos.sort(key=lambda info: (info.date, info.path.name), reverse=True)
    return infos


def read_text(path: Path) -> Optional[str]:
    """
    議事録（または要約）の中身を読む。

    Args:
        path: ファイル

    Returns:
        中身（読めなければ None）
    """
    try:
        return Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def read_summary(transcript: Path) -> Optional[str]:
    """
    その議事録の保存済みの要約を読む。

    Args:
        transcript: 議事録のファイル

    Returns:
        要約（まだ無ければ None）
    """
    return read_text(summary_path(transcript))


def write_summary(transcript: Path, text: str) -> bool:
    """
    要約を議事録の隣へ保存する（書きかけのファイルを残さないよう、別名で書いてから置き換える）。

    Args:
        transcript: 議事録のファイル
        text: 要約

    Returns:
        保存できたら True
    """
    target = summary_path(transcript)
    temporary = target.with_name(target.name + ".tmp")
    try:
        with open(temporary, "w", encoding="utf-8", newline="") as file:
            file.write(text)
        os.replace(temporary, target)
        return True
    except OSError as error:
        logger.error(f"要約を保存できません: {error.strerror or error}")
        return False
