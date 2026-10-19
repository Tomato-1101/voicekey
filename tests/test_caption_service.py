"""ライブ字幕サービス・字幕の表示・設定画面の「字幕と議事録」の結線テスト（Windows）。

音声の取り込みと API の接続は模擬に差し替える（外部通信なし・実デバイスなし）。
Qt はオフスクリーン。待ちは条件で待つ（スレッドのタイミングに頼らない）。
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import tempfile
import threading
import time
import unittest
from pathlib import Path

from PySide6.QtWidgets import QApplication

from src.caption import service as svc
from src.caption import settings as cs
from src.caption.caption_hud import CaptionHud, preferred_width
from src.caption.transcript_recorder import TranscriptRecorder
from src.ui.hud import Hud

_app = QApplication.instance() or QApplication([])


def pump_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    _app.processEvents()
    return predicate()


class FakeConfig:
    """ConfigManager の代わり（get / save だけ。save は deep merge）。"""

    def __init__(self, data=None):
        self.data = data or {}

    def get(self, key, default=None):
        return self.data.get(key, default)

    def save(self, values):
        for key, value in values.items():
            if isinstance(value, dict):
                merged = dict(self.data.get(key) or {})
                merged.update(value)
                self.data[key] = merged
            else:
                self.data[key] = value
        return True

    @property
    def config(self):
        return self.data


class FakeStream:
    PROVIDER = "Soniox"
    KEY_ITEM = "soniox"
    MODEL = "stt-rt-v5"

    def __init__(self, ok=True):
        self.ok = ok
        self.started = False
        self.stopped = False
        self.sent = 0
        self.on_live = self.on_confirmed = self.on_give_up = self.on_link_state = self.on_usage = None

    def start(self, api_key=None):
        self.started = True
        return self.ok

    def send(self, samples):
        self.sent += 1

    def stop(self, timeout=3.0):
        self.stopped = True


class FakeCapture:
    def __init__(self, fail=None):
        self.fail = fail
        self.started = threading.Event()
        self.stopped = False
        self.on_target_changed = self.on_failed = self.on_notice = None

    def start(self):
        if self.fail:
            raise RuntimeError(self.fail)
        self.started.set()
        if self.on_target_changed:
            self.on_target_changed("Google Chrome")

    def stop(self):
        self.stopped = True


class FakeHud:
    """字幕の表示の代わり（呼ばれた順に記録する）。"""

    def __init__(self):
        from PySide6.QtCore import QObject, Signal

        class _Sig(QObject):
            visibility_changed = Signal(bool)

        self._sig = _Sig()
        self.visibility_changed = self._sig.visibility_changed
        self.calls = []
        self.suppressed = False
        self.live_looks_confirmed = False

    def __getattr__(self, name):
        if name.startswith("show_") or name in ("clear", "set_scale", "set_shows_source"):
            return lambda *args: self.calls.append((name,) + args)
        raise AttributeError(name)

    def set_suppressed(self, value):
        self.suppressed = value


class TestCaptionService(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.recorder = TranscriptRecorder(Path(self.tmp.name))

    def _service(self, config, stream=None, capture=None):
        self.stream = stream or FakeStream()
        self.capture = capture or FakeCapture()
        self.hud = FakeHud()
        service = svc.CaptionService(
            FakeConfig(config), hud_factory=lambda: self.hud,
            stream_factory=lambda cfg: self.stream, capture_factory=lambda cfg, cb: self.capture,
            recorder=self.recorder)
        self.addCleanup(service.shutdown)
        return service

    def test_soniox_translation_shows_lines_and_saves_only_source(self):
        service = self._service({"caption": {"engine": "soniox", "save_transcript": True}})
        service.start()
        self.assertTrue(pump_until(lambda: service.state == svc.RUNNING), service.title)
        self.assertTrue(service._settings().ever_started, "一度開始したら次回から自動開始してよい印を残す")
        self.stream.on_live("Hello", "こん")
        self.stream.on_confirmed("Hello world.", "こんにちは世界。")
        self.assertTrue(pump_until(lambda: ("show_translation", "Hello world.", "こんにちは世界。") in self.hud.calls))
        self.assertIn(("show_partial", "Hello", "こん"), self.hud.calls)
        self.recorder.wait_until_written()
        body = next(Path(self.tmp.name).glob("*.md")).read_text(encoding="utf-8")
        self.assertIn("Hello world.", body)
        self.assertNotIn("こんにちは世界", body, "訳文は保存しない")
        self.assertIn("記録元: Google Chrome", body)

    def test_save_switch_off_does_not_write(self):
        service = self._service({"caption": {"engine": "soniox", "save_transcript": False}})
        service.start()
        self.assertTrue(pump_until(lambda: service.state == svc.RUNNING))
        self.stream.on_confirmed("Hello.", "やあ。")
        self.recorder.wait_until_written()
        self.assertEqual(list(Path(self.tmp.name).glob("*.md")), [])

    def test_groq_mode_sends_confirmed_to_translator(self):
        service = self._service({"caption": {"engine": "groq", "recognizer": "soniox"}})
        translated = threading.Event()

        def fake_translator(text, context, on_delta):
            translated.set()
            return "訳:" + text

        service._current_translator = lambda: fake_translator
        service.start()
        self.assertTrue(pump_until(lambda: service.state == svc.RUNNING))
        self.stream.on_confirmed("This is a complete sentence.", "")
        self.assertTrue(pump_until(lambda: any(c[0] == "show_translation" for c in self.hud.calls)))
        call = next(c for c in self.hud.calls if c[0] == "show_translation")
        self.assertEqual(call[1:], ("This is a complete sentence.", "訳:This is a complete sentence."))

    def test_translation_failure_shows_source_and_reason(self):
        service = self._service({"caption": {"engine": "groq"}})

        def failing(text, context, on_delta):
            from src.caption.translators import TranslationError

            raise TranslationError("missing_key", "Groq の API キーが未設定です（設定 › API キー）")

        service._current_translator = lambda: failing
        service.start()
        self.assertTrue(pump_until(lambda: service.state == svc.RUNNING))
        self.stream.on_confirmed("Something said here.", "")
        self.assertTrue(pump_until(lambda: any(c[0] == "show_source_only" for c in self.hud.calls)))
        self.assertTrue(pump_until(lambda: service.translation_error is not None))
        self.assertTrue(any("API キー" in line for line in service.status_lines()))

    def test_give_up_stops_with_reason_and_never_switches_engine(self):
        config = {"caption": {"engine": "soniox"}}
        service = self._service(config)
        service.start()
        self.assertTrue(pump_until(lambda: service.state == svc.RUNNING))
        self.stream.on_give_up("Soniox の残高不足")
        self.assertTrue(pump_until(lambda: service.state == svc.FAILED))
        self.assertIn("残高不足", service.title)
        self.assertEqual(service._settings().engine, cs.ENGINE_SONIOX, "エンジンを勝手に切り替えない")
        self.assertTrue(pump_until(lambda: self.stream.stopped and self.capture.stopped))

    def test_missing_key_fails_without_capture(self):
        service = self._service({"caption": {"engine": "soniox"}}, stream=FakeStream(ok=False))
        service.start()
        self.assertEqual(service.state, svc.FAILED)
        self.assertIn("API キー", service.title)
        self.assertFalse(self.capture.started.is_set())

    def test_capture_failure(self):
        service = self._service({"caption": {}}, capture=FakeCapture(fail="取り込めません"))
        service.start()
        self.assertTrue(pump_until(lambda: service.state == svc.FAILED))
        self.assertIn("取り込めません", service.title)

    def test_dictation_hides_and_stop_releases_pill(self):
        service = self._service({"caption": {"engine": "soniox"}})
        visible = []
        service.visibility_changed.connect(visible.append)
        service.start()
        self.assertTrue(pump_until(lambda: service.state == svc.RUNNING))
        service.set_dictation_active(True)
        self.assertTrue(self.hud.suppressed)
        service.set_dictation_active(False)
        self.assertFalse(self.hud.suppressed)
        service.stop()
        self.assertEqual(service.state, svc.STOPPED)
        self.assertIn(False, visible, "止めたら待機ピルを戻す合図を出す")

    def test_restart_only_when_session_settings_change(self):
        config = FakeConfig({"caption": {"engine": "groq", "groq_model": "openai/gpt-oss-20b"}})
        streams = []

        def make_stream(cfg):
            stream = FakeStream()
            streams.append(stream)
            return stream

        service = svc.CaptionService(config, hud_factory=FakeHud, stream_factory=make_stream,
                                     capture_factory=lambda cfg, cb: FakeCapture(), recorder=self.recorder)
        self.addCleanup(service.shutdown)
        service.start()
        self.assertTrue(pump_until(lambda: service.state == svc.RUNNING))
        # モデルだけの変更は次の文から効く（張り直さない）
        config.save({"caption": {"groq_model": "openai/gpt-oss-120b"}})
        service.apply_config()
        self.assertEqual(len(streams), 1)
        # Soniox への切り替えは音声の流し先ごと変わるので張り直す
        config.save({"caption": {"engine": "soniox"}})
        service.apply_config()
        self.assertTrue(pump_until(lambda: len(streams) == 2 and service.state == svc.RUNNING, timeout=5.0))

    def test_disabled_by_environment(self):
        service = self._service({"caption": {}})
        os.environ["VOICEKEY_CAPTION_DISABLE"] = "1"
        try:
            service.start()
        finally:
            del os.environ["VOICEKEY_CAPTION_DISABLE"]
        self.assertEqual(service.state, svc.STOPPED)


class TestCaptionHudWidget(unittest.TestCase):
    def test_grows_upward_from_pill_and_respects_lines(self):
        hud = CaptionHud(scale=1.0)
        self.addCleanup(hud.deleteLater)
        shown = []
        hud.visibility_changed.connect(shown.append)
        hud.show_translation("Hello.", "こんにちは。")
        self.assertTrue(hud.is_on_screen)
        self.assertEqual(shown, [True])
        from PySide6.QtGui import QGuiApplication

        geo = QGuiApplication.primaryScreen().availableGeometry()
        bottom = Hud.capsule_bottom_y(geo)
        self.assertEqual(hud.geometry().y() + hud.geometry().height(), bottom, "下端はピルのカプセル下端に揃える")
        one_line_h = hud.height()
        hud.show_translation("Second.", "二行目の訳です。")
        self.assertGreater(hud.height(), one_line_h, "行が増えたら上へ伸びる")
        self.assertEqual(hud.geometry().y() + hud.geometry().height(), bottom)
        # 音声入力中は隠す（行は残す）
        hud.set_suppressed(True)
        self.assertTrue(pump_until(lambda: not hud.is_on_screen))
        self.assertIn(False, shown)
        hud.set_suppressed(False)
        self.assertTrue(hud.is_on_screen)

    def test_width_scales_within_screen(self):
        self.assertEqual(preferred_width(1920, 1.0), 880)
        self.assertEqual(preferred_width(1000, 1.0), 620)
        self.assertEqual(preferred_width(1000, 2.0), 960)


class TestHudCaptionCovering(unittest.TestCase):
    def test_idle_pill_hides_while_caption_covers(self):
        hud = Hud(enabled=True)
        self.addCleanup(hud.deleteLater)
        hud._fullscreen_probe = lambda: False
        hud.always_visible = True
        modes = []
        hud.mode_changed.connect(modes.append)
        hud.set_state("idle")
        self.assertTrue(hud.isVisible())
        hud.caption_covering = True
        self.assertFalse(hud.isVisible(), "字幕が出ている間は待機ピルを出さない")
        self.assertEqual(hud._mode, "idle")
        # 録音は字幕に関係なく出す
        hud.set_state("recording")
        self.assertTrue(hud.isVisible())
        hud.set_state("idle")
        self.assertFalse(hud.isVisible())
        hud.caption_covering = False
        self.assertTrue(hud.isVisible(), "字幕が畳み終わったら待機ピルを戻す")
        self.assertIn("recording", modes)


class TestSettingsPages(unittest.TestCase):
    def test_pages_in_sidebar_and_round_trip(self):
        from src.ui.settings_window import SettingsWindow

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        from src.config.config_manager import ConfigManager

        manager = ConfigManager(str(Path(tmp.name) / "settings.yaml"))
        manager.save({"caption": {"mode": "transcribe", "language": "en", "engine": "gemini",
                                  "gemini_model": "gemini-3.5-flash-lite", "recognizer": "openai_live",
                                  "hud_scale": 1.5, "scope": "all", "save_transcript": False},
                      "meet_bot": {"source": "apiSpeech", "language": "en", "recognizer": "openai_live"}})
        window = SettingsWindow(config_manager=manager)
        self.addCleanup(window.deleteLater)
        titles = [t for t in window._nav_titles if t]
        self.assertIn("ライブ字幕", titles)
        self.assertIn("議事録・会議", titles)
        # Mac と同じ並び（音声入力の後・アプリの前）
        self.assertLess(titles.index("翻訳して入力"), titles.index("ライブ字幕"))
        self.assertLess(titles.index("議事録・会議"), titles.index("一般"))
        collected = window._collect_caption_config()
        caption, meet = collected["caption"], collected["meet_bot"]
        self.assertEqual(caption["mode"], "transcribe")
        self.assertEqual(caption["language"], "en")
        self.assertEqual(caption["engine"], "gemini")
        self.assertEqual(caption["recognizer"], "openai_live")
        self.assertEqual(caption["hud_scale"], 1.5)
        self.assertEqual(caption["scope"], "all")
        self.assertFalse(caption["save_transcript"])
        self.assertEqual(meet, {"source": "apiSpeech", "language": "en", "recognizer": "openai_live"})
        # 翻訳エンジンを Soniox にすると、認識エンジンの行は隠れる（Soniox が両方行う）
        window._caption_mode_combo.setCurrentIndex(window._caption_mode_combo.findData("translate"))
        window._set_caption_engine_choice(("soniox", ""))
        self.assertTrue(window._caption_recognizer_row.isHidden())
        window._set_caption_engine_choice(("groq", "openai/gpt-oss-20b"))
        self.assertFalse(window._caption_recognizer_row.isHidden())
        self.assertEqual(window._collect_caption_config()["caption"]["groq_model"], "openai/gpt-oss-20b")


if __name__ == "__main__":
    unittest.main()


class TestAppWiring(unittest.TestCase):
    """app.py の結線（字幕は開始するまで作らない・音声入力中は隠す・トレイの状態）。"""

    def test_hud_mode_hides_caption_and_tray_status(self):
        from types import SimpleNamespace

        from src.app import _DICTATION_HUD_MODES, VoicekeyApp

        calls = []
        fake_service = SimpleNamespace(set_dictation_active=calls.append, title="キャプチャ中",
                                       status_lines=lambda: ["Soniox 接続中"], is_active=True)
        app = SimpleNamespace(_caption_service=None, _meet_bot_service=None,
                              _config=FakeConfig({"meet_bot": {"source": "apiSpeech"}}))
        # 字幕を作っていない間は何もしない（音声入力の経路に足さない）
        VoicekeyApp._on_hud_mode_changed(app, "recording")
        self.assertEqual(VoicekeyApp._caption_tray_status(app), ("停止中", [], False))
        app._caption_service = fake_service
        for mode in ("recording", "transcribing", "notice", "idle"):
            VoicekeyApp._on_hud_mode_changed(app, mode)
        self.assertEqual(calls, [True, True, True, False])
        self.assertEqual(set(_DICTATION_HUD_MODES), {"recording", "recording_auto_enter", "transcribing", "notice"})
        self.assertEqual(VoicekeyApp._caption_tray_status(app), ("キャプチャ中", ["Soniox 接続中"], True))
        status = VoicekeyApp._meet_tray_status(app)
        self.assertEqual(status["source"], "会議の音声（API）")
        self.assertFalse(status["active"])


class TestFailureSignal(unittest.TestCase):
    def test_failed_signal_carries_reason(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        reasons = []
        service = svc.CaptionService(FakeConfig({"caption": {"engine": "soniox"}}), hud_factory=FakeHud,
                                     stream_factory=lambda cfg: FakeStream(ok=False),
                                     capture_factory=lambda cfg, cb: FakeCapture(),
                                     recorder=TranscriptRecorder(Path(tmp.name)))
        self.addCleanup(service.shutdown)
        service.failed.connect(reasons.append)
        service.start()
        self.assertEqual(len(reasons), 1)
        self.assertIn("API キー", reasons[0])
