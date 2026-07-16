"""数字表記正規化（numeral_normalizer v2）の単体テスト。

半角化・漢数字→算用数字（裸数字列／位取り）・助数詞つき単独漢数字・保護リスト・
2 トグルを、ネットワークなしで検証する。Mac 版 NumeralNormalizerTests.swift と同じ観点。
"""

import unittest

from src.core.numeral_normalizer import normalize

# 既定シード保護語（DEFAULT_CONFIG の numeral_protect_words と一致させる）
SEED = ["一時的", "一時停止", "一人", "二人", "十分", "一日中", "一部始終", "一石二鳥"]


class TestHalfwidth(unittest.TestCase):
    """全角英数字の半角化。"""

    def test_fullwidth_digits_to_halfwidth(self):
        self.assertEqual(normalize("１２３４５"), "12345")

    def test_fullwidth_single_digit(self):
        self.assertEqual(normalize("１０"), "10")

    def test_fullwidth_letters_to_halfwidth(self):
        self.assertEqual(normalize("ＡＢＣａｂｃ"), "ABCabc")


class TestBareDigitRun(unittest.TestCase):
    """位取りを含まない連続漢数字（2 文字以上）は各桁を算用数字化する。"""

    def test_consecutive_kanji_digits_to_arabic(self):
        self.assertEqual(normalize("三五八〇九一"), "358091")

    def test_kanji_year_sequence(self):
        # 二〇二六 のような読み上げ年号は連続漢数字なので変換される
        self.assertEqual(normalize("二〇二六年"), "2026年")

    def test_phone_number_with_leading_zero(self):
        self.assertEqual(normalize("〇九〇一二三四五六七八"), "09012345678")

    def test_mixed_sentence(self):
        self.assertEqual(
            normalize("電話番号は〇九〇一二三四五六七八です"),
            "電話番号は09012345678です",
        )


class TestPositional(unittest.TestCase):
    """位取りを含む日本語数詞は整数化する（v2 で新規変換）。"""

    def test_juuni(self):
        self.assertEqual(normalize("十二人"), "12人")

    def test_nijuusan(self):
        self.assertEqual(normalize("二十三"), "23")

    def test_sen_nihyaku(self):
        self.assertEqual(normalize("千二百三十四円"), "1234円")

    def test_sanman_gosen(self):
        self.assertEqual(normalize("三万五千"), "35000")


class TestSingleWithCounter(unittest.TestCase):
    """単独漢数字＋助数詞は変換する（三時→3時）。"""

    def test_san_ji(self):
        self.assertEqual(normalize("三時"), "3時")

    def test_juu_ji(self):
        self.assertEqual(normalize("十時"), "10時")

    def test_hyaku_nin(self):
        self.assertEqual(normalize("百人"), "100人")

    def test_sen_en(self):
        self.assertEqual(normalize("千円"), "1000円")


class TestPlaceNames(unittest.TestCase):
    """地名など、助数詞集合外の直後文字は変換しない（除外語で守る）。"""

    def test_chiba(self):
        self.assertEqual(normalize("千葉"), "千葉")

    def test_roppongi(self):
        # 本 は COUNTER 除外なので 六 は変換されない
        self.assertEqual(normalize("六本木"), "六本木")

    def test_gotanda(self):
        # 反 は COUNTER 除外なので 五 は変換されない
        self.assertEqual(normalize("五反田"), "五反田")


class TestProtectWords(unittest.TestCase):
    """保護リスト（先頭アンカー照合）。誤変換を防ぎつつ誤保護もしない。"""

    def test_ichijiteki_protected(self):
        self.assertEqual(normalize("一時的", protect_words=SEED), "一時的")

    def test_hitori_protected(self):
        # シード「一人」で 1人 化を防ぐ
        self.assertEqual(normalize("一人", protect_words=SEED), "一人")

    def test_juuichinin_not_falsely_protected(self):
        # 「一人」保護は先頭アンカーなので「十一人」の中の「一人」に誤爆せず 11人 に変換される
        self.assertEqual(normalize("十一人", protect_words=SEED), "11人")

    def test_juppun_protected(self):
        # シード「十分」で じゅうぶん を守る
        self.assertEqual(normalize("十分", protect_words=SEED), "十分")


class TestToggles(unittest.TestCase):
    """2 トグル（enabled / convert_counter）の挙動。"""

    def test_convert_counter_off_keeps_single(self):
        # convert_counter=False では単独漢数字＋助数詞は変換しない
        self.assertEqual(normalize("三時", convert_counter=False), "三時")

    def test_convert_counter_off_still_converts_positional(self):
        # 位取り>=2 はマスターのみで常時変換（convert_counter の影響を受けない）
        self.assertEqual(normalize("十二", convert_counter=False), "12")

    def test_disabled_is_full_passthrough(self):
        # enabled=False は全角半角化も含めて完全パススルー
        self.assertEqual(normalize("三時の１２３", enabled=False), "三時の１２３")


class TestEdgeCases(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(normalize(""), "")

    def test_halfwidth_already_unchanged(self):
        self.assertEqual(normalize("abc123"), "abc123")

    def test_normal_text_unchanged(self):
        self.assertEqual(normalize("今日は晴れです"), "今日は晴れです")

    def test_fullwidth_punctuation_kept(self):
        # 全角の記号・句読点は変換対象外（日本語表記を壊さない）
        self.assertEqual(normalize("はい、（そうです）！"), "はい、（そうです）！")

    def test_idempotent(self):
        for text in ["十二人", "千二百三十四円", "三時", "二〇二六年", "六本木", "１０と三万五千"]:
            once = normalize(text, protect_words=SEED)
            self.assertEqual(normalize(once, protect_words=SEED), once, text)


if __name__ == "__main__":
    unittest.main()
