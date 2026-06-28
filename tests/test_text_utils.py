"""text_utils（CJK 隣接スペース除去）のテスト。"""

import unittest

from src.core.text_utils import _is_cjk, join_segments, strip_cjk_spaces


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

    def test_chinese_spaces_removed(self):
        # 中国語（CJK 統合漢字）の単語間スペースも除去（#21・Mac TextNormalize と同結果）
        self.assertEqual(strip_cjk_spaces("今天 天气 很好"), "今天天气很好")

    def test_fullwidth_boundary_removed(self):
        # 全角・半角形（全角英数）も CJK とみなしスペースを除去
        self.assertEqual(strip_cjk_spaces("Ａ は B"), "ＡはB")

    def test_idempotent(self):
        # 冪等: 2 回適用しても結果は変わらない（Deepgram 既処理の二重適用に備える）
        once = strip_cjk_spaces("今日 は 晴れ")
        self.assertEqual(strip_cjk_spaces(once), once)

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


class TestJoinSegments(unittest.TestCase):
    """分割並列送信の結果結合（join_segments）を検証する。"""

    def test_japanese_no_space(self):
        # 日本語境界はスペース無しで詰める
        self.assertEqual(join_segments(["これはテスト", "次の文です"]), "これはテスト次の文です")

    def test_english_single_space(self):
        # 英数字同士は単語境界としてスペース 1 個
        self.assertEqual(join_segments(["hello world", "foo bar"]), "hello world foo bar")

    def test_mixed_boundary_cjk_wins(self):
        # 片側が CJK の境界はスペース無し（英語→日本語、日本語→英語とも）
        self.assertEqual(join_segments(["終わりです", "Hello"]), "終わりですHello")
        self.assertEqual(join_segments(["Hello", "終わりです"]), "Hello終わりです")

    def test_empty_pieces_skipped(self):
        # 空・空白のみのセグメントは飛ばす
        self.assertEqual(join_segments(["あ", "", "  ", "い"]), "あい")

    def test_order_preserved(self):
        # 渡された index 順をそのまま保つ
        self.assertEqual(join_segments(["1", "2", "3"]), "1 2 3")

    def test_single_and_empty(self):
        self.assertEqual(join_segments(["solo"]), "solo")
        self.assertEqual(join_segments([]), "")
        self.assertEqual(join_segments(["", "  "]), "")


if __name__ == "__main__":
    unittest.main()
