"""「貼り付け先が AI のときは整形しない」のテスト（core/ai_destination と VoicekeyApp の snapshot）。

判定は録音開始時の最前面アプリの実行ファイル名だけで決まる純関数。録音開始時の snapshot
（_snapshot_context）で整形を省くと決めた回は、slot.format_enabled が False に差し替わり、
整形（text_formatter.format_text）もサーバー統合整形も走らないことを確かめる。

VoicekeyApp は QObject 派生で素の生成ができないため、test_task_context_snapshot と同じく
対象メソッドが参照する属性だけを持つ SimpleNamespace を self に見立てて呼ぶ。
"""

import unittest
from types import SimpleNamespace
from unittest import mock

from src.app import HotkeySlot, VoicekeyApp
from src.config.constants import DEFAULT_CONFIG
from src.core import ai_destination, diagnostics


class _FakeConfig:
    """snapshot で読まれる設定値を返す最小 config。"""

    def __init__(self, values: dict):
        self._values = values

    def get(self, key, default=None):
        return self._values.get(key, default)


class _FakePlatform:
    """最前面アプリの実行ファイル名を返すだけのプラットフォーム。"""

    def __init__(self, exe):
        self.exe = exe
        self.calls = 0

    def foreground_exe(self):
        self.calls += 1
        return self.exe


def _slot(backend: str = "groq", format_enabled: bool = True) -> HotkeySlot:
    return HotkeySlot(
        slot_id=1, hotkey="<f2>", hotkey_mode="hold", required_keys={"f2"},
        backend=backend, api_model="m", api_prompt="", format_enabled=format_enabled,
        transcriber=mock.Mock(),
    )


def _fake_self(exe, config: dict = None, practice: bool = False):
    return SimpleNamespace(
        _config=_FakeConfig(config or {}),
        _practice_format_override=practice,
        _platform=_FakePlatform(exe),
    )


class TestIsAIApp(unittest.TestCase):
    def test_known_ai_apps(self):
        """AI チャット・ターミナル・Cursor は AI とみなす（大小は区別しない）。"""
        for exe in ("claude.exe", "ChatGPT.exe", "WindowsTerminal.exe", "wt.exe", "powershell.exe",
                    "pwsh.exe", "cmd.exe", "Cursor.exe"):
            self.assertTrue(ai_destination.is_ai_app(exe), exe)

    def test_ordinary_apps_and_browsers_are_not_ai(self):
        """ふつうのアプリ・ブラウザ（chatgpt.com を開いていても）・取れない場合は AI ではない。"""
        for exe in ("notepad.exe", "chrome.exe", "msedge.exe", "WINWORD.EXE", "slack.exe", "", None):
            self.assertFalse(ai_destination.is_ai_app(exe), exe)

    def test_should_skip_format(self):
        """省くのは「整形 ON × 設定 ON × AI 宛て」のときだけ。"""
        self.assertTrue(ai_destination.should_skip_format(True, True, "claude.exe"))
        self.assertFalse(ai_destination.should_skip_format(True, False, "claude.exe"))  # 設定 OFF
        self.assertFalse(ai_destination.should_skip_format(False, True, "claude.exe"))  # 整形 OFF
        self.assertFalse(ai_destination.should_skip_format(True, True, "notepad.exe"))
        self.assertFalse(ai_destination.should_skip_format(True, True, None))

    def test_default_on(self):
        """設定は既定 ON（Mac の skipFormatForAI と同じ）。"""
        self.assertTrue(DEFAULT_CONFIG["skip_format_for_ai"])


class TestSnapshotSkipsFormatForAI(unittest.TestCase):
    def test_ai_destination_disables_format_and_server_format(self):
        """AI 宛てなら整形もサーバー統合整形も OFF にし、アプリ名を snapshot に載せる。"""
        slot = _slot(format_enabled=True)
        s = _fake_self("claude.exe")
        with mock.patch("src.core.backend_client.is_logged_in", return_value=True):
            ctx = VoicekeyApp._snapshot_context(s, slot)
        self.assertFalse(ctx.slot.format_enabled)
        self.assertFalse(ctx.server_format)
        self.assertEqual(ctx.format_skip_app, "claude.exe")
        # 保存値（元のスロット）は書き換えない
        self.assertTrue(slot.format_enabled)

    def test_setting_off_keeps_format(self):
        """設定 OFF なら今までどおり整形する（前面アプリも読まない）。"""
        slot = _slot(format_enabled=True)
        s = _fake_self("claude.exe", {"skip_format_for_ai": False})
        ctx = VoicekeyApp._snapshot_context(s, slot)
        self.assertTrue(ctx.slot.format_enabled)
        self.assertIsNone(ctx.format_skip_app)
        self.assertEqual(s._platform.calls, 0)

    def test_non_ai_destination_keeps_format(self):
        slot = _slot(format_enabled=True)
        ctx = VoicekeyApp._snapshot_context(_fake_self("notepad.exe"), slot)
        self.assertTrue(ctx.slot.format_enabled)
        self.assertIsNone(ctx.format_skip_app)

    def test_format_off_slot_does_not_look_up_foreground(self):
        """整形 OFF の録音キーは前面アプリを読まない（録音開始に何も足さない）。"""
        s = _fake_self("claude.exe")
        ctx = VoicekeyApp._snapshot_context(s, _slot(format_enabled=False))
        self.assertFalse(ctx.slot.format_enabled)
        self.assertIsNone(ctx.format_skip_app)
        self.assertEqual(s._platform.calls, 0)

    def test_practice_override_is_not_skipped(self):
        """オンボーディングの整形体験中は省かない（voicekey 自身の窓が前面のため）。"""
        ctx = VoicekeyApp._snapshot_context(_fake_self("claude.exe", practice=True), _slot(format_enabled=False))
        self.assertTrue(ctx.slot.format_enabled)
        self.assertIsNone(ctx.format_skip_app)

    def test_foreground_failure_keeps_format(self):
        """前面アプリが取れない（例外）ときは整形する（安全側）。"""
        s = _fake_self(None)
        s._platform.foreground_exe = mock.Mock(side_effect=OSError("boom"))
        ctx = VoicekeyApp._snapshot_context(s, _slot(format_enabled=True))
        self.assertTrue(ctx.slot.format_enabled)

    def test_maybe_format_does_not_call_formatter_for_ai(self):
        """AI 宛ての回は整形 LLM を呼ばない（原文がそのまま返る）。"""
        ctx = VoicekeyApp._snapshot_context(_fake_self("WindowsTerminal.exe"), _slot(format_enabled=True))
        with mock.patch("src.core.text_formatter.format_text") as fmt:
            out = VoicekeyApp._maybe_format(SimpleNamespace(), "えーと、これを直して", ctx)
        fmt.assert_not_called()
        self.assertEqual(out, "えーと、これを直して")


class TestTimelineSkipNote(unittest.TestCase):
    def test_summary_line_shows_skip_reason(self):
        """[計測] の整形欄は、省いた回だけ時間の代わりに理由を出す（空白を含めない）。"""
        tl = diagnostics.DictationTimeline(generation=3, slot=1, backend="groq", model="m", pressed_at=0.0)
        tl.format_skip_note = ai_destination.SKIPPED_LABEL
        self.assertIn(" 整形=省略(AI宛て) ", tl.summary_line())
        self.assertNotIn(" ", ai_destination.SKIPPED_LABEL)
        plain = diagnostics.DictationTimeline(generation=4, slot=1, backend="groq", model="m", pressed_at=0.0)
        self.assertIn(" 整形=- ", plain.summary_line())


if __name__ == "__main__":
    unittest.main()
