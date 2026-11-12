"""設定のホットリロードで HUD・サイドノッチを UI スレッドで反映するかのテスト。

バグ: 監視スレッド（ConfigWatchdog）の _apply_config_changes が HUD の enabled /
always_visible とサイドノッチの set_enabled を直接触っていた。Qt のウィジェットは
UI スレッド以外から触ると落ちる・表示が壊れることがある。

VoicekeyApp は QObject 派生で素の生成ができないため、対象メソッドが参照する属性だけを持つ
SimpleNamespace を self に見立て、未束縛メソッドとして呼び出す（既存テストと同じ流儀）。
"""

import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from src.app import VoicekeyApp

# QApplication はプロセスに 1 つ。各テストで使い回す
_app = QApplication.instance() or QApplication([])


class _FakeConfig:
    def __init__(self, values):
        self._values = values

    def get(self, key, default=None):
        return self._values.get(key, default)


class _UiProbe(QObject):
    """VoicekeyApp と同じ配線（シグナル → _apply_ui_config）だけを持つ QObject。"""

    ui_config_changed = Signal()
    _apply_ui_config = VoicekeyApp._apply_ui_config

    def __init__(self, config):
        super().__init__()
        self._config = config
        self.applied_on = []
        probe = self

        class _Hud:
            def __setattr__(self, name, value):
                probe.applied_on.append(threading.current_thread())
                object.__setattr__(self, name, value)

        self._hud = _Hud()
        self._side_notch = mock.Mock()
        self._side_notch.set_enabled.side_effect = (
            lambda enabled: self.applied_on.append(threading.current_thread())
        )
        self.ui_config_changed.connect(self._apply_ui_config)


class TestHotReloadUiThread(unittest.TestCase):
    def _fake_self(self, config):
        hud = SimpleNamespace(enabled=True, always_visible=False)
        return SimpleNamespace(
            _config=config,
            _recorder=mock.Mock(),
            _current_input_device=None,
            _slots={},
            _build_slots=lambda: {},
            _handsfree_transcriber=mock.Mock(),
            _build_handsfree_transcriber=mock.Mock(),
            _parse_hotkey=lambda value: set(),
            _hud=hud,
            _side_notch=mock.Mock(),
            ui_config_changed=mock.Mock(),
            _emit_state=mock.Mock(),
            _history_sync=mock.Mock(),
        )

    def test_reload_does_not_touch_widgets_directly(self):
        """監視スレッド側では HUD・サイドノッチに触らず、UI スレッドへの合図だけ出す。"""
        config = _FakeConfig({"hud_enabled": False, "hud_always_visible": True,
                              "side_notch_enabled": False})
        s = self._fake_self(config)
        with mock.patch("src.app.threading.Timer"):
            VoicekeyApp._apply_config_changes(s)
        s.ui_config_changed.emit.assert_called_once_with()
        self.assertEqual((s._hud.enabled, s._hud.always_visible), (True, False))
        s._side_notch.set_enabled.assert_not_called()
        s._emit_state.assert_called_once()

    def test_apply_ui_config_sets_values(self):
        config = _FakeConfig({"hud_enabled": False, "hud_always_visible": True,
                              "side_notch_enabled": False})
        s = SimpleNamespace(
            _config=config,
            _hud=SimpleNamespace(enabled=True, always_visible=False),
            _side_notch=mock.Mock(),
        )
        VoicekeyApp._apply_ui_config(s)
        self.assertEqual((s._hud.enabled, s._hud.always_visible), (False, True))
        s._side_notch.set_enabled.assert_called_once_with(False)

    def test_signal_from_worker_thread_applies_on_ui_thread(self):
        """監視スレッドから合図しても、反映そのものは UI スレッドで走る。"""
        probe = _UiProbe(_FakeConfig({}))
        worker = threading.Thread(target=probe.ui_config_changed.emit)
        worker.start()
        worker.join()
        # 別スレッドからの emit はキューに積まれる。UI スレッドのイベント処理で初めて走る
        self.assertEqual(probe.applied_on, [])
        _app.processEvents()
        self.assertEqual(len(probe.applied_on), 3)
        self.assertTrue(all(t is threading.main_thread() for t in probe.applied_on))


if __name__ == "__main__":
    unittest.main()
