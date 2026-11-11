"""
議事録の保存と読み出しのテスト

保存形式は Mac 版と 1 文字も違わないこと（同じフォルダを両 OS で開いても同じに読める）を、
出来上がったファイルの中身そのもので確かめる。
"""

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from src.meetbot import transcript
from src.meetbot.transcript import TranscriptRecorder


class TranscriptRecorderTests(unittest.TestCase):
    """書く側（1 行ずつ追記）。"""

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.directory = Path(self._temp.name) / "transcripts"
        self.start = datetime(2026, 10, 11, 14, 5, 7)

    def test_file_matches_mac_format(self):
        """ヘッダ・話者あり・話者なしの行が Mac 版と同じ文字列になる。"""
        recorder = TranscriptRecorder(self.directory)
        self.assertTrue(recorder.begin("abc-defg-hij", "Google Meet / 定例", "Meet の字幕（日本語）", self.start))
        self.assertEqual(recorder.path.name, "2026-10-11_140507-abc-defg-hij.md")
        self.assertTrue(recorder.path.exists(), "参加しただけでファイルができること")
        self.assertEqual(recorder.lines, 0)

        self.assertEqual(recorder.append("よろしくお願いします", "山田", self.start + timedelta(seconds=3)), "14:05:10")
        self.assertEqual(recorder.append("  話者の分からない発言 ", None, self.start + timedelta(seconds=8)), "14:05:15")
        summary = recorder.end()

        self.assertEqual(summary.lines, 2)
        self.assertIsNone(summary.error)
        self.assertEqual(
            summary.path.read_bytes().decode("utf-8"),
            "# 議事録 2026-10-11 14:05\n"
            "\n"
            "- 記録元: Google Meet / 定例\n"
            "- 言語: Meet の字幕（日本語）\n"
            "- `14:05:10` **山田**: よろしくお願いします\n"
            "- `14:05:15` 話者の分からない発言\n",
        )

    def test_ignores_immediate_duplicate_but_keeps_other_speaker(self):
        """同じ話者の同じ文の読み直しは弾くが、別の人の同じ言葉は残す。"""
        recorder = TranscriptRecorder(self.directory)
        recorder.begin("abc-defg-hij", "Google Meet / 定例", "Meet の字幕（日本語）", self.start)
        self.assertIsNotNone(recorder.append("はい", "山田", self.start))
        self.assertIsNone(recorder.append("はい", "山田", self.start + timedelta(seconds=2)))
        self.assertIsNotNone(recorder.append("はい", "佐藤", self.start + timedelta(seconds=3)))
        self.assertIsNotNone(recorder.append("はい", "佐藤", self.start + timedelta(seconds=30)))
        self.assertIsNone(recorder.append("   ", "佐藤", self.start + timedelta(seconds=31)))
        self.assertEqual(recorder.lines, 3)

    def test_meeting_keeps_one_file_across_long_silence(self):
        """会議中は時間があいても 1 ファイルのまま。"""
        recorder = TranscriptRecorder(self.directory)
        recorder.begin("abc-defg-hij", "Google Meet / 定例", "Meet の字幕（日本語）", self.start)
        first = recorder.path
        recorder.append("冒頭の発言", None, self.start)
        recorder.append("20 分後の発言", None, self.start + timedelta(minutes=20))
        self.assertEqual(recorder.path, first)
        self.assertEqual(len(list(self.directory.iterdir())), 1)

    def test_same_second_gets_separate_files(self):
        """同じ秒に始めた 2 つの記録は、別のファイルになる（上書きしない）。"""
        first = TranscriptRecorder(self.directory)
        second = TranscriptRecorder(self.directory)
        first.begin("abc-defg-hij", "Google Meet / A", "Meet の字幕（日本語）", self.start)
        second.begin("abc-defg-hij", "Google Meet / B", "Meet の字幕（日本語）", self.start)
        self.assertNotEqual(first.path, second.path)
        self.assertEqual(second.path.name, "2026-10-11_140507-abc-defg-hij-2.md")
        self.assertIn("Google Meet / A", first.path.read_text(encoding="utf-8"))

    def test_file_base_name_sanitizes_code(self):
        """会議コードからファイル名に使えない文字を落とす。"""
        name = transcript.file_base_name(self.start, "../abc/def?x=1")
        self.assertNotIn("/", name)
        self.assertNotIn("?", name)
        self.assertTrue(name.endswith("-abcdefx1"), name)
        self.assertEqual(transcript.file_base_name(self.start, None), "2026-10-11_140507")

    def test_write_failure_is_reported(self):
        """保存先を作れないときは、失敗として理由が残る（黙って捨てない）。"""
        blocker = Path(self._temp.name) / "blocked"
        blocker.write_text("x", encoding="utf-8")
        recorder = TranscriptRecorder(blocker)
        self.assertFalse(recorder.begin("abc-defg-hij", "Google Meet / 定例", "Meet の字幕（日本語）", self.start))
        self.assertIsNone(recorder.append("書けない発言", None, self.start))
        summary = recorder.end()
        self.assertIsNone(summary.path)
        self.assertEqual(summary.lines, 0)
        self.assertTrue(summary.error)


class TranscriptLibraryTests(unittest.TestCase):
    """読む側（一覧・本文・要約）。"""

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.directory = Path(self._temp.name)

    def _write(self, name, body):
        path = self.directory / name
        path.write_text(body, encoding="utf-8")
        return path

    def test_lists_newest_first_with_summary_flag(self):
        """新しい順に並び、要約の有無・行数・字数が付く。要約ファイルは一覧に出ない。"""
        header = "# 議事録 2026-10-11 14:05\n\n- 記録元: Google Meet / 定例\n- 言語: Meet の字幕（日本語）\n\n"
        old = self._write("2026-10-10_090000-abc-defg-hij.md", header + "- `09:00:01` **山田**: おはよう\n")
        new = self._write(
            "2026-10-11_140507-xyz-abcd-efg-2.md",
            header + "- `14:05:10` **山田**: 一つ目\n- `14:05:15` 二つ目です\n",
        )
        self._write("2026-08-28_1432.md", header)
        self._write("メモ.txt", "議事録ではない")
        self.assertTrue(transcript.write_summary(new, "## 要点\n- まとめ"))

        infos = transcript.list_transcripts(self.directory)
        self.assertEqual([info.path for info in infos], [new, old, self.directory / "2026-08-28_1432.md"])
        self.assertEqual(infos[0].date, datetime(2026, 10, 11, 14, 5, 7))
        self.assertEqual(infos[0].meeting_code, "xyz-abcd-efg")
        self.assertEqual((infos[0].line_count, infos[0].character_count), (2, 8))
        self.assertTrue(infos[0].has_summary)
        self.assertFalse(infos[1].has_summary)
        self.assertEqual(infos[0].title, "2026/10/11 14:05")
        self.assertEqual(infos[0].detail, "xyz-abcd-efg・2 行・8 字")
        self.assertEqual(infos[2].date, datetime(2026, 8, 28, 14, 32))
        self.assertIsNone(infos[2].meeting_code)

    def test_missing_directory_is_empty(self):
        """フォルダがまだ無いときは空の一覧。"""
        self.assertEqual(transcript.list_transcripts(self.directory / "none"), [])

    def test_parse_lines_reads_speaker_and_time(self):
        """本文から発言の行だけを取り出す（ヘッダは発言ではない）。"""
        lines = transcript.parse_lines(
            "# 議事録 2026-10-11 14:05\n\n- 記録元: Google Meet / 定例\n- 言語: 日本語\n\n"
            "- `14:05:10` **山田**: **強調**: を含む発言\n- `14:05:15` 話者なし\n"
        )
        self.assertEqual(
            [(line.id, line.time, line.speaker, line.text) for line in lines],
            [(1, "14:05:10", "山田", "**強調**: を含む発言"), (2, "14:05:15", None, "話者なし")],
        )

    def test_summary_round_trip(self):
        """要約は議事録の隣の `.summary.md` に保存され、読み戻せる。"""
        path = self._write("2026-10-11_140507.md", "# 議事録\n")
        self.assertIsNone(transcript.read_summary(path))
        self.assertTrue(transcript.write_summary(path, "## 要点\n- A"))
        self.assertEqual(transcript.summary_path(path).name, "2026-10-11_140507.summary.md")
        self.assertEqual(transcript.read_summary(path), "## 要点\n- A")
        self.assertEqual(transcript.read_text(path), "# 議事録\n")
        self.assertIsNone(transcript.read_text(self.directory / "none.md"))
        self.assertEqual([p.name for p in self.directory.iterdir() if p.name.endswith(".tmp")], [])


if __name__ == "__main__":
    unittest.main()
