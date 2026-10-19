"""Meet 議事録ボット（Windows）のテスト。

Mac の CaptionSettleTrackerTests / MeetBotRulesTests / MeetBotPoliciesTests / MeetBotURLTests と同じ観点を移し、
さらに Chrome を模擬した CDP で「参加 → 字幕を読む → 退出 → 議事録 1 ファイル」の通しを確かめる。
本物の Chrome・Meet には接続しない。
"""

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from src.caption.meetbot import cdp, rules, scripts
from src.caption.meetbot.settle import CaptionSettleTracker, Entry, Line, is_revision, new_tail
from src.caption.transcript_recorder import TranscriptRecorder


def wait_until(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


class TestSettleTracker(unittest.TestCase):
    def setUp(self):
        self.t = CaptionSettleTracker(2.5)

    def test_does_not_settle_while_growing(self):
        self.assertEqual(self.t.ingest([Entry("山田", "こんに")], 0), [])
        self.assertEqual(self.t.ingest([Entry("山田", "こんにちは")], 1), [])
        self.assertEqual(self.t.ingest([Entry("山田", "こんにちは今日は")], 2), [])

    def test_settles_after_quiet(self):
        self.t.ingest([Entry("山田", "本日の議題は")], 0)
        self.t.ingest([Entry("山田", "本日の議題は三つです")], 1)
        self.assertEqual(self.t.ingest([Entry("山田", "本日の議題は三つです")], 4), [Line("山田", "本日の議題は三つです")])

    def test_does_not_emit_twice_while_lingering(self):
        self.t.ingest([Entry("山田", "一度きり")], 0)
        self.assertEqual(self.t.ingest([Entry("山田", "一度きり")], 3), [Line("山田", "一度きり")])
        for offset in (6, 9, 12):
            self.assertEqual(self.t.ingest([Entry("山田", "一度きり")], offset), [])

    def test_repeated_phrase_after_another_utterance(self):
        self.t.ingest([Entry("山田", "はい")], 0)
        self.t.ingest([Entry("山田", "はい")], 3)
        self.t.ingest([Entry("山田", "では次にいきます")], 4)
        self.t.ingest([Entry("山田", "では次にいきます")], 7)
        self.assertEqual(self.t.ingest([Entry("山田", "はい")], 8), [])
        self.assertEqual(self.t.ingest([Entry("山田", "はい")], 11), [Line("山田", "はい")])

    def test_settles_immediately_on_new_utterance(self):
        self.t.ingest([Entry("山田", "前の発言です")], 0)
        self.assertEqual(self.t.ingest([Entry("山田", "次の話に移ります")], 1), [Line("山田", "前の発言です")])

    def test_speakers_separately_and_missing_speaker(self):
        both = [Entry("山田", "私は賛成です"), Entry("佐藤", "私は反対です")]
        self.t.ingest(both, 0)
        settled = self.t.ingest(both, 3)
        self.assertEqual(set(settled), {Line("山田", "私は賛成です"), Line("佐藤", "私は反対です")})
        t = CaptionSettleTracker(2.5)
        t.ingest([Entry("", "誰かの発言")], 0)
        self.assertEqual(t.ingest([Entry("", "誰かの発言")], 3), [Line(None, "誰かの発言")])

    def test_two_blocks_of_same_speaker_do_not_duplicate(self):
        screen = [Entry("山田", "前の発言です"), Entry("山田", "次の話に移ります")]
        emitted = []
        for second in range(12):
            emitted += self.t.ingest(screen, second)
        emitted += self.t.flush()
        self.assertEqual(emitted, [Line("山田", "前の発言です"), Line("山田", "次の話に移ります")])

    def test_growth_after_settle_emits_only_rest(self):
        self.t.ingest([Entry("山田", "まず予算です")], 0)
        self.assertEqual(self.t.ingest([Entry("山田", "まず予算です")], 3), [Line("山田", "まず予算です")])
        self.t.ingest([Entry("山田", "まず予算です 次に日程です")], 4)
        self.assertEqual(self.t.ingest([Entry("山田", "まず予算です 次に日程です")], 7), [Line("山田", "次に日程です")])

    def test_revisions(self):
        self.t.ingest([Entry("Alice", "hello everyone we")], 0)
        self.assertEqual(self.t.ingest([Entry("Alice", "Hello everyone, we will start now.")], 1), [])
        self.assertEqual(self.t.ingest([Entry("Alice", "Hello everyone, we will start now.")], 4),
                         [Line("Alice", "Hello everyone, we will start now.")])
        t = CaptionSettleTracker(2.5)
        t.ingest([Entry("山田", "今日は会議を始めま")], 0)
        self.assertEqual(t.ingest([Entry("山田", "今日の会議を始めます")], 1), [])
        self.assertEqual(t.ingest([Entry("山田", "今日の会議を始めます")], 4), [Line("山田", "今日の会議を始めます")])
        self.assertFalse(is_revision("前の発言です", "次の話に移ります"))
        self.assertFalse(is_revision("thanks everyone", "next item is budget"))
        self.assertTrue(is_revision("hello everyone we", "Hello everyone, we will"))

    def test_flush(self):
        self.t.ingest([Entry("山田", "まだ途中の発言")], 0)
        self.assertEqual(self.t.flush(), [Line("山田", "まだ途中の発言")])
        self.assertEqual(self.t.flush(), [])
        self.assertEqual(self.t.ingest([Entry("山田", "   ")], 0), [])

    def test_region(self):
        self.assertEqual(self.t.ingest_region("Good morning everyone.", 0), [])
        self.assertEqual(self.t.ingest_region("Good morning everyone.", 3), [Line(None, "Good morning everyone.")])
        grown = "Good morning everyone. Let's begin with the budget."
        self.assertEqual(self.t.ingest_region(grown, 4), [])
        self.assertEqual(self.t.ingest_region(grown, 7), [Line(None, "Let's begin with the budget")])
        self.assertEqual(self.t.ingest_region(grown, 10), [])
        self.assertEqual(new_tail("second line. third line", "first line. second line."), "third line")
        self.assertEqual(new_tail("全く別の内容", "first line"), "全く別の内容")
        self.assertEqual(new_tail("最初の字幕", None), "最初の字幕")

    def test_region_forces_settle(self):
        text, emitted = "a", []
        for second in range(17):
            text += f" word{second}"
            emitted += self.t.ingest_region(text, second)
        self.assertEqual(len(emitted), 1)


class TestRules(unittest.TestCase):
    def test_page_status(self):
        self.assertEqual(rules.page_status(True, "参加をリクエストしています"), rules.IN_MEETING)
        self.assertEqual(rules.page_status(False, "参加をリクエストしています"), rules.WAITING_APPROVAL)
        self.assertEqual(rules.page_status(False, "Someone denied your request"), rules.DENIED)
        self.assertEqual(rules.page_status(False, "You've been removed from the meeting"), rules.REMOVED)
        self.assertEqual(rules.page_status(False, "メッセージが削除されました"), rules.OTHER)
        self.assertEqual(rules.page_status(False, "The call has ended"), rules.ENDED)
        self.assertEqual(rules.page_status(False, "Check your meeting code"), rules.NOT_FOUND)
        self.assertEqual(rules.page_status(False, "読み込み中"), rules.OTHER)

    def test_caption_toggle(self):
        self.assertEqual(rules.caption_toggle([], True, False), ("alreadyOn", None))
        self.assertEqual(rules.caption_toggle([("字幕をオフにする", None)], False, False), ("alreadyOn", None))
        self.assertEqual(rules.caption_toggle([("Captions", True)], False, False), ("alreadyOn", None))
        self.assertEqual(rules.caption_toggle([("字幕をオンにする", None)], False, False), ("click", "字幕をオンにする"))
        self.assertEqual(rules.caption_toggle([("Turn on captions", False)], False, False),
                         ("click", "Turn on captions"))
        self.assertEqual(rules.caption_toggle([], False, False), ("shortcut", None))
        self.assertEqual(rules.caption_toggle([], False, True), ("unknown", None))

    def test_logged_in(self):
        now = 1_800_000_000
        self.assertTrue(rules.is_logged_in([{"name": "SID", "domain": ".google.com", "expires": -1}], now))
        self.assertFalse(rules.is_logged_in([{"name": "SID", "domain": ".google.com", "expires": now - 10}], now))
        self.assertFalse(rules.is_logged_in([{"name": "SID", "domain": "example.com", "expires": -1}], now))
        self.assertTrue(rules.is_logged_in([{"name": "SAPISID", "domain": "google.com", "expires": now + 10}], now))

    def test_policies(self):
        streak = rules.TimeoutStreak()
        self.assertFalse(streak.record(True))
        self.assertFalse(streak.record(False))
        self.assertFalse(streak.record(True))
        self.assertFalse(streak.record(True))
        self.assertTrue(streak.record(True))
        watch = rules.MeetingPresenceWatch()
        self.assertIsNone(watch.observe(rules.REMOVED))
        self.assertEqual(watch.observe(rules.REMOVED), rules.REMOVED)
        watch = rules.MeetingPresenceWatch()
        self.assertIsNone(watch.observe(rules.OTHER))
        self.assertIsNone(watch.observe(rules.ENDED))
        self.assertEqual(watch.observe(rules.OTHER), rules.ENDED)
        caption = rules.CaptionPresenceWatch(0)
        self.assertIsNone(caption.observe(False, 59))
        self.assertEqual(caption.observe(False, 60), "warn")
        self.assertEqual(caption.observe(True, 61), "recovered")
        self.assertIsNone(caption.observe(False, 200), "1 会議に 1 回だけ")
        retry = rules.JoinRetry()
        self.assertTrue(retry.should_press(rules.OTHER, 0))
        retry.record_press(False, 0)
        self.assertFalse(retry.should_press(rules.OTHER, 3))
        self.assertTrue(retry.should_press(rules.OTHER, 4))
        self.assertFalse(retry.should_press(rules.WAITING_APPROVAL, 10))
        for t in range(4):
            retry.record_press(True, 10 + t * 5)
        self.assertFalse(retry.should_press(rules.OTHER, 100), "5 回まで")
        self.assertTrue(retry.button_found)
        self.assertEqual(rules.join_timeout_error(True, False).kind, "approvalTimeout")
        self.assertEqual(rules.join_timeout_error(False, True).kind, "couldNotEnter")
        self.assertEqual(rules.join_timeout_error(False, False).kind, "joinButtonNotFound")

    def test_closing_message(self):
        self.assertIn("保存できませんでした", rules.closing_message("退出", True, 3, "disk full"))
        self.assertIn("0 行", rules.closing_message("退出", True, 0, None))
        self.assertIn("（5 行）", rules.closing_message("退出", True, 5, None))

    def test_generation(self):
        g = rules.JoinGeneration()
        first = g.begin()
        g.invalidate()
        self.assertFalse(g.is_current(first))
        second = g.begin()
        self.assertTrue(g.is_current(second))

    def test_urls(self):
        self.assertEqual(rules.normalized_meet_url(" meet.google.com/abc-defg-hij "), "https://meet.google.com/abc-defg-hij")
        self.assertIsNotNone(rules.normalized_meet_url("https://meet.google.com/lookup/team-sync"))
        self.assertIsNone(rules.normalized_meet_url("https://meet.google.com/"))
        self.assertIsNone(rules.normalized_meet_url("https://meet.google.com/landing"))
        self.assertIsNone(rules.normalized_meet_url("https://example.com/abc-defg-hij"))
        self.assertEqual(rules.meeting_code("https://meet.google.com/abc-defg-hij?authuser=0"), "abc-defg-hij")

    def test_stale_chrome_pids(self):
        profile = r"C:\Users\me\AppData\Roaming\voicekey\meetbot-profile"
        listing = "\n".join([
            rf'101 "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9333 --user-data-dir={profile} --headless=new',
            rf'102 "C:\Program Files\Google\Chrome\Application\chrome.exe" --type=renderer --user-data-dir="{profile}" --lang=ja',
            rf'103 "C:\Program Files\Google\Chrome\Application\chrome.exe" --user-data-dir={profile}2',
            r'104 "C:\Program Files\Google\Chrome\Application\chrome.exe"',
            rf'105 "C:\Program Files\Google\Chrome\Application\chrome.exe" --user-data-dir={profile.upper()}',
            "garbage line",
        ])
        self.assertEqual(rules.stale_chrome_pids(listing, profile), [101, 102, 105])

    def test_scripts_embed_safely(self):
        js = scripts.join_script("bot's \"name\"")
        self.assertIn('"bot\'s \\"name\\""', js)
        self.assertIn("今すぐ参加", js)
        self.assertIn('jsname="tgaKEf"', scripts.CAPTION_SCRIPT)
        self.assertIn("\\s+", scripts.CAPTION_SCRIPT)
        self.assertIn("'日本語'", scripts.caption_language_script("ja").replace('"', "'"))

    def test_launch_args(self):
        args = cdp.launch_args("chrome.exe", 9333, r"C:\p", headless=True, mute_audio=True)
        self.assertIn("--remote-debugging-port=9333", args)
        self.assertIn(r"--user-data-dir=C:\p", args)
        self.assertIn("--headless=new", args)
        self.assertIn("--mute-audio", args)
        self.assertIn("--deny-permission-prompts", args)
        visible = cdp.launch_args("chrome.exe", 9333, r"C:\p", headless=False,
                                  initial_url="https://accounts.google.com/")
        self.assertNotIn("--headless=new", visible)
        self.assertEqual(visible[-1], "https://accounts.google.com/")


class FakeCDPServer:
    """CDP の WebSocket を模擬する（Runtime.evaluate に決まった値を返す・1 つは応答しない）。"""

    def __init__(self):
        from websockets.sync.server import serve

        self._server = serve(self._handler, "127.0.0.1", 0)
        self.port = self._server.socket.getsockname()[1]
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def close(self):
        self._server.shutdown()

    def _handler(self, ws):
        try:
            for message in ws:
                data = json.loads(message)
                expr = data.get("params", {}).get("expression", "")
                if expr == "hang":
                    continue  # 応答しない（Chrome が固まった）
                if expr == "throw":
                    ws.send(json.dumps({"id": data["id"], "result": {"exceptionDetails": {"text": "boom"}}}))
                    continue
                ws.send(json.dumps({"id": data["id"], "result": {"result": {"type": "number", "value": 42}}}))
        except Exception:
            pass


class TestChromeDevToolsClient(unittest.TestCase):
    def test_evaluate_timeout_and_exception(self):
        server = FakeCDPServer()
        self.addCleanup(server.close)
        tools = cdp.ChromeDevTools()
        tools.connect(f"ws://127.0.0.1:{server.port}")
        self.addCleanup(tools.close)
        self.assertEqual(tools.evaluate("1+1"), 42)
        start = time.monotonic()
        with self.assertRaises(cdp.ChromeDevToolsError) as ctx:
            tools.evaluate("hang", timeout=0.3)
        self.assertEqual(ctx.exception.kind, "timedOut")
        self.assertLess(time.monotonic() - start, 2.0)
        with self.assertRaises(cdp.ChromeDevToolsError) as ctx:
            tools.evaluate("throw")
        self.assertEqual(ctx.exception.kind, "protocol")

    def test_disconnect_is_reported(self):
        server = FakeCDPServer()
        tools = cdp.ChromeDevTools()
        gone = threading.Event()
        tools.on_disconnect = gone.set
        tools.connect(f"ws://127.0.0.1:{server.port}")
        server.close()
        self.assertTrue(gone.wait(5.0))
        tools.close()


class _FakeProcess:
    def __init__(self):
        self.pid = 4242
        self._done = threading.Event()

    def poll(self):
        return 0 if self._done.is_set() else None

    def wait(self, timeout=None):
        self._done.wait(timeout)
        return 0

    def terminate(self):
        self._done.set()

    def kill(self):
        self._done.set()


class _FakeTools:
    """会議ページの代わり（式の中身で返事を決める）。"""

    def __init__(self, page):
        self.page = page
        self.on_disconnect = None
        self.closed = False

    def connect(self, url):
        pass

    def close(self):
        self.closed = True

    def send(self, method, params=None, timeout=10.0):
        if method == "Storage.getCookies":
            return {"cookies": [{"name": "SID", "domain": ".google.com", "expires": -1}] if self.page.logged_in else []}
        return {}

    def evaluate(self, expression, timeout=10.0):
        page = self.page
        if expression == scripts.CONSENT_SCRIPT:
            return {"clicked": None}
        if expression == scripts.LEFT_MEETING_PAGE_SCRIPT:
            return False
        if expression == scripts.PAGE_STATUS_SCRIPT:
            return {"inMeeting": page.in_meeting, "text": "" if page.in_meeting else page.lobby_text}
        if "今すぐ参加" in expression:
            page.join_presses += 1
            page.in_meeting = True
            return {"clicked": "今すぐ参加"}
        if expression == "document.title":
            return "定例ミーティング - Google Meet"
        if expression == scripts.ENSURE_MUTED_SCRIPT:
            return 0
        if expression == scripts.CAPTION_STATE_SCRIPT:
            return {"buttons": [], "regionVisible": True}
        if expression == scripts.CAPTION_SCRIPT:
            return {"found": True, "captionsOn": True, "source": "jsname",
                    "entries": [{"speaker": s, "text": t} for s, t in page.captions]}
        if expression == scripts.LEAVE_SCRIPT:
            page.left = True
            return {"clicked": "通話から退出"}
        return None


class _FakePage:
    def __init__(self, logged_in=True):
        self.logged_in = logged_in
        self.in_meeting = False
        self.lobby_text = "会議の準備はできましたか"
        self.join_presses = 0
        self.captions = []
        self.left = False


class _FakeCDP:
    """cdp モジュールの代わり（本物の Chrome を起こさない）。"""

    ChromeDevToolsError = cdp.ChromeDevToolsError

    def __init__(self, page):
        self.page = page
        self.launched = []

    def terminate_stale_chrome(self, profile):
        return 0

    def is_answering(self, port):
        return False

    def launch_chrome(self, port, profile, headless, initial_url=None, mute_audio=False):
        self.launched.append({"headless": headless, "mute_audio": mute_audio})
        return _FakeProcess()

    def wait_for_devtools(self, port, timeout=20.0, should_stop=lambda: False):
        return None

    def browser_socket_url(self, port):
        return "ws://fake/browser"

    def open_tab(self, url, port):
        return "ws://fake/page"

    def list_page_urls(self, port):
        return []

    def ChromeDevTools(self):  # noqa: N802 - 本物と同じ呼び方
        return _FakeTools(self.page)


class TestMeetBotServiceFlow(unittest.TestCase):
    def setUp(self):
        from PySide6.QtWidgets import QApplication

        self.app = QApplication.instance() or QApplication([])
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        import src.caption.meetbot.service as service_module

        self.service_module = service_module
        # 待ち時間を詰める（4 秒のロビー待ちなどを短くする）
        self._sleep = service_module.MeetBotService._sleep
        service_module.MeetBotService._sleep = lambda self, gen, seconds: self._ensure_current(gen)
        self.addCleanup(lambda: setattr(service_module.MeetBotService, "_sleep", self._sleep))
        self._settle = service_module.SETTLE_SECONDS

    def _service(self, page):
        from src.caption.meetbot.service import MeetBotService

        config = {"meet_bot": {"source": "meetCaptions", "language": "ja"}}

        class _Config:
            def get(self, key, default=None):
                return config.get(key, default)

            def save(self, values):
                return True

        recorder = TranscriptRecorder(Path(self.tmp.name))
        service = MeetBotService(_Config(), recorder=recorder)
        service.cdp = _FakeCDP(page)
        self.notices = []
        service.notified.connect(lambda message, path: self.notices.append(message))
        # 本物の Chrome の有無に関係なく通す
        original = self.service_module.cdp.is_chrome_installed
        self.service_module.cdp.is_chrome_installed = lambda: True
        self.addCleanup(lambda: setattr(self.service_module.cdp, "is_chrome_installed", original))
        return service

    def _pump(self, predicate, timeout=10.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return True
            time.sleep(0.02)
        self.app.processEvents()
        return predicate()

    def test_join_record_and_leave_writes_one_meeting_file(self):
        page = _FakePage()
        service = self._service(page)
        service.join("meet.google.com/abc-defg-hij")
        self.assertTrue(self._pump(lambda: service.state == "recording"), service.status_line)
        self.assertEqual(service.cdp.launched[0], {"headless": True, "mute_audio": True})
        self.assertEqual(page.join_presses, 1)
        page.captions = [("山田", "本日の議題は三つです")]
        self.assertTrue(self._pump(lambda: service.recorded_lines >= 1, timeout=8.0), service.status_line)
        service.leave()
        self.assertTrue(self._pump(lambda: service.state == "idle" and any("保存しました" in n for n in self.notices)),
                        self.notices)
        self.assertTrue(page.left, "退出ボタンを押していない")
        files = list(Path(self.tmp.name).glob("*.md"))
        self.assertEqual(len(files), 1)
        self.assertIn("abc-defg-hij", files[0].name)
        body = files[0].read_text(encoding="utf-8")
        self.assertIn("**山田**: 本日の議題は三つです", body)
        self.assertIn("Google Meet / 定例ミーティング", body)

    def test_not_logged_in_fails_with_guidance(self):
        page = _FakePage(logged_in=False)
        service = self._service(page)
        service.join("https://meet.google.com/abc-defg-hij")
        # 状態が変わった後に通知が届く（別スレッドからの合図）ので、両方を待つ
        self.assertTrue(self._pump(lambda: service.state == "failed"
                                   and any("参加できませんでした" in n for n in self.notices)), self.notices)
        self.assertIn("ログインしていません", service.status_line)
        self.assertEqual(service.login_status, "loggedOut")

    def test_rejects_non_meeting_url(self):
        service = self._service(_FakePage())
        service.join("https://meet.google.com/")
        self.assertEqual(service.state, "failed")
        self.assertIn("会議 URL ではありません", service.status_line)


if __name__ == "__main__":
    unittest.main()
