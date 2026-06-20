"""deep link OS 連携（deep_link）のテスト。

argv からの URL 抽出とレジストリ登録のガードを検証する。
QtNetwork を使う単一インスタンス IPC は実行環境に依存するためここでは検証しない
（py_compile と実機検証に委ねる）。
"""

import sys
import unittest

from src.core import deep_link


class TestExtractURL(unittest.TestCase):
    def test_finds_url(self):
        argv = ["voicekey.exe", "voicekey://auth?code=a&state=b"]
        self.assertEqual(
            deep_link.extract_url_from_argv(argv),
            "voicekey://auth?code=a&state=b",
        )

    def test_none_when_absent(self):
        self.assertIsNone(deep_link.extract_url_from_argv(["voicekey.exe", "--debug"]))

    def test_none_for_empty(self):
        self.assertIsNone(deep_link.extract_url_from_argv(["voicekey.exe"]))

    def test_ignores_other_schemes(self):
        argv = ["voicekey.exe", "https://example.com", "voicekey://auth?code=c&state=d"]
        self.assertEqual(
            deep_link.extract_url_from_argv(argv),
            "voicekey://auth?code=c&state=d",
        )


class TestRegisterURLScheme(unittest.TestCase):
    @unittest.skipIf(sys.platform == "win32", "非 win32 のときの no-op を検証する")
    def test_noop_on_non_windows(self):
        # 他 OS では何もせず False を返す（macOS は Info.plist で登録するため）
        self.assertFalse(deep_link.register_url_scheme())


if __name__ == "__main__":
    unittest.main()
