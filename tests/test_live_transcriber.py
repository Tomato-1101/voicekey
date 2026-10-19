"""ライブ文字起こし（Soniox / OpenAI ライブ）とリージョン判定のテスト。

外部通信はしない。Soniox の通しの動作は 127.0.0.1 に立てた模擬 WebSocket サーバー相手に確かめる
（Mac の SonioxSessionLocalServerTests と同じ考え方）。
"""

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

import numpy as np

from src.core import live_transcriber as lt
from src.core import soniox_region


class TestSonioxTranscript(unittest.TestCase):
    def test_final_and_interim(self):
        t = lt.SonioxTranscript()
        r, _, _ = t.apply(json.dumps({"tokens": [
            {"text": "こんにちは", "is_final": True}, {"text": "世", "is_final": False}]}))
        self.assertEqual(r, t.UPDATED)
        self.assertEqual(t.full_text, "こんにちは世")
        # 非確定は応答ごとに置き換わる
        t.apply(json.dumps({"tokens": [{"text": "世界", "is_final": False}]}))
        self.assertEqual(t.full_text, "こんにちは世界")

    def test_fin_and_finished(self):
        t = lt.SonioxTranscript()
        r, _, _ = t.apply(json.dumps({"tokens": [
            {"text": "です", "is_final": True}, {"text": "<fin>", "is_final": True}]}))
        self.assertEqual(r, t.FINALIZED)
        self.assertEqual(t.final_text, "です")  # 制御トークンは結果に含めない
        r, _, _ = t.apply(json.dumps({"tokens": [], "finished": True}))
        self.assertEqual(r, t.FINISHED)

    def test_error_code_string_or_number_and_null_is_not_error(self):
        t = lt.SonioxTranscript()
        self.assertEqual(t.apply('{"error_code": 401, "error_type": "unauthorized"}'),
                         (t.ERROR, "401", "unauthorized"))
        self.assertEqual(t.apply('{"error_code": "402"}'), (t.ERROR, "402", None))
        r, _, _ = t.apply('{"error_code": null, "tokens": [{"text": "a", "is_final": true}]}')
        self.assertEqual(r, t.UPDATED)
        self.assertEqual(t.apply("not json")[0], t.IGNORED)


class TestEndingRules(unittest.TestCase):
    def test_messages(self):
        self.assertIn("無効", lt.soniox_failure_message(lt.failed("error", "401")))
        self.assertIn("残高", lt.soniox_failure_message(lt.failed("error", "402")))
        self.assertIn("上限", lt.soniox_failure_message(lt.failed("error", "429")))
        self.assertIn("接続", lt.soniox_failure_message(lt.failed("disconnect")))
        self.assertTrue(lt.is_invalid_key_failure(lt.failed("error", "403")))
        self.assertFalse(lt.is_invalid_key_failure(lt.failed("timeout")))

    def test_should_replay(self):
        self.assertTrue(lt.should_replay_after_live_failure("soniox", lt.failed("disconnect"), False))
        # タイムアウト・空・Soniox 以外・正常終了は再送しない
        self.assertFalse(lt.should_replay_after_live_failure("soniox", lt.failed("timeout"), False))
        self.assertFalse(lt.should_replay_after_live_failure("soniox", lt.failed("disconnect"), True))
        self.assertFalse(lt.should_replay_after_live_failure("openai_live", lt.failed("disconnect"), False))
        self.assertFalse(lt.should_replay_after_live_failure("soniox", lt.ENDING_COMPLETED, False))

    def test_log_label(self):
        self.assertEqual(lt.failed("error", "401", "x").log_label, "failed:error(code=401 type=x)")
        self.assertEqual(lt.ENDING_COMPLETED.log_label, "completed")


class TestSonioxConfig(unittest.TestCase):
    def test_config_json(self):
        cfg = json.loads(lt.SonioxLiveSession.config_json("K", "stt-rt-v5", "ja", "固有名詞"))
        self.assertEqual(cfg["api_key"], "K")
        self.assertEqual(cfg["audio_format"], "pcm_s16le")
        self.assertEqual(cfg["sample_rate"], 16000)
        self.assertEqual(cfg["language_hints"], ["ja"])
        self.assertEqual(cfg["context"], {"text": "固有名詞"})
        self.assertNotIn("language_hints_strict", cfg)
        # 言語が空（自動判定）ならヒントを送らない・プロンプト空なら context も無し
        cfg = json.loads(lt.SonioxLiveSession.config_json("K", "m", "", " "))
        self.assertNotIn("language_hints", cfg)
        self.assertNotIn("context", cfg)

    def test_replay_timeout(self):
        self.assertEqual(lt.SonioxLiveSession.replay_finish_timeout(0), 3.0)
        self.assertEqual(lt.SonioxLiveSession.replay_finish_timeout(10), 8.0)
        self.assertEqual(lt.SonioxLiveSession.replay_finish_timeout(100), 15.0)

    def test_pcm16_clips(self):
        data = lt.pcm16(np.array([2.0, -2.0, 0.0], dtype=np.float32))
        values = np.frombuffer(data, dtype="<i2")
        self.assertEqual(list(values), [32767, -32767, 0])


class TestUpsampler(unittest.TestCase):
    def test_length_ratio_and_continuity(self):
        up = lt.Upsampler16To24()
        ramp = np.linspace(0, 1, 1600, dtype=np.float32)
        out = np.concatenate([up.process(ramp[i:i + 160]) for i in range(0, 1600, 160)])
        # 16kHz → 24kHz で約 1.5 倍
        self.assertAlmostEqual(len(out) / 1600, 1.5, delta=0.01)
        # 連続した入力は境界で段差を作らない（単調増加を保つ）
        self.assertTrue(np.all(np.diff(out) >= -1e-6))

    def test_split_equals_whole(self):
        a = np.random.default_rng(0).uniform(-1, 1, 3200).astype(np.float32)
        whole = lt.Upsampler16To24().process(a)
        up = lt.Upsampler16To24()
        parts = np.concatenate([up.process(a[:1000]), up.process(a[1000:2333]), up.process(a[2333:])])
        self.assertEqual(len(whole), len(parts))
        self.assertTrue(np.allclose(whole, parts, atol=1e-5))


class TestOpenAILive(unittest.TestCase):
    def test_session_update(self):
        s = lt.OpenAILiveSession("gpt-live-transcribe", "")
        payload = json.loads(s.session_update_json())
        audio = payload["session"]["audio"]["input"]
        self.assertEqual(audio["format"]["rate"], 24000)
        self.assertEqual(audio["transcription"]["language"], "ja")  # 空なら既定 ja
        self.assertIsNone(audio["turn_detection"])

    def test_handle_delta_and_completed(self):
        s = lt.OpenAILiveSession("gpt-live-transcribe", "ja")
        seen = []
        s.on_interim = seen.append
        s._handle(json.dumps({"type": "conversation.item.input_audio_transcription.delta", "delta": "こん"}))
        s._handle(json.dumps({"type": "conversation.item.input_audio_transcription.delta", "delta": "にちは"}))
        self.assertEqual(s._text(), "こんにちは")
        s._handle(json.dumps({"type": "conversation.item.input_audio_transcription.completed",
                              "transcript": "こんにちは。"}))
        self.assertEqual(s._text(), "こんにちは。")
        self.assertTrue(s._done.is_set())
        # OpenAI ライブは終わり方を区別しない（Mac と同じ）
        self.assertEqual(s.ending, lt.ENDING_UNKNOWN)
        self.assertEqual(seen[-1], "こんにちは。")

    def test_audio_bytes_counted_from_base64(self):
        s = lt.OpenAILiveSession("m", "ja")
        frame = s._encode(np.zeros(1600, dtype=np.float32))
        self.assertEqual(s._frame_audio_bytes(frame), 2400 * 2)


class TestSonioxRegion(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "soniox_region.json"

    def test_decide(self):
        self.assertEqual(soniox_region.decide(200, 200), "jp")
        self.assertEqual(soniox_region.decide(401, 200), "us")
        self.assertIsNone(soniox_region.decide(401, None))

    def test_fingerprint_hides_key(self):
        fp = soniox_region.fingerprint("secret-key")
        self.assertEqual(len(fp), 16)
        self.assertNotIn("secret", fp)

    def test_detect_remembers_only_when_decided(self):
        self.assertIsNone(soniox_region.detect("K", self.path, probe=lambda r, k: None))
        self.assertFalse(self.path.exists())
        self.assertEqual(soniox_region.detect("K", self.path, probe=lambda r, k: 200 if r == "jp" else 401), "jp")
        self.assertEqual(soniox_region.cached("K", self.path), "jp")
        # 記憶にはキー本体を書かない
        self.assertNotIn("K\"", self.path.read_text(encoding="utf-8"))
        self.assertIsNone(soniox_region.cached("other", self.path))

    def test_websocket_url(self):
        url = soniox_region.websocket_url_for("K", self.path, detector=lambda k, p: None)
        self.assertEqual(url, soniox_region.WEBSOCKET_URLS["us"])  # 判定できなければ米国
        soniox_region.remember("K", "jp", self.path)
        self.assertEqual(soniox_region.websocket_url_for("K", self.path), soniox_region.WEBSOCKET_URLS["jp"])


class _FakeSonioxServer:
    """127.0.0.1 の模擬 Soniox サーバー（設定 → 音声 → finalize → 空フレームを受けて結果を返す）。"""

    def __init__(self, mode="ok"):
        from websockets.sync.server import serve

        self.mode = mode
        self.config = None
        self.audio_bytes = 0
        self.got_finalize = False
        self.got_empty = False
        self._server = serve(self._handler, "127.0.0.1", 0)
        self.port = self._server.socket.getsockname()[1]
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def close(self):
        self._server.shutdown()

    @property
    def url(self):
        return f"ws://127.0.0.1:{self.port}"

    def _handler(self, ws):
        try:
            self.config = json.loads(ws.recv())
            if self.mode == "error":
                ws.send(json.dumps({"error_code": 401, "error_type": "unauthorized", "error_message": "bad"}))
                return
            for message in ws:
                if isinstance(message, bytes):
                    if len(message) == 0:
                        self.got_empty = True
                        ws.send(json.dumps({"tokens": [], "finished": True}))
                        return
                    self.audio_bytes += len(message)
                    if self.mode == "drop" and self.audio_bytes > 3200:
                        ws.send(json.dumps({"tokens": [{"text": "前半", "is_final": True}]}))
                        return  # 途中で切る
                    ws.send(json.dumps({"tokens": [{"text": "途中", "is_final": False}]}))
                else:
                    if json.loads(message).get("type") == "finalize":
                        self.got_finalize = True
                        ws.send(json.dumps({"tokens": [
                            {"text": "こんにちは", "is_final": True},
                            {"text": "<fin>", "is_final": True}]}))
        except Exception:
            pass


class TestSonioxLocalServer(unittest.TestCase):
    def _run_session(self, server, chunks=5):
        session = lt.SonioxLiveSession("stt-rt-v5", "ja", "", endpoint=server.url)
        usage = []
        session.on_usage = usage.append
        self.assertTrue(session.start(api_key="test-key"))
        for _ in range(chunks):
            session.send(np.zeros(1600, dtype=np.float32))
        text = session.finish(timeout=3.0)
        return session, text, usage

    def test_normal_flow_completes_with_fin(self):
        server = _FakeSonioxServer("ok")
        self.addCleanup(server.close)
        session, text, usage = self._run_session(server)
        self.assertEqual(text, "こんにちは")
        self.assertEqual(session.ending, lt.ENDING_COMPLETED)
        # 設定が最初のフレームで届いている
        self.assertEqual(server.config["api_key"], "test-key")
        self.assertEqual(server.config["model"], "stt-rt-v5")
        self.assertTrue(server.got_finalize)
        # 使用量は実際に送った音声の秒数（0.5 秒 + 末尾の無音 0.2 秒）
        self.assertEqual(len(usage), 1)
        self.assertAlmostEqual(usage[0], 0.7, delta=0.01)

    def test_error_reports_code(self):
        server = _FakeSonioxServer("error")
        self.addCleanup(server.close)
        session, text, _ = self._run_session(server)
        self.assertEqual(text, "")
        self.assertTrue(session.ending.is_failed)
        self.assertEqual(session.ending.code, "401")

    def test_drop_returns_partial_as_failed(self):
        server = _FakeSonioxServer("drop")
        self.addCleanup(server.close)
        session, text, _ = self._run_session(server)
        self.assertEqual(text, "前半")
        self.assertEqual(session.ending.failure, "disconnect")

    def test_finish_without_connect_or_audio_is_immediate(self):
        session = lt.SonioxLiveSession("stt-rt-v5", "ja", "", endpoint="ws://127.0.0.1:9")
        start = time.monotonic()
        self.assertEqual(session.finish(timeout=3.0), "")
        self.assertLess(time.monotonic() - start, 1.0)
        self.assertEqual(session.ending.failure, "disconnect")


if __name__ == "__main__":
    unittest.main()
