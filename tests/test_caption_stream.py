"""ライブ字幕の連続ストリーミング（Soniox / OpenAI ライブ）と翻訳器の通しのテスト。

実 API には接続しない（課金なし）。Soniox / OpenAI は 127.0.0.1 の模擬 WebSocket サーバー、
Groq / Gemini は httpx の模擬トランスポート相手に確かめる。時間は差し替えた時計で進め、
待ちは必ず「条件が満たされるまで」待つ（CI のスレッドのタイミングに頼らない）。
"""

import json
import threading
import time
import unittest

import httpx
import numpy as np

from src.caption import live_stream as ls
from src.caption import soniox_assembler as sa
from src.caption import translators as tr


def wait_until(predicate, timeout=5.0):
    """条件が満たされるまで待つ（満たされたら True）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class FakeClock:
    """手で進める時計（30 秒の無音を一瞬で再現する）。"""

    def __init__(self):
        self.offset = 0.0

    def __call__(self):
        return time.monotonic() + self.offset


class FakeSonioxServer:
    """模擬 Soniox。mode: ok（音声ごとに途中結果）/ fatal402 / drop（毎回すぐ切る）。"""

    def __init__(self, mode="ok"):
        from websockets.sync.server import serve

        self.mode = mode
        self.connections = 0
        self.configs = []
        self.audio_bytes = 0
        self.keepalives = 0
        self.got_finalize = False
        self.closed = 0
        self._server = serve(self._handler, "127.0.0.1", 0)
        self.port = self._server.socket.getsockname()[1]
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    @property
    def url(self):
        return f"ws://127.0.0.1:{self.port}"

    def close(self):
        self._server.shutdown()

    def _handler(self, ws):
        self.connections += 1
        try:
            self.configs.append(json.loads(ws.recv()))
            if self.mode == "fatal402":
                ws.send(json.dumps({"error_code": 402, "error_message": "balance"}))
                return
            if self.mode == "drop":
                return
            for message in ws:
                if isinstance(message, bytes):
                    if len(message) == 0:
                        ws.send(json.dumps({"tokens": [], "finished": True}))
                        return
                    self.audio_bytes += len(message)
                    ws.send(json.dumps({"tokens": [
                        {"text": "Hello world.", "is_final": True, "translation_status": "original"},
                        {"text": "こんにちは世界。", "is_final": True, "translation_status": "translation"},
                    ]}))
                else:
                    kind = json.loads(message).get("type")
                    if kind == "keepalive":
                        self.keepalives += 1
                    elif kind == "finalize":
                        self.got_finalize = True
                        ws.send(json.dumps({"tokens": [{"text": "<fin>", "is_final": True}]}))
        except Exception:
            pass
        finally:
            self.closed += 1


def voiced(n=1600):
    return (np.sin(np.arange(n) / 5.0) * 0.3).astype(np.float32)


class TestSonioxCaptionStream(unittest.TestCase):
    def _stream(self, server, translates=True):
        stream = ls.SonioxCaptionStream(translates, "en", endpoint=server.url)
        stream.clock = FakeClock()
        self.confirmed, self.links, self.gave_up, self.usage = [], [], [], []
        stream.on_confirmed = lambda s, t: self.confirmed.append((s, t))
        stream.on_link_state = self.links.append
        stream.on_give_up = self.gave_up.append
        stream.on_usage = self.usage.append
        self.addCleanup(lambda: stream.stop(timeout=3.0))
        return stream

    def test_translates_and_confirms_and_stops_cleanly(self):
        server = FakeSonioxServer("ok")
        self.addCleanup(server.close)
        stream = self._stream(server)
        self.assertTrue(stream.start(api_key="test-key"))
        stream.send(voiced())
        self.assertTrue(wait_until(lambda: self.confirmed))
        self.assertEqual(self.confirmed[0], ("Hello world.", "こんにちは世界。"))
        config = server.configs[0]
        self.assertEqual(config["api_key"], "test-key")
        self.assertEqual(config["translation"], {"type": "one_way", "target_language": "ja"})
        self.assertTrue(config["enable_endpoint_detection"])
        self.assertEqual(config["max_endpoint_delay_ms"], 500)
        stream.stop(timeout=3.0)
        self.assertTrue(server.got_finalize, "停止時に finalize を送っていない")
        self.assertFalse(stream.is_running)
        self.assertTrue(self.usage and self.usage[0] > 0, "使った音声の秒数を記録していない")
        self.assertEqual(self.gave_up, [])

    def test_idle_disconnect_after_30s_and_reconnect_on_voice_is_not_a_failure(self):
        server = FakeSonioxServer("ok")
        self.addCleanup(server.close)
        stream = self._stream(server, translates=False)
        self.assertTrue(stream.start(api_key="k"))
        stream.send(voiced())
        self.assertTrue(wait_until(lambda: server.audio_bytes > 0))
        # 30 秒の無音（時計だけ進める）→ WebSocket だけ切る（課金を止める）
        stream.clock.offset += 31
        self.assertTrue(wait_until(lambda: sa.LINK_IDLE in self.links))
        self.assertTrue(wait_until(lambda: server.closed >= 1))
        # 無音のフレームでは張り直さない
        stream.send(np.zeros(1600, dtype=np.float32))
        time.sleep(0.2)
        self.assertEqual(server.connections, 1)
        # 声が戻ったら張り直す（失敗に数えない＝諦めない）
        stream.send(voiced())
        self.assertTrue(wait_until(lambda: server.connections == 2))
        self.assertTrue(wait_until(lambda: self.links[-1] == sa.LINK_CONNECTED))
        # 何回切っても諦めの上限に近づかない
        for _ in range(7):
            count = server.connections
            stream.clock.offset += 31
            self.assertTrue(wait_until(lambda: self.links[-1] == sa.LINK_IDLE))
            stream.send(voiced())
            self.assertTrue(wait_until(lambda: server.connections == count + 1))
            # 次の無音を数え始める前に、張り直しが終わったのを待つ（古い IDLE を読まない）
            self.assertTrue(wait_until(lambda: self.links[-1] == sa.LINK_CONNECTED))
        self.assertEqual(self.gave_up, [])

    def test_fatal_402_gives_up_with_reason(self):
        server = FakeSonioxServer("fatal402")
        self.addCleanup(server.close)
        stream = self._stream(server)
        self.assertTrue(stream.start(api_key="k"))
        self.assertTrue(wait_until(lambda: self.gave_up))
        self.assertEqual(self.gave_up, ["Soniox の残高不足"])
        self.assertTrue(wait_until(lambda: not stream.is_running))
        self.assertEqual(server.connections, 1, "張り直しても直らないのに張り直した")

    def test_reconnect_has_upper_limit(self):
        server = FakeSonioxServer("drop")
        self.addCleanup(server.close)
        stream = self._stream(server)
        self.assertTrue(stream.start(api_key="k"))

        def advance():
            # 指数バックオフの待ちを時計で飛ばす
            stream.clock.offset += 10
            return bool(self.gave_up)

        self.assertTrue(wait_until(advance, timeout=10.0))
        self.assertIn("切れました", self.gave_up[0])
        self.assertEqual(server.connections, sa.ReconnectPolicy.MAX_ATTEMPTS + 1)

    def test_no_key_does_not_start(self):
        stream = ls.SonioxCaptionStream(True, "en", endpoint="ws://127.0.0.1:9")
        self.assertFalse(stream.start(api_key=""))


class FakeOpenAIServer:
    def __init__(self, error_code=None):
        from websockets.sync.server import serve

        self.error_code = error_code
        self.session = None
        self.appends = 0
        self._server = serve(self._handler, "127.0.0.1", 0)
        self.port = self._server.socket.getsockname()[1]
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def close(self):
        self._server.shutdown()

    def _handler(self, ws):
        try:
            self.session = json.loads(ws.recv())
            if self.error_code:
                ws.send(json.dumps({"type": "error", "error": {"type": "x", "code": self.error_code}}))
            for message in ws:
                kind = json.loads(message).get("type")
                if kind == "input_audio_buffer.append":
                    self.appends += 1
                    if self.appends == 1:
                        ws.send(json.dumps({"type": "conversation.item.input_audio_transcription.delta",
                                            "item_id": "i1", "delta": "こんに"}))
                        ws.send(json.dumps({"type": "conversation.item.input_audio_transcription.completed",
                                            "item_id": "i1", "transcript": "こんにちは"}))
        except Exception:
            pass


class TestOpenAICaptionStream(unittest.TestCase):
    def test_session_uses_server_vad_and_confirms(self):
        server = FakeOpenAIServer()
        self.addCleanup(server.close)
        stream = ls.OpenAICaptionStream("ja", endpoint=f"ws://127.0.0.1:{server.port}")
        lives, confirmed = [], []
        stream.on_live = lambda s, t: lives.append(s)
        stream.on_confirmed = lambda s, t: confirmed.append(s)
        self.addCleanup(lambda: stream.stop(timeout=3.0))
        self.assertTrue(stream.start(api_key="k"))
        stream.send(voiced())
        self.assertTrue(wait_until(lambda: confirmed))
        self.assertEqual(confirmed, ["こんにちは"])
        self.assertIn("こんに", lives)
        audio = server.session["session"]["audio"]["input"]
        self.assertEqual(audio["turn_detection"]["type"], "server_vad")
        self.assertEqual(audio["format"]["rate"], 24000)
        self.assertEqual(audio["transcription"]["language"], "ja")

    def test_insufficient_quota_is_fatal(self):
        server = FakeOpenAIServer(error_code="insufficient_quota")
        self.addCleanup(server.close)
        stream = ls.OpenAICaptionStream("en", endpoint=f"ws://127.0.0.1:{server.port}")
        gave_up = []
        stream.on_give_up = gave_up.append
        self.addCleanup(lambda: stream.stop(timeout=3.0))
        self.assertTrue(stream.start(api_key="k"))
        self.assertTrue(wait_until(lambda: gave_up))
        self.assertEqual(gave_up, ["OpenAI の残高不足"])


class TestTranslatorsOverMockHTTP(unittest.TestCase):
    def test_groq_stream_and_usage(self):
        sse = (
            'data: {"choices":[{"delta":{"content":"こんにちは"}}]}\n\n'
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
            'data: {"choices":[],"usage":{"prompt_tokens":100,"completion_tokens":20}}\n\n'
            "data: [DONE]\n\n"
        )
        seen = {}

        def handler(request):
            seen["auth"] = request.headers.get("authorization")
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, content=sse.encode("utf-8"))

        translator = tr.GroqCaptionTranslator(
            model_provider=lambda: "openai/gpt-oss-20b", key_provider=lambda: "gk",
            client_factory=lambda: httpx.Client(transport=httpx.MockTransport(handler)))
        deltas = []
        self.assertEqual(translator("Hello", [], deltas.append), "こんにちは")
        self.assertEqual(seen["auth"], "Bearer gk")
        self.assertEqual(seen["body"]["model"], "openai/gpt-oss-20b")
        self.assertEqual(deltas, ["こんにちは"])
        translator.close()

    def test_groq_model_unavailable_is_replaced_once(self):
        models = []

        def handler(request):
            body = json.loads(request.content)
            models.append(body["model"])
            if body["model"] == "retired-model":
                return httpx.Response(404, json={"error": {"code": "model_not_found", "message": "x"}})
            return httpx.Response(200, content=b'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\n')

        replaced = []
        translator = tr.GroqCaptionTranslator(
            model_provider=lambda: "retired-model", key_provider=lambda: "gk", on_model_replaced=replaced.append,
            client_factory=lambda: httpx.Client(transport=httpx.MockTransport(handler)))
        self.assertEqual(translator("Hi", []), "ok")
        self.assertEqual(models, ["retired-model", "openai/gpt-oss-20b"])
        self.assertEqual(replaced, ["openai/gpt-oss-20b"])
        translator.close()

    def test_groq_401_is_unauthorized(self):
        translator = tr.GroqCaptionTranslator(
            model_provider=lambda: "openai/gpt-oss-20b", key_provider=lambda: "bad",
            client_factory=lambda: httpx.Client(transport=httpx.MockTransport(
                lambda request: httpx.Response(401, json={"error": {"message": "invalid"}}))))
        with self.assertRaises(tr.TranslationError) as ctx:
            translator("Hi", [])
        self.assertEqual(ctx.exception.kind, "unauthorized")
        translator.close()

    def test_gemini_sends_key_in_header(self):
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            seen["key"] = request.headers.get("x-goog-api-key")
            return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "やあ"}]}}],
                                             "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 2}})

        translator = tr.GeminiCaptionTranslator(
            model_provider=lambda: "gemini-3.5-flash-lite", key_provider=lambda: "gem",
            client_factory=lambda: httpx.Client(transport=httpx.MockTransport(handler)))
        self.assertEqual(translator("Hi", []), "やあ")
        self.assertEqual(seen["key"], "gem")
        self.assertNotIn("gem", seen["url"].split("models/")[0])
        self.assertNotIn("key=", seen["url"])
        translator.close()


if __name__ == "__main__":
    unittest.main()
