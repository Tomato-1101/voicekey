"""text_utils（CJK 隣接スペース除去）のテスト。"""

import unittest

from src.core.text_utils import _is_cjk, strip_cjk_spaces


class TestStripCJKSpaces(unittest.TestCase):
    def test_japanese_spaces_removed(self):
        # 日本語の単語間スペースは除去
        self.assertEqual(strip_cjk_spaces("今日 は 晴れ"), "今日は晴れ")

    def test_english_spaces_kept(self):
        # 英単語間のスペースは保持
        self.assertEqual(strip_cjk_spaces("hello world"), "hello world")

    def test_mixed_japanese_english(self):
        # 片側が CJK のスペースのみ除去（英語表記は壊さない）
        self.assertEqual(strip_cjk_spaces("GPT 4 と Claude Code"), "GPT 4とClaude Code")

    def test_cjk_english_boundary_removed(self):
        # 漢字と英字の境界スペースは片側 CJK なので除去
        self.assertEqual(strip_cjk_spaces("日本 ABC"), "日本ABC")

    def test_no_space_unchanged(self):
        self.assertEqual(strip_cjk_spaces("テスト"), "テスト")
        self.assertEqual(strip_cjk_spaces(""), "")

    def test_is_cjk(self):
        self.assertTrue(_is_cjk("あ"))   # ひらがな
        self.assertTrue(_is_cjk("カ"))   # カタカナ
        self.assertTrue(_is_cjk("漢"))   # 漢字
        self.assertFalse(_is_cjk("a"))
        self.assertFalse(_is_cjk(" "))
        self.assertFalse(_is_cjk("4"))


if __name__ == "__main__":
    unittest.main()
