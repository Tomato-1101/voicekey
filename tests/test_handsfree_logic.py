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

from src.app import HotkeySlot, VoicekeyApp
from src.config import HotkeyMode, TranscriptionBackend


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


class TestParseHotkeyUnassigned(unittest.TestCase):
    """設定 UI の「未割り当て」（空文字ホットキー）が空トークン集合になり録音を発火させないこと。

    ホットキーを外すと保存値は空文字 "" になる。_parse_hotkey("") が空集合を返し、
    そこから作ったスロットが _slot_matches で何にも一致しない（誤発火しない）ことを保証する。
    Mac 版 SlotConfigTests.testDecodeWithoutHotkeyIsUnassigned と対応する回帰テスト。
    """

    def test_empty_string_parses_to_empty_set(self):
        # 空文字ホットキー（未割り当て）は空集合になる（誤って {""} を作らない）
        self.assertEqual(VoicekeyApp._parse_hotkey(""), set())

    def test_angle_brackets_only_parses_to_empty_set(self):
        # "<>" のような実質空の指定も空集合
        self.assertEqual(VoicekeyApp._parse_hotkey("<>"), set())

    def test_unassigned_slot_built_from_empty_string_never_matches(self):
        # 空文字ホットキーから構築したスロットは、どのキー押下にも一致しない
        slot = SimpleNamespace(required_keys=VoicekeyApp._parse_hotkey(""))
        fs = SimpleNamespace(
            _pressed_keys={"f2"},
            _acceptable_names=VoicekeyApp._acceptable_names,
        )
        self.assertFalse(VoicekeyApp._slot_matches(fs, slot))


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


class TestMaybeHandsfreeSlot(unittest.TestCase):
    """ハンズフリー(toggle 実効)で groq スロットのとき、内部で EL(scribe_v1) に差し替える判定。

    _maybe_handsfree_slot は self._handsfree_transcriber しか見ないため、SimpleNamespace の
    ダミー self で unbound 呼び出しして検証する（VoicekeyApp 全体を構築しない）。
    """

    def _slot(self, backend, mode):
        return HotkeySlot(
            slot_id=1,
            hotkey="<f2>",
            hotkey_mode=mode,
            required_keys={"f2"},
            backend=backend,
            api_model="whisper-large-v3-turbo",
            api_prompt="",
            format_enabled=True,
            transcriber=object(),
        )

    def test_toggle_groq_swaps_to_elevenlabs(self):
        # toggle 実効の groq スロットは EL(scribe_v1) にスナップショット差し替えされる
        fake_el = object()
        fake = SimpleNamespace(_handsfree_transcriber=fake_el)
        groq_slot = self._slot(TranscriptionBackend.GROQ.value, HotkeyMode.HOLD.value)
        result = VoicekeyApp._maybe_handsfree_slot(
            fake, groq_slot, HotkeyMode.TOGGLE.value
        )
        self.assertEqual(result.backend, TranscriptionBackend.ELEVENLABS.value)
        self.assertEqual(result.api_model, "scribe_v1")
        self.assertIs(result.transcriber, fake_el)
        # 整形可否など他フィールドは維持される
        self.assertTrue(result.format_enabled)
        # 元スロットは不変（保存値 groq を汚さない）
        self.assertEqual(groq_slot.backend, TranscriptionBackend.GROQ.value)
        self.assertIsNot(result.transcriber, groq_slot.transcriber)

    def test_hold_groq_stays_groq(self):
        # hold 実効（自動 Enter 用の通常押下）の groq スロットは差し替えない
        fake = SimpleNamespace(_handsfree_transcriber=object())
        groq_slot = self._slot(TranscriptionBackend.GROQ.value, HotkeyMode.HOLD.value)
        result = VoicekeyApp._maybe_handsfree_slot(
            fake, groq_slot, HotkeyMode.HOLD.value
        )
        self.assertIs(result, groq_slot)

    def test_toggle_deepgram_stays(self):
        # deepgram(リアルタイム)は toggle でも差し替えない（groq のときだけ）
        fake = SimpleNamespace(_handsfree_transcriber=object())
        dg_slot = self._slot(TranscriptionBackend.DEEPGRAM.value, HotkeyMode.TOGGLE.value)
        result = VoicekeyApp._maybe_handsfree_slot(
            fake, dg_slot, HotkeyMode.TOGGLE.value
        )
        self.assertIs(result, dg_slot)


if __name__ == "__main__":
    unittest.main()
