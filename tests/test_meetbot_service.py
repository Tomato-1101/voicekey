"""
Meet ボット本体の状態遷移のテスト（Chrome も通信も使わない）

偽のブラウザ（偽 CDP）を差し込み、参加 → 記録 → 停止、弾かれた場合、Chrome が無い場合などで、
画面へ届くシグナルの並びと議事録の中身を確かめる。
"""

import tempfile
import threading
import unittest
from pathlib import Path

from PySide6.QtCore import Qt

from src.meetbot import rules, scripts, summarizer, transcript
from src.meetbot.chrome import CDPDisconnected, CDPError, ChromeNotFound
from src.meetbot.service import SUMMARY_UNREADABLE, MeetBotService, Timing

URL = "https://meet.google.com/abc-defg-hij"

# 待ち時間を詰めた一式（実時間で待たない）
FAST = Timing(
    devtools_wait=0.5, ready_poll=0.001, page_load=0.001, consent_wait=0.001, join_poll=0.001, join_limit=0.3,
    join_retry=0.0, caption_retry=0.001, caption_poll=0.002, status_poll=0.0, leave_wait=0.0,
    login_poll=0.002, login_cookie=0.0, login_limit=2.0,
)

IN_MEETING = {"inMeeting": True, "text": "", "signInPage": False, "joinControls": False}
LOBBY = {"inMeeting": False, "text": "参加の準備はできましたか", "signInPage": False, "joinControls": True}
WAITING = {"inMeeting": False, "text": "参加をリクエストしています", "signInPage": False, "joinControls": False}
DENIED = {"inMeeting": False, "text": "参加リクエストが拒否されました", "signInPage": False, "joinControls": False}
ENDED = {"inMeeting": False, "text": "通話は終了しました", "signInPage": False, "joinControls": False}


def captions(*entries):
    """字幕の読み取り結果を作る。"""
    return {
        "found": True, "captionsOn": True, "source": "jsname",
        "entries": [{"speaker": speaker, "text": text} for speaker, text in entries],
    }


class FakePage:
    """
    偽の会議タブ。スクリプトの種類ごとに、用意した返事を順に返す（最後の 1 つは繰り返す）。
    """

    def __init__(self, statuses, caption_frames=()):
        self.statuses = list(statuses)
        self.caption_frames = list(caption_frames)
        self.calls = []
        self.closed = False
        self.gate = None  # 字幕を読み始めたら立つ合図（テストが記録中まで待つのに使う）
        self.fail_captions_with = None

    def _next(self, items):
        return items.pop(0) if len(items) > 1 else (items[0] if items else None)

    def evaluate(self, java_script):
        self.calls.append(java_script)
        if java_script == scripts.PAGE_STATUS:
            return self._next(self.statuses)
        if java_script == scripts.CAPTIONS:
            if self.fail_captions_with is not None and len(self.caption_frames) <= 1:
                raise self.fail_captions_with
            # 最後の 1 つを返す回になってから合図する（その前だと、読み切る前にテストが止めてしまう）
            last = len(self.caption_frames) <= 1
            frame = self._next(self.caption_frames)
            if self.gate is not None and last:
                self.gate.set()
            return frame
        if java_script == scripts.join(rules.BOT_DISPLAY_NAME):
            return {"clicked": True, "nameFilled": True}
        if java_script == scripts.CAPTION_STATE:
            return {"buttons": [], "regionVisible": True}
        if java_script == scripts.TITLE:
            return "週次定例 - Google Meet"
        if java_script == scripts.LEFT_MEETING_PAGE:
            return False
        return {"clicked": False}

    def close(self):
        self.closed = True


class FakeBrowser:
    """偽のボット用 Chrome。"""

    port = 9333

    def __init__(self, page=None, cookies=(), busy=False, launch_error=None, page_counts=()):
        self.page = page
        self._cookies = list(cookies)
        self.busy = busy
        self.launch_error = launch_error
        self.page_counts = list(page_counts)
        self.launch_arguments = None
        self.launched = False
        self.closed = False
        self.denied = []

    def close_leftover(self):
        return False

    def is_answering(self):
        return self.busy or (self.launched and not self.closed)

    def launch(self, headless, mute, initial_url=None):
        if self.launch_error is not None:
            raise self.launch_error
        self.launch_arguments = (headless, mute, initial_url)
        self.launched = True

    def is_running(self):
        return self.launched and not self.closed

    def cookies(self):
        return self._cookies

    def deny_media(self, origin):
        self.denied.append(origin)

    def open_page(self, url):
        self.opened = url
        return self.page

    def page_count(self):
        if len(self.page_counts) > 1:
            return self.page_counts.pop(0)
        return self.page_counts[0] if self.page_counts else 0

    def close(self):
        self.closed = True


class ServiceTestCase(unittest.TestCase):
    """シグナルを全部記録するサービスを用意する。"""

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.directory = Path(self._temp.name) / "transcripts"
        self.events = []
        self.lock = threading.Lock()

    def make(self, browser, chat=None, config=None):
        """偽のブラウザを使うサービスを作り、シグナルを記録につなぐ。"""
        service = MeetBotService(
            config=config, browser_factory=lambda: browser, directory=self.directory, timing=FAST, chat=chat,
        )
        self.addCleanup(service.shutdown, 2.0)
        for name in (
            "state_changed", "line_confirmed", "finished", "notice",
            "summary_progress", "summary_finished", "summary_failed", "login_state_changed",
        ):
            # 画面のイベントループが無いので、出したスレッドの上でそのまま受ける
            getattr(service, name).connect(
                lambda *args, name=name: self._record(name, args), Qt.ConnectionType.DirectConnection,
            )
        return service

    def _record(self, name, args):
        with self.lock:
            self.events.append((name, args))

    def of(self, name):
        """その種類のシグナルの引数を、届いた順に返す。"""
        with self.lock:
            return [args for event, args in self.events if event == name]

    def states(self):
        """届いた状態を、同じ状態の連続を 1 つにまとめて返す。"""
        result = []
        for state, _ in self.of("state_changed"):
            if not result or result[-1] != state:
                result.append(state)
        return result

    def wait(self, service, thread_name="_thread"):
        """作業スレッドが終わるまで待つ。"""
        thread = getattr(service, thread_name)
        thread.join(5.0)
        self.assertFalse(thread.is_alive(), "作業スレッドが終わること")


class MeetingFlowTests(ServiceTestCase):
    """参加 → 記録 → 終了。"""

    def test_join_record_stop(self):
        """ゲストで参加し、字幕を議事録に書き、停止で退出して保存先を知らせる。"""
        page = FakePage(
            [LOBBY, IN_MEETING],
            [captions(("山田", "前の発言です")), captions(("山田", "次の話に移ります"))],
        )
        page.gate = threading.Event()
        browser = FakeBrowser(page)
        service = self.make(browser)

        self.assertIsNone(service.start("meet.google.com/abc-defg-hij"))
        self.assertTrue(page.gate.wait(5.0), "記録中まで進むこと")
        self.assertTrue(service.is_active())
        self.assertEqual(service.start(URL), rules.ALREADY_ACTIVE)
        self.assertEqual(service.open_login(), rules.LOGIN_WHILE_ACTIVE)
        service.stop()
        self.wait(service)

        self.assertEqual(
            self.states(),
            [rules.STATE_LAUNCHING, rules.STATE_JOINING, rules.STATE_RECORDING, rules.STATE_ENDED],
        )
        self.assertEqual(browser.launch_arguments, (True, True, None))
        self.assertEqual(browser.opened, URL)
        self.assertEqual(browser.denied, ["https://meet.google.com"])
        self.assertIn(scripts.join(rules.BOT_DISPLAY_NAME), page.calls)
        self.assertIn(scripts.caption_language("ja"), page.calls)
        self.assertEqual(page.calls[-1], scripts.LEAVE, "退出ボタンを押してから閉じること")
        self.assertTrue(browser.closed and page.closed)

        lines = self.of("line_confirmed")
        self.assertEqual([(speaker, text) for speaker, _, text in lines], [("山田", "前の発言です"), ("山田", "次の話に移ります")])
        self.assertRegex(lines[0][1], r"^\d\d:\d\d:\d\d$")

        (path, message), = self.of("finished")
        self.assertEqual(message, "会議から退出しました。議事録を保存しました（2 行）。")
        saved = Path(path)
        self.assertEqual(saved.parent, self.directory)
        self.assertTrue(saved.name.endswith("-abc-defg-hij.md"))
        content = saved.read_text(encoding="utf-8")
        self.assertIn("- 記録元: Google Meet / 週次定例\n- 言語: Meet の字幕（日本語）\n", content)
        self.assertTrue(content.endswith(f"- `{lines[1][1]}` **山田**: 次の話に移ります\n"))
        recording_texts = [text for state, text in self.of("state_changed") if state == rules.STATE_RECORDING]
        self.assertEqual(recording_texts[0], "参加中 — 記録中 0 行 — 週次定例")
        self.assertEqual(self.of("state_changed")[-1], (rules.STATE_ENDED, "終了 — 会議から退出しました"))
        self.assertFalse(service.is_active())

    def test_meeting_ended_by_host(self):
        """会議が終わったら自分で記録を閉じる（退出ボタンは押さない）。"""
        page = FakePage([IN_MEETING, IN_MEETING, ENDED], [captions()])
        service = self.make(FakeBrowser(page))
        service.start(URL)
        self.wait(service)
        (path, message), = self.of("finished")
        self.assertTrue(message.startswith("会議が終了しました。議事録の記録は 0 行でした"))
        self.assertTrue(Path(path).exists(), "0 行でも参加した記録としてファイルが残ること")
        self.assertNotIn(scripts.LEAVE, page.calls)
        self.assertEqual(self.states()[-1], rules.STATE_ENDED)

    def test_disconnect_while_recording_keeps_transcript(self):
        """記録の途中で接続が切れても、そこまでの議事録を残して理由つきで閉じる。"""
        page = FakePage([IN_MEETING], [captions(("山田", "切れる前の発言"))])
        page.fail_captions_with = CDPDisconnected()
        page.caption_frames.append(captions(("山田", "切れる前の発言")))
        service = self.make(FakeBrowser(page))
        service.start(URL)
        self.wait(service)
        (path, message), = self.of("finished")
        self.assertEqual(message, "ブラウザとの接続が切れました。議事録を保存しました（1 行）。")
        self.assertIn("切れる前の発言", Path(path).read_text(encoding="utf-8"))

    def test_region_fallback_is_recorded_without_speaker(self):
        """話者ごとの塊が取れないときは、字幕欄の文字を話者なしで残す。"""
        frame = {"found": True, "captionsOn": True, "source": "region", "entries": [{"speaker": "", "text": "欄の文字"}]}
        page = FakePage([IN_MEETING], [frame])
        page.gate = threading.Event()
        service = self.make(FakeBrowser(page))
        service.start(URL)
        self.assertTrue(page.gate.wait(5.0))
        service.stop()
        self.wait(service)
        self.assertEqual([(s, t) for s, _, t in self.of("line_confirmed")], [("", "欄の文字")])

    def test_caption_language_follows_config(self):
        """字幕の言語は設定に従い、議事録のヘッダにも出る。"""
        page = FakePage([IN_MEETING, ENDED], [captions()])
        service = self.make(FakeBrowser(page), config={"meetbot_caption_language": "en"})
        service.start(URL)
        self.wait(service)
        self.assertIn(scripts.caption_language("en"), page.calls)
        (path, _), = self.of("finished")
        self.assertIn("- 言語: Meet の字幕（英語）\n", Path(path).read_text(encoding="utf-8"))


class JoinFailureTests(ServiceTestCase):
    """入れなかった場合。"""

    def test_guest_waits_for_approval_then_is_denied(self):
        """ゲストは承認待ちを知らせ、断られたら理由とログインの導線を知らせる。"""
        page = FakePage([LOBBY, WAITING, WAITING, DENIED])
        browser = FakeBrowser(page)
        service = self.make(browser)
        service.start(URL)
        self.wait(service)

        self.assertEqual(
            self.states(),
            [rules.STATE_LAUNCHING, rules.STATE_JOINING, rules.STATE_WAITING_APPROVAL, rules.STATE_FAILED],
        )
        self.assertEqual(self.of("state_changed")[-1], (rules.STATE_FAILED, "エラー — 主催者に承認されませんでした"))
        self.assertIn((rules.STATE_WAITING_APPROVAL, "主催者の承認待ち（5 分まで待ちます）"), self.of("state_changed"))
        notices = [text for text, in self.of("notice")]
        self.assertEqual(len(notices), 2)
        self.assertIn("「voicekey 議事録ボット」を承認してください", notices[0])
        self.assertTrue(notices[1].startswith("主催者に承認されませんでした。"))
        self.assertEqual(self.of("finished"), [])
        self.assertEqual(list(self.directory.glob("*.md")) if self.directory.exists() else [], [])
        self.assertTrue(browser.closed)
        self.assertEqual(service.state, rules.STATE_FAILED)

    def test_logged_in_account_is_denied(self):
        """ログイン済みで断られたときは、ゲスト向けでない文言になる。"""
        page = FakePage([DENIED])
        service = self.make(FakeBrowser(page, cookies=[{"name": "SID", "domain": ".google.com", "expires": -1}]))
        service.start(URL)
        self.wait(service)
        self.assertEqual(self.of("state_changed")[-1][0], rules.STATE_FAILED)
        self.assertEqual(
            self.of("notice")[-1], ("参加を断られました。ホストに承認してもらうか、会議の URL を確かめてください。",),
        )

    def test_join_times_out(self):
        """参加ボタンは押せたのに入れないまま時間が過ぎたら、その旨で失敗にする。"""
        service = self.make(FakeBrowser(FakePage([LOBBY])))
        service.start(URL)
        self.wait(service)
        self.assertEqual(
            self.of("state_changed")[-1],
            (rules.STATE_FAILED, "エラー — 参加ボタンは押せましたが、90 秒たっても会議に入れませんでした"),
        )

    def test_chrome_missing(self):
        """Chrome が入っていないときは、そう分かる失敗になる。"""
        browser = FakeBrowser(launch_error=ChromeNotFound())
        service = self.make(browser)
        service.start(URL)
        self.wait(service)
        self.assertEqual(self.states(), [rules.STATE_LAUNCHING, rules.STATE_FAILED])
        self.assertEqual(self.of("state_changed")[-1], (rules.STATE_FAILED, "エラー — Google Chrome が必要です"))
        self.assertEqual(self.of("notice"), [("会議に参加できませんでした: Google Chrome が必要です",)])

    def test_port_in_use_does_not_launch(self):
        """ポートを別のアプリが使っているときは起動しない（相手にも触らない）。"""
        browser = FakeBrowser(busy=True)
        service = self.make(browser)
        service.start(URL)
        self.wait(service)
        self.assertFalse(browser.launched)
        self.assertIn("ポート 9333 を別のアプリが使っています", self.of("state_changed")[-1][1])

    def test_chrome_stops_answering_while_joining(self):
        """時間切れが続いたら「Chrome が応答しません」で諦める。"""

        class Silent(FakePage):
            def evaluate(self, java_script):
                from src.meetbot.chrome import CDPTimeout
                raise CDPTimeout()

        service = self.make(FakeBrowser(Silent([])))
        service.start(URL)
        self.wait(service)
        self.assertEqual(self.of("state_changed")[-1], (rules.STATE_FAILED, "エラー — Chrome が応答しません"))

    def test_stop_before_entering_returns_to_idle(self):
        """入る前に止めたら、失敗でも終了でもなく待機へ戻る。"""
        page = FakePage([WAITING])
        browser = FakeBrowser(page)
        service = self.make(browser)
        service.start(URL)
        deadline = threading.Event()
        for _ in range(500):
            if service.state == rules.STATE_WAITING_APPROVAL:
                break
            deadline.wait(0.01)
        self.assertEqual(service.start(URL), rules.ALREADY_ACTIVE)
        service.stop()
        self.wait(service)
        self.assertEqual(self.states()[-1], rules.STATE_IDLE)
        self.assertEqual(self.of("finished"), [])
        self.assertTrue(browser.closed)

    def test_rejects_invalid_url(self):
        service = self.make(FakeBrowser())
        self.assertEqual(service.start("https://example.com/abc-defg-hij"), rules.INVALID_URL_MESSAGE)
        self.assertEqual(self.events, [])
        self.assertFalse(service.is_active())

    def test_shutdown_closes_browser(self):
        """アプリ終了時は、記録中でも Chrome を畳んで議事録を閉じる。"""
        page = FakePage([IN_MEETING], [captions(("山田", "最後の発言"))])
        page.gate = threading.Event()
        browser = FakeBrowser(page)
        service = self.make(browser)
        service.start(URL)
        self.assertTrue(page.gate.wait(5.0))
        service.shutdown()
        self.assertTrue(browser.closed)
        self.assertFalse(service.is_active())
        (path, _), = self.of("finished")
        self.assertIn("最後の発言", Path(path).read_text(encoding="utf-8"))


class LoginTests(ServiceTestCase):
    """ログイン画面。"""

    def test_login_window_until_closed(self):
        """見える Chrome でログイン画面を出し、窓が閉じられたら結果を知らせて畳む。"""
        browser = FakeBrowser(page_counts=[1, 1, 0])
        service = self.make(browser)
        self.assertIsNone(service.open_login())
        self.wait(service, "_login_thread")
        self.assertEqual(browser.launch_arguments, (False, False, "https://accounts.google.com/"))
        self.assertTrue(browser.closed)
        self.assertEqual(
            [text for text, in self.of("login_state_changed")],
            [rules.LOGIN_CHECKING, rules.LOGIN_WAITING, rules.LOGIN_NONE],
        )

    def test_login_detected(self):
        """ログインできたら「ログイン済み」になる。"""
        browser = FakeBrowser(page_counts=[1, 1, 1, 0])
        original = browser.cookies
        answers = [[], [{"name": "SID", "domain": ".google.com", "expires": -1}]]
        browser.cookies = lambda: answers.pop(0) if len(answers) > 1 else answers[0]
        service = self.make(browser)
        service.open_login()
        self.wait(service, "_login_thread")
        del original
        self.assertEqual(self.of("login_state_changed")[-1], (rules.LOGIN_DONE,))

    def test_login_without_chrome_is_reported(self):
        service = self.make(FakeBrowser(launch_error=ChromeNotFound()))
        service.open_login()
        self.wait(service, "_login_thread")
        self.assertEqual(self.of("notice"), [("Google Chrome が必要です",)])
        self.assertEqual(self.of("login_state_changed")[-1], (rules.LOGIN_UNKNOWN,))


class SummaryTests(ServiceTestCase):
    """要約の口。"""

    def _transcript(self):
        self.directory.mkdir(parents=True)
        path = self.directory / "2026-10-11_140507-abc-defg-hij.md"
        path.write_text(
            "# 議事録 2026-10-11 14:05\n\n- 記録元: Google Meet / 定例\n- 言語: Meet の字幕（日本語）\n"
            "- `14:05:10` **山田**: 予算は来週決めます\n",
            encoding="utf-8",
        )
        return path

    def test_summary_is_saved_next_to_transcript(self):
        path = self._transcript()
        service = self.make(FakeBrowser(), chat=lambda system, user: "## 要点\n- 予算は来週")
        self.assertIsNone(service.summarize(str(path)))
        self.wait(service, "_summary_thread")
        self.assertEqual(self.of("summary_progress")[0], (str(path), summarizer.PREPARING_MESSAGE))
        self.assertEqual(
            self.of("summary_finished"), [(str(path), "## 要点\n- 予算は来週", summarizer.SAVED_MESSAGE)],
        )
        self.assertEqual(transcript.read_summary(path), "## 要点\n- 予算は来週")
        self.assertTrue(transcript.list_transcripts(self.directory)[0].has_summary)

    def test_summary_failure_is_reported(self):
        path = self._transcript()

        def chat(system, user):
            raise summarizer.ChatError(summarizer.ChatError.MISSING_KEY)

        service = self.make(FakeBrowser(), chat=chat)
        service.summarize(str(path))
        self.wait(service, "_summary_thread")
        self.assertEqual(
            self.of("summary_failed"),
            [(str(path), "Groq の API キーがありません（設定の「API キー」で入れてください）")],
        )
        self.assertIsNone(transcript.read_summary(path))

    def test_missing_file_is_reported(self):
        service = self.make(FakeBrowser(), chat=lambda system, user: "x")
        service.summarize(str(self.directory / "none.md"))
        self.wait(service, "_summary_thread")
        self.assertEqual(self.of("summary_failed"), [(str(self.directory / "none.md"), SUMMARY_UNREADABLE)])


if __name__ == "__main__":
    unittest.main()
