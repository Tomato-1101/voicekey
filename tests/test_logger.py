"""ロガーのログ出力先解決の検証（#18）。

ログ先を作業ディレクトリ（cwd）依存にしないことを確かめる:
- 相対名は OS 標準ログディレクトリ配下に作成される（cwd ではない）
- 絶対パスはそのまま使われる
- ディレクトリ/ファイルが作れなくてもアプリは止めない（コンソールのみで継続）

logging はプロセス全体で共有のため、各テストでルートロガーのハンドラと
モジュールの設定フラグをリセットして独立させる。
"""

import logging
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
