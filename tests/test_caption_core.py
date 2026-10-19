"""ライブ字幕（Windows）の純ロジックのテスト。

区切りポリシー（Mac の TranslationBreakPolicyTests と同じ観点）・翻訳の流量制御・Soniox の組み立て・
再接続／無音切断の方針・行の管理・議事録・音声の変換・最前面の追従・設定の読み方を、外部通信なしで確かめる。
"""

import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

from src.caption import line_model as lm
from src.caption import settings as cs
from src.caption import soniox_assembler as sa
from src.caption import translation_coordinator as tc
from src.caption import translators as tr
from src.caption.audio_capture import (
    ALL_EXCEPT_SELF, FrontmostTargetPolicy, RebuildLimiter, Resampler, display_name,
)
from src.caption.transcript_recorder import (
    TranscriptRecorder, file_base_name, format_line, latest_file,
)


def _normalize(text):
    return " ".join(text.split())


class TestSegmentBuffer(unittest.TestCase):
    """区切りポリシー（上限 40 字・節区切り 24 字・判定順は 長さ → 文末 → 節区切り）。"""

    LONG = ("Today I woke up really early and I made some coffee because I had a lot of work to do, "
            "so I sat down at my desk and started writing the report, "
            "but then my friend called me and we talked for a while.")

    def _chunks(self, finals):
        buf = tc.SegmentBuffer()
        out = []
        for text in finals:
            out += buf.submit(text)
        out += buf.flush()
        return out

    def test_long_sentence_is_split_into_clauses(self):
        chunks = self._chunks([self.LONG])
        self.assertGreaterEqual(len(chunks), 4, chunks)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), tc.MAX_BUFFERED_CHARACTERS, chunk)
        # 刻んでも 1 文字も落とさない
        self.assertEqual(_normalize(" ".join(chunks)), _normalize(self.LONG))

    def test_short_sentence_stays_single_line(self):
        self.assertEqual(self._chunks(["That was a good idea."]), ["That was a good idea."])

    def test_keeps_breaking_without_sentence_ending(self):
        chunks = self._chunks(["I went to the store and I bought some bread",
                               "and then I walked back home because it was raining"])
        self.assertGreaterEqual(len(chunks), 2, chunks)

    def test_length_is_checked_before_sentence_end(self):
        # 文末で終わっていても 40 字を超えたら刻む（文末判定を先に置くと刻みが効かない）
        text = "We should probably start the meeting now because everyone is here."
        buf = tc.SegmentBuffer()
        ready = buf.submit(text)
        self.assertTrue(ready, "長い 1 文が丸ごと待たされている")
        self.assertLessEqual(len(ready[0]), tc.MAX_BUFFERED_CHARACTERS)

    def test_short_fragment_waits(self):
        buf = tc.SegmentBuffer()
        self.assertEqual(buf.submit("So,"), [])
        self.assertTrue(buf.has_pending)
        self.assertEqual(buf.flush(), ["So,"])

    def test_values_match_mac(self):
        self.assertEqual(tc.MAX_BUFFERED_CHARACTERS, 40)
        self.assertEqual(tc.CLAUSE_BREAK_MINIMUM_CHARACTERS, 24)
        self.assertAlmostEqual(tc.IDLE_FLUSH_INTERVAL, 0.25)

    def test_word_break_when_no_clause_marker(self):
        text = "abcdefgh " * 8  # 節の目印なし・空白だけ
        chunks = self._chunks([text])
        self.assertTrue(all(len(c) <= tc.MAX_BUFFERED_CHARACTERS for c in chunks))
        self.assertEqual(_normalize(" ".join(chunks)), _normalize(text))


class TestTranslationCoordinator(unittest.TestCase):
    def test_translates_all_in_order_with_context(self):
        seen_contexts = []

        def translate(text, context, on_delta):
            seen_contexts.append(len(context))
            if on_delta:
                on_delta("訳")
            return "訳:" + text

        coordinator = tc.TranslationCoordinator(lambda: translate, idle_flush_interval=0.05)
        results = []
        done = threading.Event()
        coordinator.on_translated = lambda s, t, seq: (results.append((s, t)), len(results) >= 3 and done.set())
        for text in ("First one is done.", "Second one is here.", "Third"):
            coordinator.submit(text)
        self.assertTrue(done.wait(3.0), results)
        self.assertEqual([s for s, _ in results], ["First one is done.", "Second one is here.", "Third"])
        self.assertEqual(results[0][1], "訳:First one is done.")
        # 直近の確定ペアを文脈として渡す
        self.assertEqual(seen_contexts, [0, 1, 2])
        coordinator.close()

    def test_failure_does_not_stop_and_reports(self):
        calls = []

        def translate(text, context, on_delta):
            calls.append(text)
            if len(calls) == 1:
                raise tr.TranslationError("rate_limited", "Groq の利用上限（レート制限）に達しました")
            return "ok"

        coordinator = tc.TranslationCoordinator(lambda: translate, idle_flush_interval=0.05)
        failed, translated = [], []
        coordinator.on_failed = lambda s, m: failed.append((s, m))
        coordinator.on_translated = lambda s, t, q: translated.append(s)
        coordinator.submit("One sentence here.")
        coordinator.submit("Two sentence here.")
        self.assertTrue(coordinator.wait_idle(3.0))
        self.assertEqual(failed[0][0], "One sentence here.")
        self.assertIn("レート", failed[0][1])
        self.assertEqual(translated, ["Two sentence here."])
        coordinator.close()

    def test_idle_flush_sends_short_tail(self):
        coordinator = tc.TranslationCoordinator(lambda: (lambda t, c, d: t), idle_flush_interval=0.05)
        got = threading.Event()
        coordinator.on_source_ready = lambda s: got.set()
        coordinator.submit("Okay")
        self.assertTrue(got.wait(2.0), "言い切って黙ったのに出ない")
        coordinator.close()

    def test_streaming_delta_forwarded(self):
        def translate(text, context, on_delta):
            on_delta("途中")
            return "全部"

        coordinator = tc.TranslationCoordinator(lambda: translate, idle_flush_interval=0.05)
        deltas = []
        done = threading.Event()
        coordinator.on_streaming_delta = lambda s, so_far, seq: deltas.append(so_far)
        coordinator.on_translated = lambda s, t, q: done.set()
        coordinator.submit("A full sentence.")
        self.assertTrue(done.wait(2.0))
        self.assertEqual(deltas, ["途中"])
        coordinator.close()


class TestTranslators(unittest.TestCase):
    def test_groq_body_uses_cheap_model_rules(self):
        context = [tc.TranslationExchange("Hi.", "やあ。")]
        body = tr.groq_request_body("openai/gpt-oss-20b", "Hello there", context)
        self.assertEqual(body["messages"][0]["role"], "system")
        self.assertEqual(body["messages"][1], {"role": "user", "content": "Hi."})
        self.assertEqual(body["messages"][2], {"role": "assistant", "content": "やあ。"})
        self.assertEqual(body["messages"][-1], {"role": "user", "content": "Hello there"})
        # 出力上限は Mac と同じ式・gpt-oss の思考は抑える
        self.assertEqual(body["max_completion_tokens"], len("Hello there") * 3 + 512)
        self.assertEqual(body["reasoning_effort"], "low")
        self.assertFalse(body["include_reasoning"])
        self.assertTrue(body["stream"])
        self.assertEqual(body["stream_options"], {"include_usage": True})

    def test_parse_stream_complete_and_usage(self):
        lines = [
            'data: {"choices":[{"delta":{"content":"こん"}}]}',
            'data: {"choices":[{"delta":{"content":"にちは"},"finish_reason":"stop"}]}',
            'data: {"choices":[],"usage":{"prompt_tokens":10,"completion_tokens":3}}',
            "data: [DONE]",
        ]
        deltas, usages = [], []
        text = tr.parse_groq_stream(lines, deltas.append, usages.append)
        self.assertEqual(text, "こんにちは")
        self.assertEqual(deltas, ["こん", "こんにちは"])
        self.assertEqual(usages[0]["usage"]["completion_tokens"], 3)

    def test_parse_stream_truncated_is_error(self):
        lines = ['data: {"choices":[{"delta":{"content":"途中"},"finish_reason":"length"}]}']
        with self.assertRaises(tr.TranslationError):
            tr.parse_groq_stream(lines)
        with self.assertRaises(tr.TranslationError):
            tr.parse_groq_stream(['data: {"choices":[{"delta":{"content":"x"}}]}'])

    def test_error_mapping(self):
        self.assertEqual(tr.error_for_status("Groq", 401).kind, "unauthorized")
        self.assertIn("残高", tr.error_for_status("Groq", 402).user_message)
        self.assertEqual(tr.error_for_status("Groq", 429).kind, "rate_limited")
        self.assertEqual(tr.error_for_status("Groq", 503).kind, "server")

    def test_gemini_body_and_text(self):
        body = tr.gemini_request_body("Hi", [tc.TranslationExchange("a", "b")])
        self.assertEqual([c["role"] for c in body["contents"]], ["user", "model", "user"])
        self.assertEqual(tr.gemini_text({"candidates": [{"content": {"parts": [
            {"text": "考え", "thought": True}, {"text": "やあ"}]}}]}), "やあ")
        with self.assertRaises(tr.TranslationError):
            tr.gemini_text({"candidates": [{"finishReason": "MAX_TOKENS", "content": {"parts": [{"text": "x"}]}}]})

    def test_missing_key_raises(self):
        groq = tr.GroqCaptionTranslator(model_provider=lambda: "openai/gpt-oss-20b", key_provider=lambda: None)
        with self.assertRaises(tr.TranslationError) as ctx:
            groq("Hello", [])
        self.assertEqual(ctx.exception.kind, "missing_key")
        groq.close()

    def test_replacement_model(self):
        self.assertEqual(tr.replacement_model("openai/gpt-oss-20b"), "qwen/qwen3.8-27b")
        self.assertEqual(tr.replacement_model("old-model"), "openai/gpt-oss-20b")


class TestSonioxAssembler(unittest.TestCase):
    @staticmethod
    def _resp(tokens):
        return sa.Response(tokens=tokens)

    @staticmethod
    def _src(text, final):
        return sa.Token(text, final, False, final)

    @staticmethod
    def _tr(text, final):
        return sa.Token(text, final, True)

    def test_parse(self):
        response = sa.parse(json.dumps({"tokens": [
            {"text": "Hello", "is_final": True, "translation_status": "original"},
            {"text": "こんにちは", "is_final": False, "translation_status": "translation"},
            {"text": "<end>", "is_final": True}]}))
        self.assertEqual(response.tokens[0], sa.Token("Hello", True, False, True))
        self.assertEqual(response.tokens[1], sa.Token("こんにちは", False, True, False))
        error = sa.parse('{"error_code":402,"error_message":"no balance"}')
        self.assertEqual(error.error_code, 402)
        self.assertTrue(sa.parse('{"tokens":[],"finished":true}').finished)
        self.assertIsNone(sa.parse("not json"))

    def test_translate_mode_confirms_when_translation_completes(self):
        a = sa.SonioxCaptionAssembler(True)
        self.assertEqual(a.apply(self._resp([self._src("Good", False), self._src(" morning", False)])),
                         [(sa.LIVE, "Good morning", "")])
        events = a.apply(self._resp([self._src("Good morning.", True), self._tr("おはよう", False)]))
        self.assertEqual(events, [(sa.LIVE, "Good morning.", "おはよう")])
        events = a.apply(self._resp([self._tr("おはようございます。", True)]))
        self.assertEqual(events[0], (sa.CONFIRMED, "Good morning.", "おはようございます。"))
        self.assertEqual(events[-1], (sa.LIVE, "", ""))

    def test_carries_over_next_sentence(self):
        a = sa.SonioxCaptionAssembler(True)
        a.apply(self._resp([self._src("Hello there. How", True)]))
        events = a.apply(self._resp([self._tr("やあ。", True), sa.Token(" are", False)]))
        self.assertEqual(events[0], (sa.CONFIRMED, "Hello there.", "やあ。"))
        self.assertEqual(events[-1], (sa.LIVE, "How are", ""))
        self.assertEqual(a.flush(), (sa.CONFIRMED, "How are", ""))

    def test_end_token_and_waiting(self):
        a = sa.SonioxCaptionAssembler(True)
        a.apply(self._resp([self._src("So yeah", True), sa.Token("<end>", True)]))
        self.assertTrue(a.is_waiting_for_translation)
        events = a.apply(self._resp([self._tr("そうだね", True)]))
        self.assertEqual(events[0], (sa.CONFIRMED, "So yeah", "そうだね"))
        self.assertFalse(a.is_waiting_for_translation)

    def test_untranslated_speech(self):
        a = sa.SonioxCaptionAssembler(True)
        events = a.apply(self._resp([sa.Token("はい", True, False, False), sa.Token("<end>", True)]))
        self.assertEqual(events[0], (sa.CONFIRMED, "はい", ""))

    def test_transcribe_mode(self):
        a = sa.SonioxCaptionAssembler(False)
        self.assertEqual(a.apply(self._resp([sa.Token("今日は", True), sa.Token("晴れ", False)])),
                         [(sa.LIVE, "今日は晴れ", "")])
        events = a.apply(self._resp([sa.Token("晴れです。", True)]))
        self.assertEqual(events[0], (sa.CONFIRMED, "今日は晴れです。", ""))
        long = "あ" * sa.MAX_CONFIRMED_SOURCE_CHARACTERS
        self.assertEqual(a.apply(self._resp([sa.Token(long, True)]))[0], (sa.CONFIRMED, long, ""))

    def test_end_token_without_speech(self):
        a = sa.SonioxCaptionAssembler(False)
        self.assertEqual(a.apply(self._resp([sa.Token("<end>", True)])), [(sa.LIVE, "", "")])
        self.assertIsNone(a.flush())

    def test_flush_includes_pending(self):
        a = sa.SonioxCaptionAssembler(True)
        a.apply(self._resp([self._src("Last words", True), sa.Token(" before stop", False),
                            self._tr("止める前の", False)]))
        self.assertEqual(a.flush(), (sa.CONFIRMED, "Last words before stop", "止める前の"))
        self.assertIsNone(a.flush())

    def test_split_sentences(self):
        self.assertEqual(sa.split_sentences("It costs 3.5 dollars. Ok"), (["It costs 3.5 dollars."], " Ok"))
        self.assertEqual(sa.split_sentences("はい。いいえ"), (["はい。"], "いいえ"))


class TestPolicies(unittest.TestCase):
    def test_reconnect_backoff_and_limit(self):
        policy = sa.ReconnectPolicy()
        delays = [policy.next_delay(False) for _ in range(5)]
        self.assertEqual(delays, [0.5, 1.0, 2.0, 4.0, 8.0])
        self.assertIsNone(policy.next_delay(False))
        policy.note_healthy()
        self.assertEqual(policy.next_delay(False), 0.5)
        self.assertIsNone(sa.ReconnectPolicy().next_delay(True))

    def test_fatal_codes_and_reasons(self):
        for code in (400, 401, 402, 403):
            self.assertTrue(sa.ReconnectPolicy.is_fatal(code))
        self.assertFalse(sa.ReconnectPolicy.is_fatal(429))
        self.assertFalse(sa.ReconnectPolicy.is_fatal(None))
        self.assertEqual(sa.ReconnectPolicy.give_up_reason(402), "Soniox の残高不足")
        self.assertEqual(sa.ReconnectPolicy.give_up_reason(401), "Soniox のキーが無効")

    def test_idle_tracker_disconnects_after_30s_and_reconnects_on_voice(self):
        idle = sa.IdleTracker()
        idle.note_connected(0.0)
        self.assertFalse(idle.should_disconnect(29.0))
        self.assertTrue(idle.should_disconnect(30.0))
        self.assertFalse(idle.should_disconnect(31.0), "2 回切らない")
        self.assertFalse(idle.note_frame(False, 32.0), "無音では張り直さない")
        self.assertTrue(idle.note_frame(True, 33.0))
        self.assertFalse(idle.is_idle)

    def test_link_state_texts(self):
        self.assertEqual(sa.link_state_text(sa.LINK_CONNECTED), "Soniox 接続中")
        self.assertIn("無音", sa.link_state_text(sa.LINK_IDLE))


class TestLineModel(unittest.TestCase):
    def test_minimum_duration(self):
        self.assertEqual(lm.minimum_duration("短い"), 3.0)
        self.assertAlmostEqual(lm.minimum_duration("あ" * 40), 6.0)
        self.assertEqual(lm.minimum_duration("あ" * 200), 12.0)

    def test_live_policy(self):
        # 暫定訳が出ている間は生の原文で上書きしない
        self.assertFalse(lm.accepts_live_update(lm.LISTENING, "", "src", ("訳", "src"), None))
        # 翻訳待ちの原文はライブ行が空のときだけ
        self.assertFalse(lm.accepts_live_update(lm.PENDING_SOURCE, "", "old", ("", "new"), None))
        self.assertTrue(lm.accepts_live_update(lm.PENDING_SOURCE, "", "old", None, None))
        # 確定直後の同じ文は出さない
        self.assertFalse(lm.accepts_live_update(lm.PARTIAL, "訳", "s", None, ("訳", "s")))

    def test_roll_two_confirmed_plus_live(self):
        model = lm.CaptionLineModel()
        long1 = "一行目の訳です。これは長めの文で、まとめられない長さにしてあります"
        long2 = "二行目の訳です。これも長めの文で、まとめられない長さにしてあります"
        model.show_translation("a", long1, 0.0)
        model.show_translation("b", long2, 0.5)
        model.show_listening("next", 0.6)
        self.assertEqual(len(model.visible_lines), 3)
        # 3 行目が来ても、最古の行が最低表示時間を満たすまでは押し出さない（ライブ行は隠す）
        model.show_translation("c", "三行目の訳です。これもかなり長めの文で、まとめられない長さです", 1.0)
        self.assertEqual(len(model.confirmed), 3)
        self.assertEqual(len(model.visible_lines), 3)
        self.assertEqual(model.visible_lines[-1].kind, lm.KIND_CONFIRMED)
        # 読了したら追い出す
        model.tick(model.confirmed[0].min_duration + 0.01)
        self.assertLessEqual(len(model.confirmed), 2)

    def test_self_retire_and_auto_hide(self):
        model = lm.CaptionLineModel()
        model.show_translation("a", "短い", 0.0)
        self.assertFalse(model.tick(3.5))
        self.assertTrue(model.tick(4.1))  # 3 秒＋余韻 1 秒で退場
        self.assertTrue(model.is_empty)
        model.show_listening("hello", 10.0)
        self.assertTrue(model.tick(18.1))  # 8 秒何も来なければ畳む
        self.assertTrue(model.is_empty)

    def test_short_lines_merge_instead_of_pushing_out(self):
        model = lm.CaptionLineModel()
        model.show_translation("a", "はい", 0.0)
        model.show_translation("b", "そうですね", 0.2)
        model.show_translation("c", "なるほど", 0.4)
        self.assertEqual(len(model.confirmed), 2)
        self.assertIn("なるほど", model.confirmed[-1].japanese)
        self.assertIn("そうですね", model.confirmed[-1].japanese)

    def test_notice_cleared_by_caption(self):
        model = lm.CaptionLineModel()
        model.show_notice("お知らせ", 0.0)
        model.show_translation("a", "訳", 0.1)
        self.assertEqual([l.kind for l in model.confirmed], [lm.KIND_CONFIRMED])


class TestTranscriptRecorder(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_appends_markdown_immediately(self):
        rec = TranscriptRecorder(self.dir)
        now = datetime(2026, 10, 19, 14, 32, 5)
        rec.record("こんにちは", context="Chrome", language="日本語", now=now)
        rec.record("二行目", context="Chrome", language="日本語", now=now + timedelta(seconds=3))
        self.assertTrue(rec.wait_until_written())
        files = list(self.dir.glob("*.md"))
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].name, "2026-10-19_143205.md")
        body = files[0].read_text(encoding="utf-8")
        self.assertIn("# 議事録 2026-10-19 14:32", body)
        self.assertIn("- 記録元: Chrome", body)
        self.assertIn("- `14:32:05` こんにちは", body)
        self.assertIn("- `14:32:08` 二行目", body)

    def test_new_file_after_five_minute_gap(self):
        rec = TranscriptRecorder(self.dir)
        now = datetime(2026, 10, 19, 10, 0, 0)
        rec.record("一つ目", language="日本語", now=now)
        rec.record("二つ目", language="日本語", now=now + timedelta(minutes=5, seconds=1))
        rec.wait_until_written()
        self.assertEqual(len(list(self.dir.glob("*.md"))), 2)

    def test_meeting_is_one_file_and_speaker_and_duplicates(self):
        rec = TranscriptRecorder(self.dir)
        outcomes = []
        rec.on_write = lambda outcome, tag: outcomes.append((outcome[0], tag))
        now = datetime(2026, 10, 19, 9, 0, 0)
        rec.begin_meeting("abc-defg-hij", "Google Meet / 定例", "Meet の字幕（日本語）", now=now, tag=7)
        rec.record("はい", speaker="山田", now=now + timedelta(seconds=1))
        rec.record("はい", speaker="山田", now=now + timedelta(seconds=2))  # 重複は弾く
        rec.record("はい", speaker="佐藤", now=now + timedelta(seconds=3))  # 別の人は残す
        rec.record("再開します", speaker="山田", now=now + timedelta(minutes=20))  # 会議中は 5 分ルールで分けない
        summaries = []
        rec.end_session(summaries.append)
        rec.wait_until_written()
        files = list(self.dir.glob("*.md"))
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].name, "2026-10-19_090000-abc-defg-hij.md")
        body = files[0].read_text(encoding="utf-8")
        self.assertEqual(body.count("**山田**: はい"), 1)
        self.assertIn("**佐藤**: はい", body)
        self.assertEqual(summaries[0].lines, 3)
        self.assertTrue(all(tag == 7 for _, tag in outcomes))

    def test_same_second_gets_suffix(self):
        now = datetime(2026, 10, 19, 9, 0, 0)
        a, b = TranscriptRecorder(self.dir), TranscriptRecorder(self.dir)
        a.record("A", now=now)
        a.wait_until_written()
        b.record("B", now=now)
        b.wait_until_written()
        names = sorted(p.name for p in self.dir.glob("*.md"))
        self.assertEqual(names, ["2026-10-19_090000-2.md", "2026-10-19_090000.md"])
        self.assertIsNotNone(latest_file(self.dir))

    def test_failure_is_reported(self):
        blocker = self.dir / "blocked"
        blocker.write_text("x")
        rec = TranscriptRecorder(blocker / "sub")  # ファイルの下にフォルダは作れない
        outcomes = []
        rec.on_write = lambda outcome, tag: outcomes.append(outcome)
        rec.record("書けない", now=datetime(2026, 1, 1))
        rec.wait_until_written()
        self.assertEqual(outcomes[0][0], "failed")
        self.assertIsNotNone(rec.last_error)

    def test_helpers(self):
        when = datetime(2026, 10, 11, 14, 5, 12)
        self.assertEqual(file_base_name(when, "abc/def?ghi"), "2026-10-11_140512-abcdefghi")
        self.assertEqual(format_line("本文", None, when), "- `14:05:12` 本文\n")
        self.assertEqual(format_line("本文", "山田", when), "- `14:05:12` **山田**: 本文\n")


class TestAudioHelpers(unittest.TestCase):
    def test_resampler_48k_stereo_to_16k_mono(self):
        r = Resampler(48000, 2)
        chunk = (np.ones(480 * 2, dtype=np.int16) * 16384).tobytes()  # 10ms ステレオ
        out = np.concatenate([r.process(chunk) for _ in range(10)])
        self.assertEqual(out.size, 1600)
        self.assertAlmostEqual(float(out.mean()), 0.5, places=3)

    def test_resampler_44k_length(self):
        r = Resampler(44100, 1)
        total = sum(r.process(np.zeros(441, dtype=np.float32)).size for _ in range(100))
        self.assertAlmostEqual(total, 16000, delta=2)

    def test_resampler_passthrough(self):
        r = Resampler(16000, 1)
        self.assertEqual(r.process(np.zeros(160, dtype=np.float32)).size, 160)

    def test_frontmost_policy(self):
        policy = FrontmostTargetPolicy(own_pid=100)
        first = policy.observe(200, "chrome.exe", 0.0)
        self.assertEqual((first.pid, first.name), (200, "Google Chrome"))
        # 自分・デスクトップは対象を変えない
        self.assertIsNone(policy.observe(100, "voicekey.exe", 1.0))
        self.assertIsNone(policy.observe(300, "explorer.exe", 1.5))
        # 1 秒続くまで・前回から 2 秒あくまで張り替えない
        self.assertIsNone(policy.observe(400, "vlc.exe", 2.0))
        self.assertIsNone(policy.observe(400, "vlc.exe", 2.5))
        switched = policy.observe(400, "vlc.exe", 3.1)
        self.assertEqual(switched.pid, 400)
        # UWP の枠は「自分以外のすべて」
        policy2 = FrontmostTargetPolicy(own_pid=1)
        self.assertEqual(policy2.observe(5, "ApplicationFrameHost.exe", 0.0).pid, ALL_EXCEPT_SELF)
        self.assertEqual(display_name("Teams.exe"), "Microsoft Teams")

    def test_rebuild_limiter(self):
        limiter = RebuildLimiter()
        delays = [limiter.next_delay(float(i)) for i in range(5)]
        self.assertEqual(delays, [0.5, 1.0, 2.0, 4.0, 8.0])
        self.assertIsNone(limiter.next_delay(6.0))
        healthy = RebuildLimiter()
        healthy.next_delay(0.0)
        healthy.note_started(1.0)
        self.assertEqual(healthy.next_delay(40.0), 0.5, "30 秒安定したら数え直す")


class TestCaptionSettings(unittest.TestCase):
    def test_defaults_are_cheap_api(self):
        cfg = cs.CaptionConfig()
        self.assertEqual(cfg.mode, cs.MODE_TRANSLATE)
        self.assertEqual(cfg.engine, cs.ENGINE_GROQ)
        self.assertEqual(cfg.groq_model, "openai/gpt-oss-20b")
        self.assertEqual(cfg.recognizer, cs.RECOGNIZER_SONIOX)
        self.assertEqual(cfg.recognition_language, cs.LANG_EN)
        self.assertEqual(cfg.translator, cs.ENGINE_GROQ)
        self.assertEqual(cfg.scope, cs.SCOPE_FRONTMOST)
        self.assertTrue(cfg.saves_transcript)
        self.assertFalse(cfg.should_auto_start_now)

    def test_soniox_engine_forces_soniox_recognizer(self):
        cfg = cs.CaptionConfig({"engine": "soniox", "recognizer": "openai_live"})
        self.assertTrue(cfg.uses_soniox_translation)
        self.assertEqual(cfg.effective_recognizer, cs.RECOGNIZER_SONIOX)
        self.assertIsNone(cfg.translator)

    def test_transcribe_mode(self):
        cfg = cs.CaptionConfig({"mode": "transcribe", "language": "ja", "engine": "soniox",
                                "recognizer": "openai_live"})
        self.assertEqual(cfg.recognition_language, cs.LANG_JA)
        self.assertIsNone(cfg.translator)
        self.assertFalse(cfg.uses_soniox_translation)
        self.assertEqual(cfg.effective_recognizer, cs.RECOGNIZER_OPENAI)

    def test_invalid_values_fall_back(self):
        cfg = cs.CaptionConfig({"mode": "x", "engine": "apple", "hud_scale": 9, "scope": "?"})
        self.assertEqual(cfg.mode, cs.MODE_TRANSLATE)
        self.assertEqual(cfg.engine, cs.ENGINE_GROQ)  # Windows に Apple は無い
        self.assertEqual(cfg.hud_scale, cs.MAX_SCALE)
        self.assertEqual(cs.clamp_scale(0.1), cs.MIN_SCALE)
        self.assertEqual(cfg.scope, cs.SCOPE_FRONTMOST)

    def test_meet_bot_language_follows_caption(self):
        meet = cs.MeetBotConfig({}, cs.CaptionConfig({"language": "en"}))
        self.assertEqual(meet.language, "en")
        self.assertEqual(meet.source, cs.MEET_SOURCE_CAPTIONS)
        self.assertEqual(cs.MeetBotConfig({"language": "ja", "source": "apiSpeech"}).source, cs.MEET_SOURCE_API)


if __name__ == "__main__":
    unittest.main()
