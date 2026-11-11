"""
ボット用 Chrome と DevTools 通信のテスト（Chrome も通信も使わない）

WebSocket・HTTP・プロセスの起動を偽物に差し替え、送る命令の形と、返事・イベント・
時間切れ・切断の扱いを確かめる。
"""

import json
import tempfile
import unittest
from pathlib import Path

from src.meetbot import chrome
from src.meetbot.chrome import Browser, CDPDisconnected, CDPError, CDPSession, CDPTimeout, ChromeNotFound


class FakeSocket:
    """
    偽の WebSocket。

    送られた命令を覚え、`replies`（命令 → 返す文字列の並び）に従って受信を作る。
    """

    def __init__(self, replies=None):
        self.sent = []
        self.inbox = []
        self.replies = replies or (lambda message: [json.dumps({"id": message["id"], "result": {}})])
        self.closed = False

    def send(self, data):
        if self.closed:
            raise ConnectionError("closed")
        message = json.loads(data)
        self.sent.append(message)
        self.inbox.extend(self.replies(message))

    def recv(self, timeout=None):
        if not self.inbox:
            raise TimeoutError()
        item = self.inbox.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self):
        self.closed = True


def session_with(replies=None):
    """偽の WebSocket につないだセッションを作る。"""
    socket = FakeSocket(replies)
    return CDPSession("ws://127.0.0.1:9333/devtools/page/1", connect=lambda url: socket), socket


class CDPSessionTests(unittest.TestCase):
    """命令の送受信。"""

    def test_evaluate_sends_runtime_evaluate_and_returns_value(self):
        session, socket = session_with(lambda m: [
            json.dumps({"id": m["id"], "result": {"result": {"type": "object", "value": {"clicked": True}}}}),
        ])
        self.assertEqual(session.evaluate("1+1"), {"clicked": True})
        self.assertEqual(socket.sent, [{
            "id": 1, "method": "Runtime.evaluate",
            "params": {"expression": "1+1", "returnByValue": True, "awaitPromise": True, "userGesture": True},
        }])

    def test_skips_events_and_stale_replies(self):
        """イベント・前の命令の遅れた返事・壊れた行は読み飛ばし、自分の返事だけ受け取る。"""
        session, _ = session_with(lambda m: [
            json.dumps({"method": "Runtime.consoleAPICalled", "params": {}}),
            json.dumps({"id": m["id"] - 1, "result": {"stale": True}}),
            "not json",
            json.dumps({"id": m["id"], "result": {"ok": True}}),
        ])
        self.assertEqual(session.call("Browser.getVersion"), {"ok": True})
        self.assertEqual(session.call("Browser.getVersion"), {"ok": True})

    def test_ids_increase(self):
        session, socket = session_with()
        session.call("A")
        session.call("B", {"x": 1})
        self.assertEqual([(m["id"], m["method"], m["params"]) for m in socket.sent], [(1, "A", {}), (2, "B", {"x": 1})])

    def test_timeout_when_no_reply(self):
        session, _ = session_with(lambda m: [])
        with self.assertRaises(CDPTimeout):
            session.call("A", timeout=0.01)

    def test_error_reply_raises(self):
        session, _ = session_with(lambda m: [json.dumps({"id": m["id"], "error": {"code": -1, "message": "bad"}})])
        with self.assertRaises(CDPError) as caught:
            session.call("A")
        self.assertNotIsInstance(caught.exception, (CDPTimeout, CDPDisconnected))
        self.assertIn("bad", str(caught.exception))

    def test_script_exception_raises(self):
        session, _ = session_with(lambda m: [
            json.dumps({"id": m["id"], "result": {"result": {"type": "object"}, "exceptionDetails": {"text": "x"}}}),
        ])
        with self.assertRaises(CDPError):
            session.evaluate("throw 1")

    def test_disconnect_while_waiting(self):
        session, _ = session_with(lambda m: [ConnectionError("gone")])
        with self.assertRaises(CDPDisconnected):
            session.call("A")

    def test_call_after_close_is_disconnected(self):
        session, socket = session_with()
        session.close()
        self.assertTrue(socket.closed)
        with self.assertRaises(CDPDisconnected):
            session.call("A")

    def test_connect_failure_is_cdp_error(self):
        def refuse(url):
            raise OSError("refused")

        with self.assertRaises(CDPError):
            CDPSession("ws://127.0.0.1:1/x", connect=refuse)


class FakeProcess:
    """偽の Chrome プロセス。"""

    def __init__(self):
        self.running = True
        self.terminated = False

    def poll(self):
        return None if self.running else 0

    def wait(self, timeout=None):
        if self.running:
            raise TimeoutError()
        return 0

    def terminate(self):
        self.terminated = True
        self.running = False

    def kill(self):
        self.running = False


class BrowserTests(unittest.TestCase):
    """起動引数・タブ・Cookie・終了。"""

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.profile = Path(self._temp.name) / "meetbot-profile"
        self.launched = []
        self.process = FakeProcess()
        self.http_calls = []
        self.http_replies = {
            "/json/version": {"webSocketDebuggerUrl": "ws://127.0.0.1:9333/devtools/browser/abc"},
        }
        self.sockets = []
        self.socket_replies = None

    def _popen(self, arguments, **kwargs):
        self.launched.append(arguments)
        return self.process

    def _http(self, port, path, method="GET"):
        self.http_calls.append((port, path, method))
        for prefix, reply in self.http_replies.items():
            if path.startswith(prefix):
                if isinstance(reply, Exception):
                    raise reply
                return reply
        raise CDPError("no route")

    def _connect(self, url):
        socket = FakeSocket(self.socket_replies)
        socket.url = url
        self.sockets.append(socket)
        return socket

    def browser(self, executable="/fake/chrome"):
        return Browser(
            profile=self.profile, executable=executable, popen=self._popen, http=self._http, connect=self._connect,
        )

    def test_launch_uses_dedicated_profile_and_headless(self):
        """会議用は専用プロファイル・ヘッドレス・消音で起動する。"""
        browser = self.browser()
        browser.launch(headless=True, mute=True)
        arguments = self.launched[0]
        self.assertEqual(arguments[0], "/fake/chrome")
        self.assertIn("--remote-debugging-port=9333", arguments)
        self.assertIn(f"--user-data-dir={self.profile}", arguments)
        self.assertIn("--headless=new", arguments)
        self.assertIn("--mute-audio", arguments)
        self.assertIn("--deny-permission-prompts", arguments)
        self.assertTrue(self.profile.is_dir())
        self.assertTrue(browser.is_running())

    def test_login_window_is_visible_and_opens_url(self):
        """ログイン用は画面に出し、最初の URL を開く。"""
        self.browser().launch(headless=False, mute=False, initial_url="https://accounts.google.com/")
        arguments = self.launched[0]
        self.assertNotIn("--headless=new", arguments)
        self.assertNotIn("--mute-audio", arguments)
        self.assertEqual(arguments[-1], "https://accounts.google.com/")

    def test_missing_chrome_raises(self):
        browser = Browser(profile=self.profile, popen=self._popen, http=self._http, connect=self._connect)
        original = chrome.find_chrome
        chrome.find_chrome = lambda: None
        self.addCleanup(setattr, chrome, "find_chrome", original)
        with self.assertRaises(ChromeNotFound):
            browser.launch(headless=True, mute=True)
        self.assertEqual(self.launched, [])

    def test_open_page_uses_put_and_connects_to_tab(self):
        self.http_replies["/json/new"] = {"webSocketDebuggerUrl": "ws://127.0.0.1:9333/devtools/page/7"}
        page = self.browser().open_page("https://meet.google.com/abc-defg-hij?authuser=0")
        self.assertEqual(
            self.http_calls[-1], (9333, "/json/new?https://meet.google.com/abc-defg-hij?authuser=0", "PUT"),
        )
        self.assertEqual(self.sockets[-1].url, "ws://127.0.0.1:9333/devtools/page/7")
        self.assertIsInstance(page, CDPSession)

    def test_cookies_and_permissions_go_to_browser_socket(self):
        self.socket_replies = lambda m: [json.dumps({
            "id": m["id"], "result": {"cookies": [{"name": "SID", "domain": ".google.com"}]},
        })]
        browser = self.browser()
        self.assertEqual(browser.cookies(), [{"name": "SID", "domain": ".google.com"}])
        self.assertEqual(self.sockets[0].url, "ws://127.0.0.1:9333/devtools/browser/abc")
        self.assertEqual(self.sockets[0].sent[0]["method"], "Storage.getCookies")
        self.assertTrue(self.sockets[0].closed)

        browser.deny_media("https://meet.google.com")
        sent = [socket.sent[0] for socket in self.sockets[1:]]
        self.assertEqual([m["method"] for m in sent], ["Browser.setPermission"] * 2)
        self.assertEqual([m["params"]["permission"]["name"] for m in sent], ["camera", "microphone"])
        self.assertTrue(all(
            m["params"]["setting"] == "denied" and m["params"]["origin"] == "https://meet.google.com" for m in sent
        ))

    def test_page_count_counts_only_pages(self):
        self.http_replies["/json"] = [{"type": "page"}, {"type": "service_worker"}, {"type": "page"}]
        # "/json/version" も "/json" で始まるので、長い方を先に当てる並びにしておく
        self.http_replies = {"/json/version": self.http_replies["/json/version"], "/json": self.http_replies["/json"]}
        self.assertEqual(self.browser().page_count(), 2)

    def test_is_answering(self):
        browser = self.browser()
        self.assertTrue(browser.is_answering())
        self.http_replies["/json/version"] = CDPError("down")
        self.assertFalse(browser.is_answering())

    def test_close_asks_browser_to_close_then_stops_process(self):
        """まず Browser.close を送り、終わらなければプロセスを止める。二度目は何もしない。"""
        browser = self.browser()
        browser.launch(headless=True, mute=True)
        browser.close()
        self.assertEqual(self.sockets[0].sent[0]["method"], "Browser.close")
        self.assertTrue(self.process.terminated)
        self.assertFalse(browser.is_running())
        browser.close()
        self.assertEqual(len(self.sockets), 1)

    def test_leftover_is_closed_only_when_it_is_our_profile(self):
        """ポートの先が自分のプロファイルの Chrome だと確かめられたときだけ終了させる。"""
        self.profile.mkdir(parents=True)
        browser = self.browser()
        # 手掛かりのファイルが無い → 触らない
        self.assertFalse(browser.close_leftover())
        # 別の Chrome（接続先が一致しない）→ 触らない
        (self.profile / "DevToolsActivePort").write_text("9333\n/devtools/browser/other\n", encoding="utf-8")
        self.assertFalse(browser.close_leftover())
        self.assertEqual(self.sockets, [])
        # 自分の取り残し → Browser.close
        (self.profile / "DevToolsActivePort").write_text("9333\n/devtools/browser/abc\n", encoding="utf-8")

        def replies(message):
            self.http_replies["/json/version"] = CDPError("down")  # 閉じたらポートが空く
            return [json.dumps({"id": message["id"], "result": {}})]

        self.socket_replies = replies
        self.assertTrue(browser.close_leftover())
        self.assertEqual(self.sockets[0].sent[0]["method"], "Browser.close")


class LocationTests(unittest.TestCase):
    """Chrome と専用プロファイルの場所。"""

    def test_profile_is_dedicated_voicekey_folder(self):
        parts = chrome.profile_directory().parts
        self.assertEqual(parts[-2:], ("voicekey", "meetbot-profile"))

    def test_launch_arguments_never_point_at_default_profile(self):
        arguments = chrome.launch_arguments(9333, Path("/p"), True, True, None)
        self.assertEqual(sum(1 for a in arguments if a.startswith("--user-data-dir=")), 1)


if __name__ == "__main__":
    unittest.main()
