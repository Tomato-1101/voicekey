"""ユーザー辞書（確定置換）の適用ロジック検証。

VoicekeyApp._apply_replacements は self._config.get("replacements", ...) しか見ないため、
SimpleNamespace のダミー self で unbound 呼び出ししてロジックだけを確かめる
（VoicekeyApp 全体・録音/UI を構築せずに核心の置換だけを検証する）。
"""

import unittest
from types import SimpleNamespace

from src.app import VoicekeyApp


class _FakeConfig:
    """settings.yaml の replacements だけを返す最小スタブ。"""

    def __init__(self, rules):
        self._rules = rules

    def get(self, key, default=None):
        if key == "replacements":
            return self._rules
        return default


def _apply(rules, text):
    """ダミー self で _apply_replacements を呼ぶ。"""
    fake = SimpleNamespace(_config=_FakeConfig(rules))
    return VoicekeyApp._apply_replacements(fake, text)


class TestApplyReplacements(unittest.TestCase):
    def test_no_rules_returns_original(self):
        self.assertEqual(_apply([], "そのまま"), "そのまま")

    def test_none_rules_returns_original(self):
        # 設定が未設定（None）でも落ちず原文を返す
        self.assertEqual(_apply(None, "そのまま"), "そのまま")

    def test_simple_replace(self):
        rules = [{"from": "ジーピーティー", "to": "GPT", "enabled": True}]
        self.assertEqual(_apply(rules, "ジーピーティーを使う"), "GPTを使う")

    def test_partial_match(self):
        # 部分一致（語境界は見ない）
        rules = [{"from": "AI", "to": "人工知能", "enabled": True}]
        self.assertEqual(_apply(rules, "AIとAIの話"), "人工知能と人工知能の話")

    def test_disabled_rule_skipped(self):
        rules = [{"from": "犬", "to": "猫", "enabled": False}]
        self.assertEqual(_apply(rules, "犬がいる"), "犬がいる")

    def test_empty_from_skipped(self):
        # 変換元が空の行は無視（全文が壊れない）
        rules = [{"from": "", "to": "X", "enabled": True}]
        self.assertEqual(_apply(rules, "abc"), "abc")

    def test_applied_in_order(self):
        # 登録順に連鎖適用される（1つ目の結果に2つ目が当たる）
        rules = [
            {"from": "a", "to": "b", "enabled": True},
            {"from": "b", "to": "c", "enabled": True},
        ]
        self.assertEqual(_apply(rules, "a"), "c")

    def test_enabled_defaults_true(self):
        # enabled キーが無い行は有効扱い
        rules = [{"from": "x", "to": "y"}]
        self.assertEqual(_apply(rules, "xyz"), "yyz")

    def test_to_defaults_empty_is_deletion(self):
        # to が無い行は空文字へ置換＝削除になる
        rules = [{"from": "削除", "enabled": True}]
        self.assertEqual(_apply(rules, "削除する"), "する")

    def test_non_dict_rule_skipped(self):
        # 設定ファイルが手編集で壊れていても落ちない
        rules = ["bad", {"from": "o", "to": "0", "enabled": True}]
        self.assertEqual(_apply(rules, "foo"), "f00")


if __name__ == "__main__":
    unittest.main()
