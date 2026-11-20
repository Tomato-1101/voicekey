"""「貼り付け先が AI のときは整形しない」のテスト（core/ai_destination と VoicekeyApp）。

判定は録音開始時の最前面アプリの実行ファイル名だけで決まる純関数。Windows 版は録音開始時に
別スレッドで控えた前面アプリ（ctx.target_app）を整形の直前（_maybe_format）で見て、AI 宛てなら
整形（text_formatter.format_text）を呼ばずに原文を返すことを確かめる（OS への問い合わせは足さない）。

VoicekeyApp は QObject 派生で素の生成ができないため、test_task_context_snapshot と同じく
対象メソッドが参照する属性だけを持つ SimpleNamespace を self に見立てて呼ぶ。
"""

import unittest
from types import SimpleNamespace
from unittest import mock

from src.app import HotkeySlot, TaskContext, VoicekeyApp, _TaskRun
from src.config.constants import DEFAULT_CONFIG
from src.core import ai_destination
from src.core.dictation_timeline import DictationTimeline


class _FakeConfig:
    """snapshot で読まれる設定値を返す最小 config。"""

    def __init__(self, values: dict):
        self._values = values

    def get(self, key, default=None):
        return self._values.get(key, default)


def _slot(backend: str = "groq", format_enabled: bool = True) -> HotkeySlot:
    return HotkeySlot(
        slot_id=1, hotkey="<f2>", hotkey_mode="hold", required_keys={"f2"},
        backend=backend, api_model="m", api_prompt="", format_enabled=format_enabled, format_preset="standard",
        transcriber=mock.Mock(),
    )


def _fake_self(config: dict = None, practice: bool = False):
    return SimpleNamespace(
        _config=_FakeConfig(config or {}),
        _practice_format_override=practice,
    )


def _ctx(target_app=None, skip_for_ai: bool = True, format_enabled: bool = True) -> TaskContext:
    """録音開始時の snapshot を模す（target_app は録音開始時に控えた (表示名, パス)）。"""
    return TaskContext(
        slot=_slot(format_enabled=format_enabled),
        language="ja",
        vad_on=False,
        split_on=False,
        volume_normalize=False,
        format_model="qwen/qwen3.8-27b",
        format_auto_prompt="",
        target_app=target_app,
        skip_format_for_ai=skip_for_ai,
    )


_CLAUDE = ("claude", "C:\\Users\\me\\AppData\\Local\\AnthropicClaude\\claude.exe")
_NOTEPAD = ("notepad", "C:\\Windows\\System32\\notepad.exe")


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


class TestSnapshotTakesSetting(unittest.TestCase):
    def test_setting_is_snapshotted(self):
        """設定の ON/OFF は録音開始時の snapshot に写される（処理中に変えても影響しない）。"""
        self.assertTrue(VoicekeyApp._snapshot_context(_fake_self({"skip_format_for_ai": True}), _slot()).skip_format_for_ai)
        self.assertFalse(VoicekeyApp._snapshot_context(_fake_self({"skip_format_for_ai": False}), _slot()).skip_format_for_ai)

    def test_default_is_on(self):
        """設定が無いときは ON 扱い。"""
        self.assertTrue(VoicekeyApp._snapshot_context(_fake_self({}), _slot()).skip_format_for_ai)

    def test_practice_override_is_not_skipped(self):
        """オンボーディングの整形体験中は AI 宛てでも整形する（体験にならないため）。"""
        ctx = VoicekeyApp._snapshot_context(_fake_self({"skip_format_for_ai": True}, practice=True), _slot())
        self.assertFalse(ctx.skip_format_for_ai)


class TestMaybeFormatSkipsForAI(unittest.TestCase):
    def _run(self, ctx, run=None):
        with mock.patch("src.app.text_formatter.format_text", return_value="整形済み") as fmt:
            out = VoicekeyApp._maybe_format(SimpleNamespace(), "えーと、これを直して", ctx, run)
        return out, fmt

    def test_ai_destination_returns_original(self):
        """貼り付け先が AI なら整形を呼ばずに原文を返す。"""
        out, fmt = self._run(_ctx(target_app=_CLAUDE))
        self.assertEqual(out, "えーと、これを直して")
        fmt.assert_not_called()

    def test_setting_off_formats(self):
        """設定 OFF なら AI 宛てでも整形する。"""
        out, fmt = self._run(_ctx(target_app=_CLAUDE, skip_for_ai=False))
        self.assertEqual(out, "整形済み")
        fmt.assert_called_once()

    def test_non_ai_destination_formats(self):
        """ふつうのアプリなら整形する。"""
        out, fmt = self._run(_ctx(target_app=_NOTEPAD))
        self.assertEqual(out, "整形済み")
        fmt.assert_called_once()

    def test_unknown_destination_formats(self):
        """前面アプリがまだ取れていない回は AI とみなさず整形する（安全側）。"""
        out, fmt = self._run(_ctx(target_app=None))
        self.assertEqual(out, "整形済み")
        fmt.assert_called_once()

    def test_format_off_slot_does_nothing(self):
        """録音キーの整形が OFF なら、AI 判定に関係なく整形しない。"""
        out, fmt = self._run(_ctx(target_app=_NOTEPAD, format_enabled=False))
        self.assertEqual(out, "えーと、これを直して")
        fmt.assert_not_called()

    def test_skip_is_noted_in_timeline(self):
        """省いた回は計測の 1 行の整形欄に理由が出る（時間の代わりに）。"""
        run = _TaskRun(timeline=DictationTimeline())
        self._run(_ctx(target_app=_CLAUDE), run)
        self.assertEqual(run.timeline.format_skip_note, ai_destination.SKIPPED_LABEL)
        self.assertIn(f"整形={ai_destination.SKIPPED_LABEL}", run.timeline.summary_line())
        self.assertEqual(run.usage, [])


class TestTimelineSkipNote(unittest.TestCase):
    def test_summary_line_shows_ms_without_skip(self):
        """省いていない回はこれまでどおり時間を出す。"""
        timeline = DictationTimeline(format_ms=123)
        self.assertIn("整形=123ms", timeline.summary_line())


if __name__ == "__main__":
    unittest.main()
