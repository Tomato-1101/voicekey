"""自分で送った貼り付けのキー（Ctrl+V）を録音キーと取り違えないかのテスト（Windows）。

バグ: 録音キーが Ctrl だけのとき、貼り付けで合成した Ctrl+V の Ctrl をキー監視が拾い、
録音キーの押下として録音が始まっていた。キー監視に合成キー（LLKHF_INJECTED）を渡さない。
"""

import unittest
from types import SimpleNamespace
from unittest import mock

from src import app as app_mod
from src.app import VoicekeyApp, _ignore_injected_keys


class TestIgnoreInjectedKeys(unittest.TestCase):
    def test_injected_key_is_dropped(self):
        self.assertFalse(_ignore_injected_keys(0x100, SimpleNamespace(flags=0x10)))
        # 合成の印に他の印（拡張キー 0x01 等）が混ざっていても落とす
        self.assertFalse(_ignore_injected_keys(0x101, SimpleNamespace(flags=0x11)))

    def test_physical_key_is_passed(self):
        self.assertTrue(_ignore_injected_keys(0x100, SimpleNamespace(flags=0x00)))
        self.assertTrue(_ignore_injected_keys(0x101, SimpleNamespace(flags=0x80)))  # 離鍵の印

    def test_listener_is_created_with_filter(self):
        """キー監視はこのフィルタ付きで作られる。"""
        s = SimpleNamespace(
            _monitoring=True,
            _listener=None,
            _on_press=mock.Mock(),
            _on_release=mock.Mock(),
            _on_listener_stopped=mock.Mock(),
        )
        created = {}

        def fake_listener(**kwargs):
            created.update(kwargs)
            s._monitoring = False  # 1 周で抜ける
            return mock.MagicMock()

        with mock.patch.object(app_mod.keyboard, "Listener", side_effect=fake_listener):
            VoicekeyApp._listener_loop(s)
        self.assertIs(created.get("win32_event_filter"), _ignore_injected_keys)
        self.assertIs(created.get("on_press"), s._on_press)


if __name__ == "__main__":
    unittest.main()
