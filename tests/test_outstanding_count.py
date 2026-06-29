"""未完了タスク数（VoicekeyApp._outstanding）が負数にならないことのテスト。

録音停止コールバックが万一二重発火しても、ワーカーの finally でのデクリメントが
_outstanding を負数にしない（HUD が「変換中」に張り付くのを防ぐ防御）。

VoicekeyApp は QObject 派生で素の生成ができないため、_worker_loop が参照する属性だけを
持つ SimpleNamespace を self に見立て、未束縛メソッドとして呼び出す（QObject 構築を回避）。
"""

import queue
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from src.app import VoicekeyApp


class TestOutstandingNeverNegative(unittest.TestCase):
    def _fake_self(self, outstanding_start: int):
        return SimpleNamespace(
            _task_q=queue.Queue(),
            _state_lock=threading.Lock(),
            _outstanding=outstanding_start,
            _process_task=mock.Mock(),
            _emit_state=mock.Mock(),
            # 録音完了ごとに呼ばれる利用権の残量再取得（finally 内）。ここでは未完了数の
            # 検証だけが目的なので no-op モックにする。
            _refresh_entitlement_async=mock.Mock(),
            notice=mock.Mock(),
        )

    def test_decrement_clamped_at_zero(self):
        """既に 0 の状態でタスクを 1 件処理してもデクリメントで -1 にならない。"""
        s = self._fake_self(outstanding_start=0)
        s._task_q.put(object())   # 1 件処理させる
        s._task_q.put(None)       # 終了センチネル
        VoicekeyApp._worker_loop(s)
        s._process_task.assert_called_once()
        self.assertEqual(s._outstanding, 0, "未完了数が負数になった")

    def test_normal_decrement_still_works(self):
        """通常時（1→0）のデクリメントは従来どおり動く。"""
        s = self._fake_self(outstanding_start=1)
        s._task_q.put(object())
        s._task_q.put(None)
        VoicekeyApp._worker_loop(s)
        self.assertEqual(s._outstanding, 0)


if __name__ == "__main__":
    unittest.main()
