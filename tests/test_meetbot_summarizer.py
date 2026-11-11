"""
議事録の要約のテスト（通信なし）

分割して部分ごとに要約 → まとめ直す流れ、429 の待ち方、Groq への問い合わせの組み立てと
失敗の分類を、HTTP を差し替えて確かめる。
"""

import unittest
from unittest.mock import MagicMock, patch

import httpx

from src.core import llm_catalog
from src.meetbot import summarizer
from src.meetbot.summarizer import ChatError


def _transcript(lines: int, width: int = 40) -> str:
    """発言が `lines` 行ある議事録を作る。"""
    header = "# 議事録 2026-10-11 14:05\n\n- 記録元: Google Meet / 定例\n- 言語: Meet の字幕（日本語）\n"
    body = "".join(f"- `14:05:{i % 60:02d}` **山田**: {'あ' * width}{i}\n" for i in range(lines))
    return header + body


class ChunkTests(unittest.TestCase):
    """分割。"""

    def test_splits_on_line_boundaries_without_losing_text(self):
        """行の切れ目で分け、1 文字も落とさない。"""
        text = "\n".join(f"line{i}-" + "x" * 30 for i in range(20))
        parts = summarizer.chunks(text, 100)
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(part) <= 100 for part in parts))
        self.assertEqual("\n".join(parts), text)

    def test_cuts_a_single_long_line(self):
        """1 行だけで上限を超えるときだけ、その行を文字数で切る。"""
        parts = summarizer.chunks("a" * 250 + "\nshort", 100)
        self.assertEqual([len(part) for part in parts], [100, 100, 56])
        self.assertEqual("".join(parts).replace("\n", ""), "a" * 250 + "short")

    def test_empty_text_has_no_chunks(self):
        self.assertEqual(summarizer.chunks("\n\n"), [])


class RetryWaitTests(unittest.TestCase):
    """429 のあとの待ち方。"""

    def test_respects_retry_after_and_backs_off(self):
        self.assertEqual(summarizer.retry_wait(12.0, 0), 12.0)
        self.assertEqual(summarizer.retry_wait(0.0, 0), 1.0)
        self.assertEqual([summarizer.retry_wait(None, n) for n in range(3)], [5.0, 10.0, 20.0])

    def test_gives_up_when_too_long_or_too_many(self):
        self.assertIsNone(summarizer.retry_wait(summarizer.MAX_RETRY_WAIT + 1, 0))
        self.assertIsNone(summarizer.retry_wait(1.0, summarizer.MAX_RETRIES))


class SummarizeTests(unittest.TestCase):
    """分割 → まとめ直しの流れ。"""

    def test_short_meeting_is_one_request(self):
        """短い会議は 1 回の問い合わせで仕上げる。"""
        calls = []

        def chat(system, user):
            calls.append((system, user))
            return "## 要点\n- A"

        messages = []
        result = summarizer.summarize(_transcript(3), chat, sleep=lambda s: None, progress=messages.append)
        self.assertEqual(result, "## 要点\n- A")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], summarizer.FINAL_PROMPT)
        self.assertTrue(calls[0][1].startswith("<<<\n") and calls[0][1].endswith("\n>>>"))
        self.assertEqual(messages, [summarizer.part_message(1, 1)])

    def test_long_meeting_is_summarized_in_parts_then_merged(self):
        """長い会議は部分ごとに要約してから、全体をまとめ直す。"""
        text = _transcript(200)
        parts = summarizer.chunks(text)
        self.assertGreater(len(parts), 1)
        calls = []

        def chat(system, user):
            calls.append(system)
            return "最終" if system == summarizer.FINAL_PROMPT else "部分の要約"

        messages = []
        result = summarizer.summarize(text, chat, sleep=lambda s: None, progress=messages.append)
        self.assertEqual(result, "最終")
        self.assertEqual(calls, [summarizer.PART_PROMPT] * len(parts) + [summarizer.FINAL_PROMPT])
        self.assertEqual(messages[0], summarizer.part_message(1, len(parts)))
        self.assertEqual(messages[-1], summarizer.MERGING_MESSAGE)

    def test_rate_limit_waits_and_retries(self):
        """429 は言われた秒数だけ待って、同じ問い合わせをやり直す。"""
        outcomes = [ChatError(ChatError.RATE_LIMITED, retry_after=7.0), ChatError(ChatError.RATE_LIMITED), "できた"]

        def chat(system, user):
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        waits, messages = [], []
        result = summarizer.summarize(_transcript(3), chat, sleep=waits.append, progress=messages.append)
        self.assertEqual(result, "できた")
        self.assertEqual(waits, [7.0, 10.0])
        self.assertIn(summarizer.waiting_message(7), messages)

    def test_rate_limit_gives_up_after_limit(self):
        """429 が続いたら、上限回数で諦める（いつまでも待たない）。"""
        calls = []

        def chat(system, user):
            calls.append(1)
            raise ChatError(ChatError.RATE_LIMITED, retry_after=1.0)

        with self.assertRaises(ChatError) as caught:
            summarizer.summarize(_transcript(3), chat, sleep=lambda s: None)
        self.assertEqual(caught.exception.kind, ChatError.RATE_LIMITED)
        self.assertEqual(len(calls), summarizer.MAX_RETRIES + 1)

    def test_other_errors_are_not_retried(self):
        calls = []

        def chat(system, user):
            calls.append(1)
            raise ChatError(ChatError.HTTP, status=500)

        with self.assertRaises(ChatError):
            summarizer.summarize(_transcript(3), chat, sleep=lambda s: None)
        self.assertEqual(len(calls), 1)

    def test_empty_transcript_is_rejected_without_request(self):
        """発言が 1 行も無い議事録は、問い合わせずに断る。"""
        chat = MagicMock()
        with self.assertRaises(summarizer.SummaryEmpty):
            summarizer.summarize(_transcript(0), chat, sleep=lambda s: None)
        chat.assert_not_called()


def _response(status_code=200, json_data=None, headers=None):
    """httpx.Response 相当の偽物を作る。"""
    response = MagicMock()
    response.status_code = status_code
    response.headers = headers or {}
    if json_data is None:
        response.json.side_effect = ValueError("no json")
    else:
        response.json.return_value = json_data
    return response


def _ok(content="## 要点\n- A", finish_reason="stop"):
    return _response(200, {
        "choices": [{"message": {"content": content}, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    })


class GroqChatTests(unittest.TestCase):
    """Groq への問い合わせ（HTTP を差し替える）。"""

    def setUp(self):
        self.client = MagicMock()
        for target, value in (
            ("src.meetbot.summarizer.text_formatter._get_client", MagicMock(return_value=self.client)),
            ("src.meetbot.summarizer.engines.api_key", MagicMock(return_value="test-key-value")),
        ):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_builds_request_and_records_usage(self):
        """既定は無料枠のおすすめモデルで、使用量を記録する。"""
        self.client.post.return_value = _ok()
        store = MagicMock()
        self.assertEqual(summarizer.groq_chat("sys", "user", usage_store=store), "## 要点\n- A")
        _, kwargs = self.client.post.call_args
        body = kwargs["json"]
        self.assertEqual(body["model"], llm_catalog.RECOMMENDED_MODEL)
        self.assertEqual(body["messages"], [
            {"role": "system", "content": "sys"}, {"role": "user", "content": "user"},
        ])
        self.assertEqual(body["max_completion_tokens"], summarizer.MAX_COMPLETION_TOKENS)
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer test-key-value")
        store.record_chat_usage.assert_called_once()
        self.assertEqual(store.record_chat_usage.call_args[0][1:], ("groq", llm_catalog.RECOMMENDED_MODEL, "meetingSummary"))

    def test_uses_given_model(self):
        self.client.post.return_value = _ok()
        summarizer.groq_chat("sys", "user", model="some-model")
        self.assertEqual(self.client.post.call_args[1]["json"]["model"], "some-model")

    def test_missing_key_does_not_send(self):
        with patch("src.meetbot.summarizer.engines.api_key", return_value=None):
            with self.assertRaises(ChatError) as caught:
                summarizer.groq_chat("sys", "user")
        self.assertEqual(caught.exception.kind, ChatError.MISSING_KEY)
        self.client.post.assert_not_called()

    def test_classifies_failures(self):
        """429・HTTP エラー・途中で切れた・読めない応答・通信失敗を分ける。"""
        cases = [
            (_response(429, {}, {"retry-after": "12"}), ChatError.RATE_LIMITED),
            (_response(401, {}), ChatError.HTTP),
            (_ok(finish_reason="length"), ChatError.TRUNCATED),
            (_ok(content=""), ChatError.BAD_RESPONSE),
            (_response(200, None), ChatError.BAD_RESPONSE),
        ]
        for response, kind in cases:
            self.client.post.return_value = response
            with self.assertRaises(ChatError) as caught:
                summarizer.groq_chat("sys", "user")
            self.assertEqual(caught.exception.kind, kind)
        self.assertEqual(caught.exception.kind, ChatError.BAD_RESPONSE)

        self.client.post.return_value = _response(429, {}, {"retry-after": "12"})
        with self.assertRaises(ChatError) as caught:
            summarizer.groq_chat("sys", "user")
        self.assertEqual(caught.exception.retry_after, 12.0)

        self.client.post.side_effect = httpx.ConnectError("boom")
        with self.assertRaises(ChatError) as caught:
            summarizer.groq_chat("sys", "user")
        self.assertEqual(caught.exception.kind, ChatError.NETWORK)

    def test_errors_never_carry_the_key(self):
        """失敗の文にも例外の中身にも、キーを出さない。"""
        self.client.post.return_value = _response(401, {})
        with self.assertRaises(ChatError) as caught:
            summarizer.groq_chat("sys", "user")
        self.assertNotIn("test-key-value", str(caught.exception))
        self.assertNotIn("test-key-value", summarizer.failure_message(caught.exception))
        self.assertEqual(summarizer.failure_message(caught.exception), "Groq の API キーが使えません")


if __name__ == "__main__":
    unittest.main()
