"""ロガーのログ出力先解決の検証（#18）。

ログ先を作業ディレクトリ（cwd）依存にしないことを確かめる:
- 相対名は OS 標準ログディレクトリ配下に作成される（cwd ではない）
- 絶対パスはそのまま使われる
- ディレクトリ/ファイルが作れなくてもアプリは止めない（コンソールのみで継続）

logging はプロセス全体で共有のため、各テストでルートロガーのハンドラと
モジュールの設定フラグをリセットして独立させる。
"""

import logging
import logging.handlers
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.utils import logger as logger_module


class _LoggerTestBase(unittest.TestCase):
    def setUp(self):
        # ルートロガーを素の状態へ戻す（前テストのハンドラを除去）
        root = logging.getLogger()
        self._saved_handlers = root.handlers[:]
        self._saved_level = root.level
        for h in root.handlers[:]:
            root.removeHandler(h)
        logger_module._is_configured = False

    def tearDown(self):
        root = logging.getLogger()
        for h in root.handlers[:]:
            h.close()
            root.removeHandler(h)
        for h in self._saved_handlers:
            root.addHandler(h)
        root.setLevel(self._saved_level)
        logger_module._is_configured = False


class TestLogPathResolution(_LoggerTestBase):
    def test_relative_name_goes_to_os_log_dir_not_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp) / "logs"
            with mock.patch.object(
                logger_module, "default_log_dir", return_value=log_dir
            ):
                logger_module.setup_logger(log_file="startup_log.txt")

            # OS 標準ログディレクトリ配下に作成される
            expected = log_dir / "startup_log.txt"
            self.assertTrue(expected.exists())
            # FileHandler の出力先が log_dir 配下＝cwd ではないことを確認
            root = logging.getLogger()
            file_handlers = [
                h for h in root.handlers if isinstance(h, logging.FileHandler)
            ]
            self.assertEqual(len(file_handlers), 1)
            self.assertEqual(
                Path(file_handlers[0].baseFilename).resolve(), expected.resolve()
            )

    def test_absolute_path_used_as_is(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "explicit.log"
            called = []

            def _should_not_call():
                called.append(True)
                return Path(tmp) / "unused"

            with mock.patch.object(
                logger_module, "default_log_dir", side_effect=_should_not_call
            ):
                logger_module.setup_logger(log_file=str(target))

            self.assertTrue(target.exists())
            self.assertEqual(called, [])  # 絶対パスでは log dir 解決を呼ばない

    def test_dir_creation_failure_degrades_to_console(self):
        # mkdir が失敗してもアプリは止めず、ファイルハンドラ無しで継続する
        with mock.patch.object(
            logger_module, "default_log_dir", return_value=Path("/dev/null/nope")
        ):
            logger_module.setup_logger(log_file="x.log")

        root = logging.getLogger()
        file_handlers = [
            h for h in root.handlers if isinstance(h, logging.FileHandler)
        ]
        self.assertEqual(file_handlers, [])  # ファイル出力は付かない
        # コンソール（Stream）ハンドラは存在する
        stream_handlers = [
            h
            for h in root.handlers
            if isinstance(h, logging.StreamHandler)
            and not isinstance(h, logging.FileHandler)
        ]
        self.assertTrue(stream_handlers)

    def test_none_disables_file_output(self):
        logger_module.setup_logger(log_file=None)
        root = logging.getLogger()
        file_handlers = [
            h for h in root.handlers if isinstance(h, logging.FileHandler)
        ]
        self.assertEqual(file_handlers, [])


class TestLogRotation(_LoggerTestBase):
    """日付ローテートと古いログの自動削除（行動ログの保持方針）。"""

    def _setup_in(self, tmp: str) -> logging.handlers.TimedRotatingFileHandler:
        """一時ディレクトリへロガーを設定し、ファイルハンドラーを返す。"""
        with mock.patch.object(
            logger_module, "default_log_dir", return_value=Path(tmp)
        ):
            logger_module.setup_logger(log_file="app.log")
        root = logging.getLogger()
        handlers = [
            h
            for h in root.handlers
            if isinstance(h, logging.handlers.TimedRotatingFileHandler)
        ]
        self.assertEqual(len(handlers), 1)
        return handlers[0]

    def test_uses_daily_rotation_with_14_backups(self):
        with tempfile.TemporaryDirectory() as tmp:
            handler = self._setup_in(tmp)
            self.assertEqual(handler.when, "MIDNIGHT")
            self.assertEqual(handler.backupCount, logger_module.LOG_RETENTION_DAYS)
            self.assertEqual(handler.backupCount, 14)
            self.assertEqual(handler.encoding, "utf-8")

    def test_does_not_truncate_existing_log_on_startup(self):
        # 起動のたびに上書きしていると障害直前の行動が消える（追記であること）
        with tempfile.TemporaryDirectory() as tmp:
            existing = Path(tmp) / "app.log"
            existing.write_text("前回の起動で書いた行\n", encoding="utf-8")
            self._setup_in(tmp)
            logging.getLogger("test").info("今回の行")
            self.assertIn("前回の起動で書いた行", existing.read_text(encoding="utf-8"))

    def test_rotation_deletes_files_beyond_retention(self):
        # 15 日分たまっていたら、いちばん古い 1 件がローテート時の削除対象になる
        with tempfile.TemporaryDirectory() as tmp:
            handler = self._setup_in(tmp)
            for day in range(1, 16):
                (Path(tmp) / f"app.log.2026-08-{day:02d}").write_text("x", encoding="utf-8")
            doomed = handler.getFilesToDelete()
            self.assertEqual(
                [Path(p).name for p in doomed], ["app.log.2026-08-01"]
            )

    def test_rotation_keeps_files_within_retention(self):
        with tempfile.TemporaryDirectory() as tmp:
            handler = self._setup_in(tmp)
            for day in range(1, 15):
                (Path(tmp) / f"app.log.2026-08-{day:02d}").write_text("x", encoding="utf-8")
            self.assertEqual(handler.getFilesToDelete(), [])


class TestDefaultLogDir(_LoggerTestBase):
    def test_platform_specific_dirs(self):
        with mock.patch.object(logger_module.sys, "platform", "win32"), \
             mock.patch.dict(os.environ, {"LOCALAPPDATA": "C:\\Users\\t\\AppData\\Local"}):
            self.assertEqual(
                logger_module.default_log_dir(),
                Path("C:\\Users\\t\\AppData\\Local") / "voicekey" / "logs",
            )

        with mock.patch.object(logger_module.sys, "platform", "darwin"):
            self.assertEqual(
                logger_module.default_log_dir(),
                Path.home() / "Library" / "Logs" / "voicekey",
            )


if __name__ == "__main__":
    unittest.main()
