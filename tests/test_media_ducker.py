"""メディア音量ダッキング（src/core/media_ducker.py）のロジックテスト。

検証する性質（Mac 版 MediaDucker と同じ安全策）:
1. 現在音量がターゲットより大きいときだけ下げ、元音量とフラグを永続化する。
2. 現在音量が既にターゲット以下なら何もしない（音量を引き上げない）。
3. 既にダッキング中なら二重に下げない（元音量を上書きしない）。
4. restore は保存した元音量へ戻して状態をクリアする。ダッキングしていなければ何もしない。
5. クラッシュ耐性: 残存フラグがあれば別インスタンス（起動時）で元へ戻して状態をクリアする。
6. 音量コントローラが取得できない（pycaw 無し等）ときは無害にスキップする。

実際の COM 呼び出し（pycaw / IAudioEndpointVolume）はモック境界の外なので、
_duck_impl / _restore_impl に fake の音量コントローラと一時状態ファイルを注入して検証する。
"""

import tempfile
import unittest
from pathlib import Path

from src.core import media_ducker
from src.core.media_ducker import DUCK_VOLUME, _duck_impl, _load_state, _restore_impl


class _FakeController:
    """テスト用の音量コントローラ（マスター音量 0..1 を保持）。"""

    def __init__(self, volume: float) -> None:
        self.volume = volume
        self.set_calls: list = []

    def get_volume(self):
        return self.volume

    def set_volume(self, value: float) -> bool:
        self.volume = max(0.0, min(1.0, float(value)))
        self.set_calls.append(self.volume)
        return True


class MediaDuckerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "media_duck_state.json"

    def tearDown(self):
        self._tmp.cleanup()

    def test_duck_lowers_and_persists_when_above_target(self):
        """現在音量がターゲットより大きい → 12% へ下げ、元音量とフラグを保存する。"""
        ctrl = _FakeController(0.8)
        _duck_impl(provider=lambda: ctrl, state_path=self.path)
        self.assertAlmostEqual(ctrl.volume, DUCK_VOLUME)
        self.assertEqual(ctrl.set_calls, [DUCK_VOLUME])
        state = _load_state(self.path)
        self.assertTrue(state["active"])
        self.assertAlmostEqual(state["saved_volume"], 0.8)

    def test_duck_skips_when_at_or_below_target(self):
        """既にターゲット以下なら下げない・フラグも立てない（音量を引き上げない）。"""
        ctrl = _FakeController(0.10)
        _duck_impl(provider=lambda: ctrl, state_path=self.path)
        self.assertEqual(ctrl.set_calls, [])
        self.assertEqual(_load_state(self.path), {})

    def test_duck_is_idempotent_when_already_active(self):
        """既にダッキング中なら二重に下げない（保存済み元音量を上書きしない）。"""
        ctrl = _FakeController(0.8)
        _duck_impl(provider=lambda: ctrl, state_path=self.path)
        # 2 回目（この間に音量が変わっていても元音量は最初の 0.8 を保持する）
        ctrl.volume = 0.5
        _duck_impl(provider=lambda: ctrl, state_path=self.path)
        state = _load_state(self.path)
        self.assertAlmostEqual(state["saved_volume"], 0.8)  # 上書きされていない
        self.assertEqual(ctrl.set_calls, [DUCK_VOLUME])       # 2 回目は下げ直さない

    def test_restore_returns_saved_and_clears(self):
        """restore は保存した元音量へ戻し、状態ファイルを消す。"""
        ctrl = _FakeController(0.8)
        _duck_impl(provider=lambda: ctrl, state_path=self.path)
        self.assertAlmostEqual(ctrl.volume, DUCK_VOLUME)
        _restore_impl(provider=lambda: ctrl, state_path=self.path)
        self.assertAlmostEqual(ctrl.volume, 0.8)
        self.assertFalse(self.path.exists())

    def test_restore_noop_when_not_ducked(self):
        """ダッキングしていなければ restore は何もしない。"""
        ctrl = _FakeController(0.8)
        _restore_impl(provider=lambda: ctrl, state_path=self.path)
        self.assertEqual(ctrl.set_calls, [])

    def test_crash_recovery_across_instances(self):
        """録音中に落ちても、別インスタンス（起動時 restore）が残存フラグで元へ戻す。"""
        ducked = _FakeController(0.7)
        _duck_impl(provider=lambda: ducked, state_path=self.path)
        # ここでプロセスが落ちた想定（restore を呼ばずに状態ファイルが残る）
        self.assertTrue(_load_state(self.path)["active"])
        # 次回起動: 新しいコントローラ（現在は下がった 0.12 のまま）で restore
        fresh = _FakeController(DUCK_VOLUME)
        _restore_impl(provider=lambda: fresh, state_path=self.path)
        self.assertAlmostEqual(fresh.volume, 0.7)
        self.assertFalse(self.path.exists())

    def test_duck_skips_when_no_controller(self):
        """音量コントローラが取得できない（pycaw 無し等）なら無害にスキップする。"""
        _duck_impl(provider=lambda: None, state_path=self.path)
        self.assertEqual(_load_state(self.path), {})

    def test_restore_clears_state_even_without_controller(self):
        """コントローラが無くても、残存フラグは restore でクリアする（永久ダッキング状態を残さない）。"""
        ctrl = _FakeController(0.8)
        _duck_impl(provider=lambda: ctrl, state_path=self.path)
        _restore_impl(provider=lambda: None, state_path=self.path)
        self.assertFalse(self.path.exists())

    def test_public_api_does_not_raise(self):
        """公開 duck()/restore() は import 済みモジュールでも例外を投げない（撃ちっぱなし）。"""
        # 既定 provider は macOS では pycaw が無く None を返す＝無害スキップ。
        media_ducker.duck()
        media_ducker.restore()


if __name__ == "__main__":
    unittest.main()
