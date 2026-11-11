"""dictation_translator（翻訳して入力）のテスト。

判定の純関数と、Groq へ送る中身・失敗の扱いを検証する。
通信は共有クライアント（text_formatter._get_client）を差し替えて遮断する（実際には送らない）。
"""

import unittest
from unittest.mock import MagicMock, patch

from src.core import dictation_translator as dt
from src.core import text_formatter


def _response(status_code=200, json_data=None, text=""):
    """httpx.Response 相当の偽物を作る。"""
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    if json_data is not None:
        resp.json.return_value = json_data
    else:
        resp.json.side_effect = ValueError("JSON ではない応答")
    return resp


def _ok(content, finish_reason="stop"):
    """訳文 1 件を返す成功応答の JSON。"""
    return {
        "choices": [{"message": {"content": content}, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


class TestSwitches(unittest.TestCase):
    """「常に翻訳」と「1 回だけ翻訳」の 2 つのスイッチ。"""

    def test_should_translate(self):
        self.assertFalse(dt.should_translate(False, False))
        self.assertTrue(dt.should_translate(True, False))
        self.assertTrue(dt.should_translate(False, True))

    def test_once_key_toggles_when_always_off(self):
        self.assertTrue(dt.toggled_translate(False, False))
        self.assertFalse(dt.toggled_translate(False, True))  # もう一度押すと取り消し

    def test_once_key_does_not_turn_off_always(self):
        """「常に翻訳して入力」が ON の間は、キーで状態を変えない。"""
        self.assertFalse(dt.toggled_translate(True, False))
        self.assertTrue(dt.toggled_translate(True, True))

    def test_badge_and_target(self):
        self.assertEqual(dt.badge("en"), "→EN")
        self.assertEqual(dt.badge("zh-Hans"), "→ZH")
        self.assertEqual(dt.normalize_target("xx"), "en")
        self.assertEqual(dt.normalize_target(None), "en")
        self.assertEqual(dt.language_name("ko"), "韓国語")


class TestKeyWarnings(unittest.TestCase):
    """1 回翻訳キーとほかのキーの重なり。"""

    def test_default_keys_do_not_collide(self):
        self.assertEqual(
            dt.translate_once_key_warnings(
                "<shift_r>", ["<f2>", "<f3>"], repaste_key="<ctrl>+<alt>+<v>"
            ),
            [],
        )

    def test_empty_key_has_no_warning(self):
        self.assertEqual(dt.translate_once_key_warnings("", ["<f2>"]), [])

    def test_same_as_record_key(self):
        warnings = dt.translate_once_key_warnings("<f3>", ["<f2>", "<f3>"])
        self.assertEqual(warnings, ["録音キー 2 と重なっています。この 1 回翻訳キーは効きません"])

    def test_generic_and_sided_modifier_are_same_key(self):
        """録音キーが汎用の shift なら、右 shift の 1 回翻訳キーは録音キーの一部に見える。"""
        warnings = dt.translate_once_key_warnings("<shift_r>", ["<shift>+<space>"])
        self.assertEqual(len(warnings), 1)
        self.assertIn("録音キー 1", warnings[0])

    def test_key_containing_record_key_is_fine(self):
        """録音キーを押したまま別のキーを足す組み合わせは効くので警告しない。"""
        self.assertEqual(dt.translate_once_key_warnings("<f2>+<t>", ["<f2>"]), [])

    def test_handsfree_overlap(self):
        warnings = dt.translate_once_key_warnings("<shift_r>", ["<f2>"], handsfree_key="<shift_r>")
        self.assertEqual(
            warnings,
            ["ハンズフリーキーと重なっています。ハンズフリーが優先され、この 1 回翻訳キーは効きません"],
        )

    def test_repaste_overlap(self):
        warnings = dt.translate_once_key_warnings(
            "<ctrl>+<alt>+<v>", ["<f2>"], repaste_key="<ctrl>+<alt>+<v>"
        )
        self.assertEqual(
            warnings,
            ["再貼り付けキーと重なっています。録音していないときに押すと再貼り付けが動きます"],
        )


class TestRequestBody(unittest.TestCase):
    """Groq へ送る中身。"""

    def test_body(self):
        body = dt.build_request_body("こんにちは", "en", "qwen/qwen3.8-27b")
        self.assertEqual(body["model"], "qwen/qwen3.8-27b")
        self.assertEqual(body["messages"][0]["role"], "system")
        self.assertIn("英語へ", body["messages"][0]["content"])
        self.assertEqual(body["messages"][1], {"role": "user", "content": "こんにちは"})
        self.assertGreater(body["max_completion_tokens"], 0)


class TestTranslate(unittest.TestCase):
    """翻訳の実行（通信は差し替え）。"""

    def _run(self, response=None, side_effect=None, key="K", on_usage=None):
        client = MagicMock()
        if side_effect is not None:
            client.post.side_effect = side_effect
        else:
            client.post.return_value = response
        with patch.object(dt.engines, "api_key", return_value=key), \
                patch.object(text_formatter, "_get_client", return_value=client):
            try:
                return dt.translate("こんにちは", "en", "m", on_usage=on_usage), client
            except dt.TranslationError as e:
                return e, client

    def test_success_returns_stripped_text_and_reports_usage(self):
        seen = []
        result, client = self._run(_response(json_data=_ok("  Hello \n")), on_usage=seen.append)
        self.assertEqual(result, "Hello")
        self.assertEqual(seen[0]["usage"]["prompt_tokens"], 10)
        self.assertEqual(client.post.call_args.kwargs["headers"]["Authorization"], "Bearer K")

    def test_no_key_fails_without_request(self):
        result, client = self._run(_response(json_data=_ok("Hello")), key="")
        self.assertIsInstance(result, dt.TranslationError)
        client.post.assert_not_called()

    def test_http_error_reason_has_no_body(self):
        """サーバーの応答の本文は理由（＝ログ・通知の元）に入れない。"""
        result, _ = self._run(_response(status_code=429, text='{"error":{"message":"SECRET-BODY"}}'))
        self.assertIsInstance(result, dt.TranslationError)
        self.assertEqual(result.reason, "HTTP 429")

    def test_network_error(self):
        result, _ = self._run(side_effect=ConnectionError("x"))
        self.assertIsInstance(result, dt.TranslationError)
        self.assertEqual(result.reason, "ConnectionError")

    def test_truncated_response_fails_but_is_billed(self):
        """途中で切れた訳文は使わない。トークン代はかかっているので usage は渡す。"""
        seen = []
        result, _ = self._run(
            _response(json_data=_ok("Hel", finish_reason="length")), on_usage=seen.append
        )
        self.assertIsInstance(result, dt.TranslationError)
        self.assertEqual(len(seen), 1)

    def test_empty_response_fails(self):
        result, _ = self._run(_response(json_data=_ok("   ")))
        self.assertIsInstance(result, dt.TranslationError)

    def test_broken_json_fails(self):
        result, _ = self._run(_response(json_data=None))
        self.assertIsInstance(result, dt.TranslationError)


if __name__ == "__main__":
    unittest.main()
