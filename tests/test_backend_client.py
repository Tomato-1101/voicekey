"""製品版バックエンドクライアント（backend_client）のテスト。

httpx.MockTransport でサーバーを模擬し、実通信・実 keyring に触れない。
サーバー契約（パス・ヘッダー・ボディ・レスポンス）が voicekey-site のルートと
一致していることを検証する。
"""

import unittest
from unittest import mock

import httpx

from src.core import backend_client


def _install_mock(handler):
    """MockTransport を共有クライアントに差し込む。"""
    backend_client._client = httpx.Client(transport=httpx.MockTransport(handler))


class _Base(unittest.TestCase):
    def setUp(self):
        # secrets の 3 関数を差し替え（実 keyring に触れない）
        self._patches = [
            mock.patch.object(backend_client.secrets, "get_auth_session",
                              return_value={"access_token": "tok-abc", "refresh_token": "r", "expires_at": 0}),
            mock.patch.object(backend_client.secrets, "get_device_id", return_value="dev-123"),
            mock.patch.object(backend_client.secrets, "get_server_base_url", return_value="http://test.local"),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        backend_client._client = None  # 共有クライアントを元に戻す


class TestEphemeral(_Base):
    def test_success_and_headers(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["path"] = request.url.path
            seen["method"] = request.method
            seen["auth"] = request.headers.get("authorization")
            seen["device"] = request.headers.get("x-device-id")
            seen["platform"] = request.headers.get("x-platform")
            return httpx.Response(200, json={"provider": "deepgram", "token": "dg-jwt", "expires_in": 60})

        _install_mock(handler)
        result = backend_client.fetch_ephemeral_token()
        self.assertEqual(result["token"], "dg-jwt")
        self.assertEqual(seen["path"], "/api/v1/auth/ephemeral")
        self.assertEqual(seen["method"], "POST")
        self.assertEqual(seen["auth"], "Bearer tok-abc")
        self.assertEqual(seen["device"], "dev-123")
        self.assertEqual(seen["platform"], "windows")

    def test_no_subscription_403(self):
        _install_mock(lambda r: httpx.Response(403, json={"error": "no sub"}))
        with self.assertRaises(backend_client.BackendError) as cm:
            backend_client.fetch_ephemeral_token()
        self.assertEqual(cm.exception.status, 403)
        self.assertIn("サブスクリプション", str(cm.exception))

    def test_device_limit_409(self):
        _install_mock(lambda r: httpx.Response(409, json={"error": "limit"}))
        with self.assertRaises(backend_client.BackendError) as cm:
            backend_client.fetch_ephemeral_token()
        self.assertEqual(cm.exception.status, 409)

    def test_rate_limited_429(self):
        _install_mock(lambda r: httpx.Response(429, json={"error": "too many"}))
        with self.assertRaises(backend_client.BackendError) as cm:
            backend_client.fetch_ephemeral_token()
        self.assertEqual(cm.exception.status, 429)


class TestUnauthenticated(_Base):
    def test_no_session_raises_before_request(self):
        """ローカルに認証セッションが無ければ通信せず 401 相当で弾く。"""
        called = {"n": 0}

        def handler(request):
            called["n"] += 1
            return httpx.Response(200, json={})

        _install_mock(handler)
        with mock.patch.object(backend_client.secrets, "get_auth_session", return_value=None):
            with self.assertRaises(backend_client.BackendError) as cm:
                backend_client.fetch_ephemeral_token()
        self.assertEqual(cm.exception.status, 401)
        self.assertEqual(called["n"], 0)  # 通信していない


class TestElevenLabs(_Base):
    def test_success_multipart(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["path"] = request.url.path
            seen["body_has_file"] = b"audio.wav" in request.content
            seen["body_has_lang"] = b"name=\"language\"" in request.content
            return httpx.Response(200, json={"text": "こんにちは"})

        _install_mock(handler)
        text = backend_client.transcribe_elevenlabs(b"RIFFxxxx", language="ja")
        self.assertEqual(text, "こんにちは")
        self.assertEqual(seen["path"], "/api/v1/transcribe/elevenlabs")
        self.assertTrue(seen["body_has_file"])
        self.assertTrue(seen["body_has_lang"])

    def test_no_language_omitted(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["body_has_lang"] = b"name=\"language\"" in request.content
            return httpx.Response(200, json={"text": "ok"})

        _install_mock(handler)
        backend_client.transcribe_elevenlabs(b"RIFF", language="")
        self.assertFalse(seen["body_has_lang"])


class TestFormat(_Base):
    def test_success_json(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["path"] = request.url.path
            seen["ctype"] = request.headers.get("content-type")
            seen["body"] = request.content
            return httpx.Response(200, json={"text": "整形済み。"})

        _install_mock(handler)
        out = backend_client.format_text("えーっと これ")
        self.assertEqual(out, "整形済み。")
        self.assertEqual(seen["path"], "/api/v1/format")
        self.assertIn("application/json", seen["ctype"])
        self.assertIn(b"text", seen["body"])

    def test_failure_raises(self):
        _install_mock(lambda r: httpx.Response(502, json={"error": "fail"}))
        with self.assertRaises(backend_client.BackendError) as cm:
            backend_client.format_text("x")
        self.assertEqual(cm.exception.status, 502)


if __name__ == "__main__":
    unittest.main()
