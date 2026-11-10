"""記号の自動削除（core/symbol_filter）と、その設定の既定・保存のテスト。

期待値の表は Mac 版 SymbolFilterTests.swift と同じ。両 OS で同じ入力に同じ出力になること。
"""

import unittest

from src.config.constants import DEFAULT_CONFIG
from src.core import symbol_filter
from src.core.symbol_filter import filter_symbols

# 既定設定（残す: 、。, .）での期待値表（両 OS 共通）
DEFAULT_TABLE = [
    ("こんにちは！元気？「はい」（笑）…", "こんにちは元気はい笑"),
    ("今日は、晴れ。", "今日は、晴れ。"),
    ("Hello, world.", "Hello, world."),
    ("3.5と1,000と10:30", "3.5と1,000と10:30"),
    ("example.comにアクセス", "example.comにアクセス"),
    ("don't と it’s", "don't と it’s"),
    ("A: B", "A B"),
    ("「はい」と言った。", "はいと言った。"),
    ("a-b/c@d#e~f〜g", "a-b/c@d#e~f〜g"),
    ("ー長音ー", "ー長音ー"),
    ("これは ！ 例です", "これは 例です"),
    ("！ 先頭", "先頭"),
    ("末尾 ！", "末尾"),
    ("一行目 ！\n二行目", "一行目\n二行目"),
    ("元の  二重スペース", "元の  二重スペース"),
    ("'quoted' and \"x\"", "quoted and x"),
    ("【重要】[a](b)", "重要ab"),
    ("A・B；C：D", "ABCD"),
    ("", ""),
]


class TestSymbolFilter(unittest.TestCase):
    def test_default_table(self):
        for src, expected in DEFAULT_TABLE:
            self.assertEqual(filter_symbols(src), expected, src)

    def test_disabled_is_passthrough(self):
        text = "こんにちは！元気？「はい」（笑）… A: B"
        self.assertEqual(filter_symbols(text, enabled=False), text)

    def test_custom_keep_groups(self):
        keep = ["exclamation", "question", "kagikakko"]
        self.assertEqual(
            filter_symbols("こんにちは！元気？「はい」（笑）…、。", keep_groups=keep),
            "こんにちは！元気？「はい」笑",
        )
        # 空リストなら全グループを消す
        self.assertEqual(filter_symbols("あ、い。う", keep_groups=[]), "あいう")

    def test_protect_keeps_dictionary_symbols(self):
        # 辞書置換で入れた記号は、訳文にかけ直しても消さない
        self.assertEqual(filter_symbols("Use【重要】now!", protect=["【重要】"]), "Use【重要】now")
        self.assertEqual(filter_symbols("a「b」c「b」", protect=["「b」"]), "a「b」c「b」")
        # 守る語が無い所の同じ記号は消す・空文字は無視
        self.assertEqual(filter_symbols("(x) (y)", protect=["(x)", ""]), "(x) y")

    def test_idempotent(self):
        for src, _ in DEFAULT_TABLE:
            once = filter_symbols(src)
            self.assertEqual(filter_symbols(once), once, src)

    def test_group_ids_unique_and_defaults_exist(self):
        ids = [g[0] for g in symbol_filter.GROUPS]
        self.assertEqual(len(set(ids)), len(ids))
        self.assertEqual(len(ids), 14)
        for gid in symbol_filter.DEFAULT_KEEP_GROUPS:
            self.assertIn(gid, ids)

    def test_config_defaults(self):
        self.assertTrue(DEFAULT_CONFIG["symbol_removal_enabled"])
        self.assertEqual(DEFAULT_CONFIG["symbol_keep_groups"], ["comma", "period", "touten", "kuten"])
        self.assertEqual(DEFAULT_CONFIG["symbol_keep_groups"], symbol_filter.DEFAULT_KEEP_GROUPS)


if __name__ == "__main__":
    unittest.main()
