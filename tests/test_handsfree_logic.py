"""機能A（ハンズフリー切替キー）の純粋判定ヘルパーの検証。

_slot_matches / _handsfree_pressed は self の属性しか見ないため、
SimpleNamespace のダミー self で unbound 呼び出ししてロジックを検証する
（VoicekeyApp 全体を構築せずに核心の判定だけを確かめる）。

実コードに合わせる点:
- スロット属性は required_keys（_parse_hotkey が <> を剥がした素トークン集合）
- _pressed_keys / _handsfree_keys も <> なしの素トークン
- _acceptable_names は staticmethod（左右修飾キーの汎用名展開）なので実体を注入する
"""

import unittest
from types import SimpleNamespace

from src.app import VoicekeyApp


class TestSlotMatches(unittest.TestCase):
    def _fake(self, pressed):
        return SimpleNamespace(
            _pressed_keys=set(pressed),
            _acceptable_names=VoicekeyApp._acceptable_names,
        )

    def _slot(self, keys):
        return SimpleNamespace(required_keys=set(keys))

    def test_exact_match(self):
        fs = self._fake({"f2"})
        self.assertTrue(VoicekeyApp._slot_matches(fs, self._slot({"f2"})))

    def test_combo_subset(self):
        # 修飾キー付きホットキーは押下集合の部分集合一致
        fs = self._fake({"ctrl", "space", "shift"})
        self.assertTrue(VoicekeyApp._slot_matches(fs, self._slot({"ctrl", "space"})))

    def test_not_pressed(self):
        fs = self._fake({"f3"})
        self.assertFalse(VoicekeyApp._slot_matches(fs, self._slot({"f2"})))

    def test_empty_slot_never_matches(self):
        # 未設定（空ホットキー）のスロットは何にも一致してはいけない（全キー誤発火の防止）
        fs = self._fake({"f2"})
        self.assertFalse(VoicekeyApp._slot_matches(fs, self._slot(set())))

    def test_generic_modifier_matches_right_key(self):
        # 汎用名 cmd 設定は右 cmd_r 押下でも一致する（_acceptable_names の左右展開）
        fs = self._fake({"cmd_r", "f2"})
        self.assertTrue(VoicekeyApp._slot_matches(fs, self._slot({"cmd", "f2"})))


class TestHandsfreePressed(unittest.TestCase):
    def _fake(self, handsfree, pressed):
        return SimpleNamespace(
            _handsfree_keys=set(handsfree),
            _pressed_keys=set(pressed),
            _acceptable_names=VoicekeyApp._acceptable_names,
        )

    def test_empty_handsfree_disabled(self):
        # 未設定（空）なら常に False＝ハンズフリー無効
        fs = self._fake(set(), {"f2", "cmd_r"})
        self.assertFalse(VoicekeyApp._handsfree_pressed(fs))

    def test_handsfree_all_pressed(self):
        fs = self._fake({"cmd_r"}, {"f2", "cmd_r"})
        self.assertTrue(VoicekeyApp._handsfree_pressed(fs))

    def test_handsfree_not_pressed(self):
        fs = self._fake({"cmd_r"}, {"f2"})
        self.assertFalse(VoicekeyApp._handsfree_pressed(fs))

    def test_multi_key_handsfree_partial(self):
        # 複数キーの切替キーは全部押されていないと False
        fs = self._fake({"cmd_r", "shift_r"}, {"f2", "cmd_r"})
        self.assertFalse(VoicekeyApp._handsfree_pressed(fs))


if __name__ == "__main__":
    unittest.main()
