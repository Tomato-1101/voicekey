"""分割並列送信の結合ロジック（VoicekeyApp._transcribe_parallel）のテスト。

transcriber をモックして、並列実行でも index 順を保つこと・1 つでも失敗したら
None（呼び出し側が全体 1 本送信にフォールバック）を返すことを検証する。
_transcribe_parallel は self を参照しないため、ダミー self で直接呼び出す。
"""

import unittest
from unittest.mock import MagicMock

from src.app import VoicekeyApp


def _slot_with(transcribe_fn):
    """transcribe をモック差し替えしたスロット風オブジェクトを返す。"""
    slot = MagicMock()
    slot.transcriber.transcribe.side_effect = transcribe_fn
    return slot


class TestTranscribeParallel(unittest.TestCase):
    def _run(self, slot, segments):
        # self は未使用なのでダミー（None）で呼べる
        return VoicekeyApp._transcribe_parallel(None, slot, segments)

    def test_order_preserved(self):
        """並列でも渡した index 順に結合される。"""
        # seg 値そのものを文字起こし結果に見立てる（英数字なのでスペース区切り）
        slot = _slot_with(lambda seg: f"r{seg}")
        result = self._run(slot, [0, 1, 2, 3])
        self.assertEqual(result, "r0 r1 r2 r3")

    def test_cjk_joined_without_space(self):
        """日本語結果はスペース無しで結合される。"""
        parts = {0: "これは", 1: "テストです"}
        slot = _slot_with(lambda seg: parts[seg])
        self.assertEqual(self._run(slot, [0, 1]), "これはテストです")

    def test_one_failure_returns_none(self):
        """1 セグメントでも例外なら None（全体送信にフォールバックさせる）。"""
        def fn(seg):
            if seg == 1:
                raise RuntimeError("429 Too Many Requests")
            return f"r{seg}"
        slot = _slot_with(fn)
        self.assertIsNone(self._run(slot, [0, 1, 2]))

    def test_all_segments_sent(self):
        """全セグメントが transcribe に渡される。"""
        slot = _slot_with(lambda seg: "x")
        self._run(slot, [10, 20, 30])
        sent = {c.args[0] for c in slot.transcriber.transcribe.call_args_list}
        self.assertEqual(sent, {10, 20, 30})


if __name__ == "__main__":
    unittest.main()
