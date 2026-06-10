"""autostart（ログイン時起動）のプラットフォーム非依存テスト。

Windows レジストリ操作は macOS/Linux では実行できないため、
非対応プラットフォームで安全に no-op となること（例外を投げず
False を返すこと）と、コマンド組み立ての体裁を検証する。
"""

import sys
import unittest

from src.utils import autostart


class TestAutostart(unittest.TestCase):
    def test_is_supported_matches_platform(self):
        # is_supported は Windows でのみ True
        self.assertEqual(autostart.is_supported(), sys.platform.startswith("win"))

    @unittest.skipIf(
        sys.platform.startswith("win"), "Windows では実レジストリに触れるため除外"
    )
    def test_noop_on_unsupported_platform(self):
        # 非 Windows では例外を投げず False を返す（UI から安全に呼べる）
        self.assertFalse(autostart.is_enabled())
        self.assertFalse(autostart.set_enabled(True))
        self.assertFalse(autostart.set_enabled(False))

    def test_launch_command_is_quoted(self):
        # frozen 判定に関わらず実行ファイルパスは引用符で括られる
        cmd = autostart._launch_command()
        self.assertTrue(cmd.startswith('"'))
        self.assertGreaterEqual(cmd.count('"'), 2)


if __name__ == "__main__":
    unittest.main()
