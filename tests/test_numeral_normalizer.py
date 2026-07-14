"""数字表記正規化（numeral_normalizer）の単体テスト。

変換系（全角→半角・連続漢数字→算用数字）と、普通の日本語語彙を壊さない保護系を
ネットワークなしで検証する。Mac 版 NumeralNormalizerTests.swift と同じ観点。
"""

import unittest

from src.core.numeral_normalizer import normalize


class TestConversion(unittest.TestCase):
    """半角化されるべきケース。"""

    def test_fullwidth_digits_to_halfwidth(self):
        self.assertEqual(normalize("１２３４５"), "12345")

    def test_fullwidth_letters_to_halfwidth(self):
        self.assertEqual(normalize("ＡＢＣａｂｃ"), "ABCabc")

    def test_consecutive_kanji_digits_to_arabic(self):
        # 位取りを含まない連続漢数字（2 文字以上）は算用数字化する
        self.assertEqual(normalize("三五八〇九一"), "358091")

    def test_kanji_year_sequence(self):
        # 二〇二六 のような読み上げ年号は連続漢数字なので変換される
        self.assertEqual(normalize("二〇二六年"), "2026年")

    def test_mixed_sentence(self):
        self.assertEqual(
            normalize("電話番号は〇九〇一二三四五六七八です"),
            "電話番号は09012345678です",
        )


class TestProtection(unittest.TestCase):
    """普通の語彙を壊してはならない（単独漢数字・位取りは不変）。"""

    def test_single_kanji_words_unchanged(self):
        # 単独の漢数字は普通の語の一部なので変換しない
        for word in ["一人", "二階建て", "一番", "五月雨", "四月", "六本木"]:
            self.assertEqual(normalize(word), word)

    def test_positional_kanji_unchanged(self):
        # 位取り（十百千万）を含む数はそのまま（十時三十分・千二百円 等）
        self.assertEqual(normalize("十時三十分"), "十時三十分")
        self.assertEqual(normalize("千二百円"), "千二百円")
        self.assertEqual(normalize("百二十三"), "百二十三")

    def test_normal_kana_and_kanji_unchanged(self):
        self.assertEqual(normalize("今日は晴れです"), "今日は晴れです")


class TestEdgeCases(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(normalize(""), "")

    def test_halfwidth_already_unchanged(self):
        self.assertEqual(normalize("abc123"), "abc123")

    def test_idempotent(self):
        once = normalize("三五八〇九一と１２３と一人")
        self.assertEqual(normalize(once), once)

    def test_fullwidth_punctuation_kept(self):
        # 全角の記号・句読点は変換対象外（日本語表記を壊さない）
        self.assertEqual(normalize("はい、（そうです）！"), "はい、（そうです）！")


if __name__ == "__main__":
    unittest.main()
