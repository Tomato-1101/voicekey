"""リアルタイム文字起こし（Soniox / OpenAI ライブ）のロジックテスト。

実際の WebSocket・HTTP にはつながない。接続は偽物（FakeWS）に差し替え、サーバーの応答は
手書きの JSON 列を順に再生する。API キーも偽の値を返すように差し替える
（開発機のシェルが本物のキーを環境変数に持っていても読まないようにするため）。
"""

import base64
import json
import queue
import threading
import time
import unittest
from unittest.mock import patch

import numpy as np

from src.core import streaming_transcriber as stream_mod
from src.core.api_transcriber import TranscriptionError
from src.core.streaming_transcriber import (
    OpenAILiveStreamer,
    SonioxStreamer,
    SonioxTranscriber,
    _Resampler16to24,
    _float32_to_pcm16le,
    create_streamer,
    soniox_region,
)

# 偽の接続に「ここで切断する」と伝える目印
_DROP = object()


class FakeWS:
    """
    WebSocket の偽物。送られたフレームを記録し、用意した応答を順に返す。

    応答は 2 段に分けて用意する。接続直後に返すぶん（途中経過）と、終わりの合図
    （is_end が True を返すフレーム）が送られてから返すぶん（確定）。本物のサーバーも
    終わりの合図を受けてから最後の確定を返すので、同じ順序を再現する。
    """

    def __init__(self, first=(), after_end=(), is_end=lambda frame: False):
        """
        Args:
            first: 接続直後に返す応答（dict は JSON にする。_DROP は切断）
            after_end: 終わりの合図の後に返す応答
            is_end: 送られたフレームが終わりの合図かを返す関数
        """
        self.sent = []
        self.closed = False
        self._after_end = list(after_end)
        self._is_end = is_end
        self._inbox: "queue.Queue" = queue.Queue()
        for item in first:
            self._inbox.put(item)

    def send(self, data):
        """送られたフレームを記録し、終わりの合図なら残りの応答を流す。"""
        self.sent.append(data)
        if self._is_end(data):
            for item in self._after_end:
                self._inbox.put(item)

    def recv(self, timeout=None):
        """次の応答を返す。無ければ本物と同じく TimeoutError。"""
        try:
            item = self._inbox.get(timeout=0.02)
        except queue.Empty:
            raise TimeoutError
        if item is _DROP:
            raise ConnectionError("切断")
        return json.dumps(item) if isinstance(item, dict) else item

    def close(self):
        """閉じたことを記録する。"""
        self.closed = True


def _is_text(frame, needle: str) -> bool:
    """フレームがテキストで、指定の文字列を含むかを返す。"""
    return isinstance(frame, str) and needle in frame


def _wait_until(cond, timeout: float = 2.0) -> bool:
    """条件が成り立つまで少しずつ待つ（別スレッドの進行待ち）。"""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


class _StreamTestCase(unittest.TestCase):
    """キーと接続を偽物に差し替える共通の準備。"""

    def setUp(self):
        # リージョンの記憶はプロセス内に残るので、テストごとに空にする
        stream_mod._soniox_regions.clear()
        self.connections = []  # (url, headers) の記録
        self.ws = FakeWS()
        self._patch(stream_mod.engines, "api_key", lambda engine_id: "TEST-KEY")
        self._patch(stream_mod, "_connect_ws", self._connect)
        # 既定は「日本リージョンのキー」
        self._patch(
            stream_mod, "_probe_status", lambda url, key: 200 if ".jp." in url else 401
        )

    def _patch(self, target, name, value):
        """テストが終わったら元に戻す差し替え。"""
        patcher = patch.object(target, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _connect(self, url, headers=None):
        """接続の偽物。行き先を記録して用意した FakeWS を返す。"""
        self.connections.append((url, headers))
        return self.ws


class TestPcmConversion(unittest.TestCase):
    def test_scales_to_int16(self):
        pcm = _float32_to_pcm16le(np.array([0.0, 1.0, -1.0], dtype=np.float32))
        self.assertEqual(list(np.frombuffer(pcm, dtype="<i2")), [0, 32767, -32767])

    def test_out_of_range_is_clipped(self):
        # 範囲外をそのまま int16 にすると折り返して轟音になる
        pcm = _float32_to_pcm16le(np.array([2.0, -3.0], dtype=np.float32))
        self.assertEqual(list(np.frombuffer(pcm, dtype="<i2")), [32767, -32767])


class TestSonioxRegion(_StreamTestCase):
    def test_jp_key_resolves_to_jp(self):
        self.assertEqual(soniox_region("K"), "jp")

    def test_us_key_resolves_to_us(self):
        with patch.object(
            stream_mod, "_probe_status", lambda url, key: 401 if ".jp." in url else 200
        ):
            self.assertEqual(soniox_region("K"), "us")

    def test_both_ok_prefers_jp(self):
        with patch.object(stream_mod, "_probe_status", lambda url, key: 200):
            self.assertEqual(soniox_region("K"), "jp")

    def test_result_is_remembered_per_key(self):
        calls = []

        def probe(url, key):
            calls.append(url)
            return 200 if ".jp." in url else 401

        with patch.object(stream_mod, "_probe_status", probe):
            soniox_region("K")
            soniox_region("K")
            self.assertEqual(len(calls), 2)  # 2 リージョンへ 1 回ずつだけ
            soniox_region("OTHER")
            self.assertEqual(len(calls), 4)  # 別のキーは判定し直す

    def test_unknown_is_not_remembered(self):
        # 通信できないだけかもしれないので、判定できなかった結果は覚えない
        with patch.object(stream_mod, "_probe_status", lambda url, key: 0):
            self.assertIsNone(soniox_region("K"))
        self.assertEqual(soniox_region("K"), "jp")

    def test_open_connects_to_resolved_region(self):
        SonioxStreamer("stt-rt-v5")._open("K")
        self.assertEqual(self.connections, [(stream_mod._SONIOX_WS_URLS["jp"], None)])

    def test_open_falls_back_to_us(self):
        with patch.object(stream_mod, "_probe_status", lambda url, key: 0):
            SonioxStreamer("stt-rt-v5")._open("K")
        self.assertEqual(self.connections[0][0], stream_mod._SONIOX_WS_URLS["us"])


class TestStartAndQueue(_StreamTestCase):
    """エンジンによらない共通部分（キュー・開始・打ち切り）。"""

    def test_start_without_key_returns_false(self):
        with patch.object(stream_mod.engines, "api_key", lambda engine_id: None):
            self.assertFalse(SonioxStreamer("stt-rt-v5").start())

    def test_start_passes_key_to_run(self):
        s = SonioxStreamer("stt-rt-v5")
        got = []
        done = threading.Event()

        def fake_run(key):
            got.append(key)
            done.set()

        with patch.object(s, "_run", fake_run):
            self.assertTrue(s.start())
            self.assertTrue(done.wait(2.0))
        self.assertEqual(got, ["TEST-KEY"])

    def test_finish_before_connect_returns_empty(self):
        s = SonioxStreamer("stt-rt-v5")
        self.assertEqual(s.finish(timeout=0.05, connect_grace=0.05), "")
        self.assertTrue(s._cancelled)

    def test_send_only_enqueues(self):
        # 録音スレッドから呼ばれるので、通信せずキューへ積むだけ
        s = SonioxStreamer("stt-rt-v5")
        s._ws = self.ws
        s.send(np.zeros(160, dtype=np.float32))
        self.assertEqual(self.ws.sent, [])
        self.assertEqual(s._send_q.qsize(), 1)

    def test_empty_chunk_is_ignored(self):
        s = SonioxStreamer("stt-rt-v5")
        s.send(np.array([], dtype=np.float32))
        self.assertEqual(s._send_q.qsize(), 0)

    def test_queue_overflow_marks_failed(self):
        # あふれた音声を黙って捨てず、録音全体の送り直しへ回す
        s = SonioxStreamer("stt-rt-v5", queue_max=2)
        s._ws = self.ws
        s._finals = ["途中まで"]
        for _ in range(3):
            s.send(np.zeros(160, dtype=np.float32))
        self.assertTrue(s.failed)
        self.assertEqual(s.finish(timeout=0.05), "")
        self.assertEqual(s.partial_text, "途中まで")

    def test_send_after_failure_is_noop(self):
        s = SonioxStreamer("stt-rt-v5")
        s._mark_failed()
        s.send(np.zeros(160, dtype=np.float32))
        self.assertEqual(s._send_q.qsize(), 0)

    def test_sender_error_marks_failed(self):
        class BrokenWS(FakeWS):
            def send(self, data):
                raise ConnectionError("送れない")

        s = SonioxStreamer("stt-rt-v5")
        s._sender_loop(BrokenWS(), "K")
        self.assertTrue(s.failed)

    def test_nothing_is_sent_after_end_of_audio(self):
        s = SonioxStreamer("stt-rt-v5")
        s._send_q.put(np.zeros(160, dtype=np.float32))
        s._send_q.put(stream_mod._END_OF_AUDIO)
        s._send_q.put(np.zeros(160, dtype=np.float32))
        s._sender_loop(self.ws, "K")
        # 設定 + 音声 1 + 終わりの合図 3（無音・finalize・空）
        self.assertEqual(len(self.ws.sent), 5)
        self.assertTrue(s._end_sent)

    def test_cancel_closes_socket(self):
        s = SonioxStreamer("stt-rt-v5")
        s._ws = self.ws
        s.cancel()
        self.assertTrue(self.ws.closed)

    def test_create_streamer_picks_class(self):
        self.assertIsInstance(create_streamer("soniox", "stt-rt-v5"), SonioxStreamer)
        self.assertIsInstance(
            create_streamer("openai_live", "gpt-live-transcribe"), OpenAILiveStreamer
        )


class TestSonioxFrames(_StreamTestCase):
    def test_config_frame(self):
        s = SonioxStreamer("stt-rt-v5", "ja", prompt=" VoiceKey ")
        (frame,) = s._opening_frames("K")
        self.assertEqual(json.loads(frame), {
            "api_key": "K",
            "model": "stt-rt-v5",
            "audio_format": "pcm_s16le",
            "sample_rate": 16000,
            "num_channels": 1,
            "language_hints": ["ja"],
            "context": {"text": "VoiceKey"},
        })

    def test_config_frame_omits_empty_options(self):
        config = json.loads(SonioxStreamer("stt-rt-v5", "")._opening_frames("K")[0])
        self.assertNotIn("language_hints", config)
        self.assertNotIn("context", config)

    def test_audio_is_binary_pcm(self):
        (frame,) = SonioxStreamer("stt-rt-v5")._encode(np.array([0.0, 1.0], dtype=np.float32))
        self.assertEqual(list(np.frombuffer(frame, dtype="<i2")), [0, 32767])

    def test_closing_frames(self):
        silence, finalize, end = SonioxStreamer("stt-rt-v5")._closing_frames()
        self.assertEqual(silence, bytes(6400))  # 0.2 秒の無音
        self.assertEqual(json.loads(finalize), {"type": "finalize"})
        self.assertEqual(end, b"")


class TestSonioxHandle(_StreamTestCase):
    def test_finals_accumulate_and_interim_is_replaced(self):
        seen = []
        s = SonioxStreamer("stt-rt-v5", on_interim=seen.append)
        s._handle({"tokens": [{"text": "今日", "is_final": True}, {"text": "は", "is_final": False}]})
        s._handle({"tokens": [{"text": "はいい", "is_final": False}]})
        s._handle({"tokens": [{"text": "はいい天気", "is_final": True}]})
        self.assertEqual(seen, ["今日は", "今日はいい", "今日はいい天気"])
        self.assertEqual(s.partial_text, "今日はいい天気")

    def test_markers_are_not_part_of_text(self):
        s = SonioxStreamer("stt-rt-v5")
        s._handle({"tokens": [
            {"text": "はい", "is_final": True},
            {"text": "<end>", "is_final": True},
            {"text": "<fin>", "is_final": True},
        ]})
        self.assertEqual(s.partial_text, "はい")

    def test_fin_before_end_does_not_complete(self):
        # 終わりの合図を送る前の <fin> は、まだ録音が続いているので終わりにしない
        s = SonioxStreamer("stt-rt-v5")
        s._handle({"tokens": [{"text": "<fin>", "is_final": True}]})
        self.assertFalse(s._completed)

    def test_fin_after_end_completes(self):
        s = SonioxStreamer("stt-rt-v5")
        s._end_sent = True
        s._handle({"tokens": [{"text": "<fin>", "is_final": True}]})
        self.assertTrue(s._completed)

    def test_finished_completes(self):
        s = SonioxStreamer("stt-rt-v5")
        s._handle({"tokens": [], "finished": True})
        self.assertTrue(s._completed)

    def test_error_reasons(self):
        cases = {
            401: "API キーが無効",
            403: "API キーが無効",
            402: "残高・利用上限",
            429: "上限に達しました",
            503: "接続できませんでした",
        }
        for code, expected in cases.items():
            s = SonioxStreamer("stt-rt-v5")
            # 本文（error_message）は通知に出さない
            s._handle({"error_code": code, "error_message": "internal detail"})
            self.assertTrue(s.failed)
            self.assertIn(expected, s.failure_reason)
            self.assertNotIn("internal detail", s.failure_reason)


class TestSonioxSession(_StreamTestCase):
    """start → send → finish を、偽の接続で通しで動かす。"""

    def _session_ws(self, after_end):
        """finalize を受けたら after_end を返す偽の接続。"""
        return FakeWS(
            first=[{"tokens": [{"text": "こん", "is_final": False}]}],
            after_end=after_end,
            is_end=lambda f: _is_text(f, "finalize"),
        )

    def test_full_session_returns_final_text(self):
        self.ws = self._session_ws([
            {"tokens": [{"text": "こんにちは", "is_final": True}]},
            {"tokens": [{"text": "<fin>", "is_final": True}]},
        ])
        seen = []
        s = SonioxStreamer("stt-rt-v5", on_interim=seen.append)
        self.assertTrue(s.start())
        s.send(np.full(1600, 0.5, dtype=np.float32))
        self.assertTrue(_wait_until(lambda: "こん" in seen))
        self.assertEqual(s.finish(timeout=2.0), "こんにちは")
        self.assertFalse(s.failed)
        self.assertTrue(self.ws.closed)
        self.assertEqual(self.connections[0][0], stream_mod._SONIOX_WS_URLS["jp"])
        # 送った順: 設定 → 音声 → 無音 → finalize → 空
        sent = self.ws.sent
        self.assertEqual(json.loads(sent[0])["api_key"], "TEST-KEY")
        self.assertEqual(len(sent[1]), 3200)
        self.assertEqual(sent[2], bytes(6400))
        self.assertEqual(json.loads(sent[3]), {"type": "finalize"})
        self.assertEqual(sent[4], b"")
        self.assertEqual(len(sent), 5)

    def test_drop_before_final_returns_empty_but_keeps_partial(self):
        # 確定前に切れたら空文字（＝録音全体の送り直し）。届いていた分は partial_text に残る
        self.ws = FakeWS(
            first=[{"tokens": [{"text": "途中", "is_final": True}]}],
            after_end=[_DROP],
            is_end=lambda f: _is_text(f, "finalize"),
        )
        s = SonioxStreamer("stt-rt-v5")
        s.start()
        s.send(np.zeros(1600, dtype=np.float32))
        self.assertTrue(_wait_until(lambda: s.partial_text == "途中"))
        self.assertEqual(s.finish(timeout=2.0), "")
        self.assertTrue(s.failed)
        self.assertEqual(s.partial_text, "途中")

    def test_timeout_is_not_success_but_keeps_partial(self):
        # 待ち時間切れは最後まで確定していないので成功として返さない（後半が欠けた文を貼らない）。
        # 届いていたぶんは partial_text に残り、終わり方は timeout として区別できる
        self.ws = FakeWS(first=[{"tokens": [{"text": "届いた分", "is_final": True}]}])
        s = SonioxStreamer("stt-rt-v5")
        s.start()
        self.assertTrue(_wait_until(lambda: s.partial_text == "届いた分"))
        self.assertEqual(s.finish(timeout=0.1), "")
        self.assertFalse(s.failed)
        self.assertEqual(s.ending, "timeout")
        self.assertEqual(s.partial_text, "届いた分")

    def test_ending_completed_and_failed(self):
        self.ws = self._session_ws([{"tokens": [{"text": "<fin>", "is_final": True}]}])
        s = SonioxStreamer("stt-rt-v5")
        s.start()
        s.finish(timeout=2.0)
        self.assertEqual(s.ending, "completed")
        self.ws = FakeWS(first=[_DROP])
        s = SonioxStreamer("stt-rt-v5")
        s.start()
        s.finish(timeout=2.0)
        self.assertEqual(s.ending, "failed")

    def test_connect_failure_sets_reason(self):
        def refuse(url, headers=None):
            raise OSError("つながらない")

        with patch.object(stream_mod, "_connect_ws", refuse):
            s = SonioxStreamer("stt-rt-v5")
            s.start()
            self.assertEqual(s.finish(timeout=2.0), "")
        self.assertTrue(s.failed)
        self.assertIn("接続できませんでした", s.failure_reason)


class TestSonioxResend(_StreamTestCase):
    """ライブで文字が取れなかったときの送り直し（SonioxTranscriber）。"""

    def test_properties(self):
        t = SonioxTranscriber("stt-rt-v5")
        self.assertFalse(t.allows_split)  # 同時接続の上限に当たるので分けて送らない
        self.assertTrue(t.is_available())

    def test_empty_audio_returns_empty(self):
        self.assertEqual(SonioxTranscriber("stt-rt-v5").transcribe(np.array([], dtype=np.float32)), "")
        self.assertEqual(self.connections, [])

    def test_missing_key_raises(self):
        with patch.object(stream_mod.engines, "api_key", lambda engine_id: None):
            with self.assertRaises(TranscriptionError):
                SonioxTranscriber("stt-rt-v5").transcribe(np.zeros(1600, dtype=np.float32))

    def test_resends_whole_recording(self):
        self.ws = FakeWS(
            after_end=[{"tokens": [{"text": "送り直し", "is_final": True}], "finished": True}],
            is_end=lambda f: _is_text(f, "finalize"),
        )
        audio = np.full(4000, 0.25, dtype=np.float32)
        self.assertEqual(SonioxTranscriber("stt-rt-v5").transcribe(audio), "送り直し")
        # 0.1 秒ずつに分けて、録音全体を欠けずに送っている
        chunks = [f for f in self.ws.sent[1:-3]]
        self.assertEqual([len(c) for c in chunks], [3200, 3200, 1600])
        self.assertEqual(b"".join(chunks), _float32_to_pcm16le(audio))

    def test_error_raises_with_short_reason(self):
        self.ws = FakeWS(
            after_end=[{"error_code": 402, "error_message": "balance exhausted for org 123"}],
            is_end=lambda f: _is_text(f, "finalize"),
        )
        with self.assertRaises(TranscriptionError) as ctx:
            SonioxTranscriber("stt-rt-v5").transcribe(np.zeros(1600, dtype=np.float32))
        self.assertIn("残高・利用上限", str(ctx.exception))
        self.assertNotIn("balance", str(ctx.exception))

    def test_partial_text_is_used_when_dropped(self):
        self.ws = FakeWS(
            after_end=[{"tokens": [{"text": "取れた分", "is_final": True}]}, _DROP],
            is_end=lambda f: _is_text(f, "finalize"),
        )
        text = SonioxTranscriber("stt-rt-v5").transcribe(np.zeros(1600, dtype=np.float32))
        self.assertEqual(text, "取れた分")

    def test_replay_reports_completion(self):
        """送り直しが最後まで確定したかを呼び出し側へ返す（途中で切れた回は False）。"""
        self.ws = FakeWS(
            after_end=[{"tokens": [{"text": "全文", "is_final": True}], "finished": True}],
            is_end=lambda f: _is_text(f, "finalize"),
        )
        audio = np.zeros(1600, dtype=np.float32)
        self.assertEqual(SonioxTranscriber("stt-rt-v5").transcribe_replay(audio), ("全文", True))
        self.ws = FakeWS(
            after_end=[{"tokens": [{"text": "取れた分", "is_final": True}]}, _DROP],
            is_end=lambda f: _is_text(f, "finalize"),
        )
        self.assertEqual(SonioxTranscriber("stt-rt-v5").transcribe_replay(audio), ("取れた分", False))

    def test_prewarm_resolves_region_once(self):
        calls = []

        def probe(url, key):
            calls.append(url)
            return 200

        with patch.object(stream_mod, "_probe_status", probe):
            t = SonioxTranscriber("stt-rt-v5")
            t.prewarm()
            t.prewarm()
        self.assertEqual(len(calls), 2)


class TestResampler(unittest.TestCase):
    def test_length_is_about_one_and_half(self):
        out = _Resampler16to24().process(np.zeros(1600, dtype=np.float32))
        self.assertLessEqual(abs(len(out) - 2400), 2)

    def test_linear_ramp_stays_linear(self):
        # 直線を線形補間すれば直線のまま（2/3 サンプルずつ進む）
        out = _Resampler16to24().process(np.arange(10, dtype=np.float32))
        np.testing.assert_allclose(out, np.arange(len(out)) * (2.0 / 3.0), atol=1e-5)

    def test_chunked_equals_whole(self):
        # チャンクの境界で途切れない（分けて渡しても 1 本で渡したのと同じ波形）
        rng = np.random.default_rng(0)
        audio = rng.uniform(-1, 1, 1000).astype(np.float32)
        whole = _Resampler16to24().process(audio)
        r = _Resampler16to24()
        parts = [r.process(audio[i:j]) for i, j in ((0, 1), (1, 160), (160, 163), (163, 1000))]
        np.testing.assert_allclose(np.concatenate(parts), whole, atol=1e-6)


class TestOpenAILiveFrames(_StreamTestCase):
    def test_session_update(self):
        (frame,) = OpenAILiveStreamer("gpt-live-transcribe", "ja")._opening_frames("K")
        self.assertEqual(json.loads(frame), {
            "type": "session.update",
            "session": {
                "type": "transcription",
                "audio": {"input": {
                    "format": {"type": "audio/pcm", "rate": 24000},
                    "transcription": {
                        "model": "gpt-live-transcribe",
                        "language": "ja",
                        "delay": "minimal",
                    },
                    "turn_detection": None,
                }},
            },
        })

    def test_key_is_not_in_frames(self):
        # キーはヘッダーで渡す（本文には入れない）
        frame = OpenAILiveStreamer("gpt-live-transcribe")._opening_frames("SECRET")[0]
        self.assertNotIn("SECRET", frame)

    def test_audio_is_base64_24k(self):
        s = OpenAILiveStreamer("gpt-live-transcribe")
        (frame,) = s._encode(np.full(1600, 0.5, dtype=np.float32))
        msg = json.loads(frame)
        self.assertEqual(msg["type"], "input_audio_buffer.append")
        pcm = np.frombuffer(base64.b64decode(msg["audio"]), dtype="<i2")
        self.assertLessEqual(abs(len(pcm) - 2400), 2)
        self.assertTrue(np.all(np.abs(pcm - 16383) <= 1))

    def test_commit_on_end(self):
        (frame,) = OpenAILiveStreamer("gpt-live-transcribe")._closing_frames()
        self.assertEqual(json.loads(frame), {"type": "input_audio_buffer.commit"})

    def test_delta_accumulates_and_completed_replaces(self):
        seen = []
        s = OpenAILiveStreamer("gpt-live-transcribe", on_interim=seen.append)
        s._handle({"type": "conversation.item.input_audio_transcription.delta", "delta": "こん"})
        s._handle({"type": "conversation.item.input_audio_transcription.delta", "delta": "にちわ"})
        self.assertFalse(s._completed)
        s._handle({
            "type": "conversation.item.input_audio_transcription.completed",
            "transcript": "こんにちは。",
        })
        self.assertEqual(seen, ["こん", "こんにちわ", "こんにちは。"])
        self.assertTrue(s._completed)

    def test_unrelated_events_are_ignored(self):
        s = OpenAILiveStreamer("gpt-live-transcribe")
        s._handle({"type": "session.updated"})
        s._handle({"type": "input_audio_buffer.committed"})
        self.assertEqual(s.partial_text, "")
        self.assertFalse(s._completed)
        self.assertFalse(s.failed)

    def test_error_marks_failed(self):
        s = OpenAILiveStreamer("gpt-live-transcribe")
        s._handle({"type": "error", "error": {"code": "invalid_api_key", "message": "detail"}})
        self.assertTrue(s.failed)
        self.assertNotIn("detail", s.failure_reason)


class TestOpenAILiveSession(_StreamTestCase):
    def test_full_session(self):
        self.ws = FakeWS(
            first=[
                {"type": "session.updated"},
                {"type": "conversation.item.input_audio_transcription.delta", "delta": "テス"},
            ],
            after_end=[
                {"type": "input_audio_buffer.committed"},
                {
                    "type": "conversation.item.input_audio_transcription.completed",
                    "transcript": "テストです。",
                },
            ],
            is_end=lambda f: _is_text(f, "input_audio_buffer.commit"),
        )
        s = OpenAILiveStreamer("gpt-live-transcribe")
        self.assertTrue(s.start())
        s.send(np.zeros(1600, dtype=np.float32))
        self.assertEqual(s.finish(timeout=2.0), "テストです。")
        self.assertEqual(
            self.connections,
            [(stream_mod._OPENAI_LIVE_URL, {"Authorization": "Bearer TEST-KEY"})],
        )
        kinds = [json.loads(f)["type"] for f in self.ws.sent]
        self.assertEqual(
            kinds, ["session.update", "input_audio_buffer.append", "input_audio_buffer.commit"]
        )

    def test_error_reply_returns_empty(self):
        # 失敗したら空文字（呼び出し側が録音全体を REST へ送り直す）
        self.ws = FakeWS(
            after_end=[{"type": "error", "error": {"code": "server_error"}}],
            is_end=lambda f: _is_text(f, "input_audio_buffer.commit"),
        )
        s = OpenAILiveStreamer("gpt-live-transcribe")
        s.start()
        s.send(np.zeros(1600, dtype=np.float32))
        self.assertEqual(s.finish(timeout=2.0), "")
        self.assertTrue(s.failed)


if __name__ == "__main__":
    unittest.main()
