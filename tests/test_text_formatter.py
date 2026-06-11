"""text_formatter（LLM テキスト整形）のロジックテスト。

プロンプト生成と「失敗時は必ず原文を返す」フォールバック動作を、
ネットワーク・実 API キーなしで検証する。
"""

import unittest
from unittest.mock import MagicMock, patch

import httpx

from src.core import text_formatter
from src.core.text_formatter import build_system_prompt, format_text

# 全モード共通フッター（仕様の文言。実装側の定数とは独立に持ち、改変を検出する）
_FOOTER = (
    "出力は整形後のテキストのみを返す。前置き・説明・引用符・コードブロックを付けない。"
    "入力と同じ言語で出力する。元の発言にない情報を追加しない。"
)

_ALL_MODES = ("clean", "bullets", "polite", "casual", "email", "custom")


def _response(status_code: int = 200, json_data=None, text: str = ""):
    """httpx.Response 相当のモックを作る（json() が dict またはパース失敗を返す）。"""
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    if json_data is not None:
        resp.json.return_value = json_data
    else:
        resp.json.side_effect = ValueError("JSON ではない応答")
    return resp


class TestBuildSystemPrompt(unittest.TestCase):
    """build_system_prompt のモード別プロンプト生成を検証する。"""

    def test_all_modes_end_with_footer(self):
        """全 6 モードでプロンプトが生成され、空行 + 共通フッターで終わる。"""
        for mode in _ALL_MODES:
            prompt = build_system_prompt(mode, "カスタム指示")
            self.assertTrue(prompt.endswith("\n\n" + _FOOTER), f"mode={mode}")

    def test_mode_bodies_match(self):
        """custom 以外のモード本文が定義どおりにフッターと連結される。"""
        for mode, body in text_formatter._MODE_PROMPTS.items():
            self.assertEqual(build_system_prompt(mode, ""), body + "\n\n" + _FOOTER)

    def test_custom_uses_custom_prompt(self):
        """custom モードはユーザーのプロンプト本文をそのまま使う。"""
        prompt = build_system_prompt("custom", "すべて英語に翻訳してください。")
        self.assertEqual(prompt, "すべて英語に翻訳してください。\n\n" + _FOOTER)

    def test_custom_blank_falls_back_to_clean(self):
        """custom でプロンプトが空白のみなら clean と同一になる。"""
        clean = build_system_prompt("clean", "")
        self.assertEqual(build_system_prompt("custom", ""), clean)
        self.assertEqual(build_system_prompt("custom", "   \n  "), clean)

    def test_unknown_mode_falls_back_to_clean(self):
        """未知のモード識別子は clean 扱いになる。"""
        self.assertEqual(build_system_prompt("unknown", ""), build_system_prompt("clean", ""))


class TestFormatText(unittest.TestCase):
    """format_text の API 呼び出しとフォールバック動作を検証する。"""

    def test_blank_text_skips_api(self):
        """空白のみの入力は API を呼ばずそのまま返る。"""
        with patch.object(text_formatter.httpx, "post") as mock_post, \
                patch.object(text_formatter.secrets, "get_api_key") as mock_key:
            self.assertEqual(format_text("   ", "clean", "", "m"), "   ")
            self.assertEqual(format_text("", "clean", "", "m"), "")
            mock_post.assert_not_called()
            mock_key.assert_not_called()

    def test_no_api_key_returns_original(self):
        """API キー未設定なら API を呼ばず原文を返す。"""
        with patch.object(text_formatter.secrets, "get_api_key", return_value=None), \
                patch.object(text_formatter.httpx, "post") as mock_post:
            self.assertEqual(format_text("えーと原文です", "clean", "", "m"), "えーと原文です")
            mock_post.assert_not_called()

    def test_httpx_exception_returns_original(self):
        """タイムアウト等の httpx 例外時は原文を返し、例外を外に出さない。"""
        for exc in (httpx.TimeoutException("timeout"), httpx.ConnectError("connect")):
            with patch.object(text_formatter.secrets, "get_api_key", return_value="K"), \
                    patch.object(text_formatter.httpx, "post", side_effect=exc):
                self.assertEqual(format_text("原文", "clean", "", "m"), "原文")

    def test_non_200_returns_original(self):
        """HTTP 非 200 応答時は原文を返す。"""
        with patch.object(text_formatter.secrets, "get_api_key", return_value="K"), \
                patch.object(
                    text_formatter.httpx, "post",
                    return_value=_response(500, text="server error"),
                ):
            self.assertEqual(format_text("原文", "clean", "", "m"), "原文")

    def test_malformed_json_returns_original(self):
        """JSON 構造不正（choices 欠落・パース失敗）時は原文を返す。"""
        for resp in (_response(200, json_data={"unexpected": True}), _response(200)):
            with patch.object(text_formatter.secrets, "get_api_key", return_value="K"), \
                    patch.object(text_formatter.httpx, "post", return_value=resp):
                self.assertEqual(format_text("原文", "clean", "", "m"), "原文")

    def test_empty_content_returns_original(self):
        """応答の content が空白のみなら原文を返す。"""
        payload = {"choices": [{"message": {"content": "   "}}]}
        with patch.object(text_formatter.secrets, "get_api_key", return_value="K"), \
                patch.object(text_formatter.httpx, "post", return_value=_response(200, payload)):
            self.assertEqual(format_text("原文", "clean", "", "m"), "原文")

    def test_success_returns_trimmed_content(self):
        """200 正常応答なら choices[0].message.content の trim 結果を返す。"""
        payload = {"choices": [{"message": {"content": "  整形済みテキスト  "}}]}
        with patch.object(text_formatter.secrets, "get_api_key", return_value="K"), \
                patch.object(
                    text_formatter.httpx, "post",
                    return_value=_response(200, payload),
                ) as mock_post:
            result = format_text("えーと、原文です", "clean", "", "llama-3.1-8b-instant")
            self.assertEqual(result, "整形済みテキスト")

            # リクエストボディの構造（モデル・温度・メッセージ）も仕様どおりか確認する
            kwargs = mock_post.call_args.kwargs
            body = kwargs["json"]
            self.assertEqual(body["model"], "llama-3.1-8b-instant")
            self.assertEqual(body["temperature"], 0.2)
            self.assertEqual(
                body["messages"][0],
                {"role": "system", "content": build_system_prompt("clean", "")},
            )
            self.assertEqual(
                body["messages"][1],
                {"role": "user", "content": "えーと、原文です"},
            )
            self.assertEqual(kwargs["headers"]["Authorization"], "Bearer K")
            self.assertEqual(kwargs["timeout"], 10.0)


if __name__ == "__main__":
    unittest.main()
