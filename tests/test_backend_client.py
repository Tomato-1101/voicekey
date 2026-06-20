"""製品版バックエンドクライアント（backend_client）のテスト。

httpx.MockTransport でサーバーを模擬し、実通信・実 keyring に触れない。
サーバー契約（パス・ヘッダー・ボディ・レスポンス）が voicekey-site のルートと
一致していることを検証する。
"""

import time
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
        # expires_at は十分先にしておき、ensure_valid_session を no-op にする
        # （失効間際の先回りリフレッシュは TestRefreshOn401 で別途検証する）
        self._patches = [
            mock.patch.object(backend_client.secrets, "get_auth_session",
                              return_value={"access_token": "tok-abc", "refresh_token": "r",
                                            "expires_at": time.time() + 3600}),
            mock.patch.object(backend_client.secrets, "get_device_id", return_value="dev-123"),
            mock.patch.object(backend_client.secrets, "get_server_base_url", return_value="http://test.local"),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        backend_client._client = None  # 共有クライアントを元に戻す


class TestIsLoggedIn(unittest.TestCase):
    """is_logged_in（並存ガードの分岐条件）を検証する。実 keyring に触れない。"""

    def test_true_with_access_token(self):
        with mock.patch.object(backend_client.secrets, "get_auth_session",
                               return_value={"access_token": "t"}):
            self.assertTrue(backend_client.is_logged_in())

    def test_false_when_no_session(self):
        with mock.patch.object(backend_client.secrets, "get_auth_session", return_value=None):
            self.assertFalse(backend_client.is_logged_in())

    def test_false_when_token_missing(self):
        with mock.patch.object(backend_client.secrets, "get_auth_session",
                               return_value={"refresh_token": "r"}):
            self.assertFalse(backend_client.is_logged_in())


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


class TestSubmitFeedback(_Base):
    """フィードバック送信（認証は任意）を検証する。"""

    def test_logged_in_attaches_bearer(self):
        """ログイン済みなら Bearer・device_id・platform を付け、本文＋版を送る。"""
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["path"] = request.url.path
            seen["method"] = request.method
            seen["auth"] = request.headers.get("authorization")
            seen["device"] = request.headers.get("x-device-id")
            seen["platform"] = request.headers.get("x-platform")
            seen["body"] = request.content
            return httpx.Response(200, json={"ok": True})

        _install_mock(handler)
        backend_client.submit_feedback("不具合の報告です")
        self.assertEqual(seen["path"], "/api/v1/feedback")
        self.assertEqual(seen["method"], "POST")
        self.assertEqual(seen["auth"], "Bearer tok-abc")
        self.assertEqual(seen["device"], "dev-123")
        self.assertEqual(seen["platform"], "windows")
        self.assertIn("不具合の報告です".encode(), seen["body"])
        self.assertIn(b"app_version", seen["body"])

    def test_not_logged_in_sends_anonymously(self):
        """未ログインでも Authorization 無しで送信できる（匿名）。"""
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["auth"] = request.headers.get("authorization")
            seen["device"] = request.headers.get("x-device-id")
            return httpx.Response(200, json={"ok": True})

        _install_mock(handler)
        with mock.patch.object(backend_client.secrets, "get_auth_session", return_value=None):
            backend_client.submit_feedback("匿名の要望")
        self.assertIsNone(seen["auth"])         # Bearer は付かない
        self.assertEqual(seen["device"], "dev-123")  # device_id は付く

    def test_failure_raises(self):
        _install_mock(lambda r: httpx.Response(429, json={"error": "too many"}))
        with self.assertRaises(backend_client.BackendError) as cm:
            backend_client.submit_feedback("x")
        self.assertEqual(cm.exception.status, 429)


class TestRefreshOn401(_Base):
    """401 を受けたら一度だけリフレッシュして再試行する（Increment 4 の配線）。"""

    def test_retries_with_new_token_after_refresh(self):
        """初回 401 → refresh 成功 → 更新後トークンで再試行して 200。"""
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.headers.get("authorization"))
            if len(calls) == 1:
                return httpx.Response(401, json={"error": "expired"})
            return httpx.Response(200, json={"provider": "deepgram", "token": "dg-jwt", "expires_in": 60})

        _install_mock(handler)
        with mock.patch("src.core.auth_client.refresh",
                        return_value={"access_token": "tok-new", "refresh_token": "r2",
                                      "expires_at": time.time() + 3600}) as m_refresh:
            result = backend_client.fetch_ephemeral_token()
        self.assertEqual(result["token"], "dg-jwt")
        self.assertEqual(len(calls), 2)                # 初回 + 再試行
        self.assertEqual(calls[0], "Bearer tok-abc")  # 初回は元トークン
        self.assertEqual(calls[1], "Bearer tok-new")  # 再試行は更新後トークン
        m_refresh.assert_called_once()

    def test_no_retry_when_refresh_fails(self):
        """refresh が失敗（401）したら再試行せず元の 401 を返す。"""
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(401, json={"error": "expired"})

        _install_mock(handler)
        with mock.patch("src.core.auth_client.refresh",
                        side_effect=backend_client.BackendError("再ログイン", status=401)):
            with self.assertRaises(backend_client.BackendError) as cm:
                backend_client.fetch_ephemeral_token()
        self.assertEqual(cm.exception.status, 401)
        self.assertEqual(len(calls), 1)  # リフレッシュ失敗 → 再試行しない

    def test_retry_only_once(self):
        """再試行も 401 なら無限ループせず一度だけで諦める（_allow_refresh=False）。"""
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(401, json={})

        _install_mock(handler)
        with mock.patch("src.core.auth_client.refresh",
                        return_value={"access_token": "tok-new", "refresh_token": "r2",
                                      "expires_at": time.time() + 3600}):
            with self.assertRaises(backend_client.BackendError) as cm:
                backend_client.fetch_ephemeral_token()
        self.assertEqual(cm.exception.status, 401)
        self.assertEqual(len(calls), 2)  # 初回 + 再試行1回のみ

    def test_proactive_refresh_before_expiry(self):
        """失効間際なら送信前に ensure_valid_session がリフレッシュする（先回り）。"""
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"provider": "deepgram", "token": "t", "expires_in": 60})

        _install_mock(handler)
        # 失効まで 10 秒（< 60 秒）のセッションに差し替える
        with mock.patch.object(backend_client.secrets, "get_auth_session",
                               return_value={"access_token": "tok-old", "refresh_token": "r",
                                             "expires_at": time.time() + 10}):
            with mock.patch("src.core.auth_client.refresh",
                            return_value={"access_token": "tok-fresh", "refresh_token": "r2",
                                          "expires_at": time.time() + 3600}) as m_refresh:
                backend_client.fetch_ephemeral_token()
        m_refresh.assert_called_once()


if __name__ == "__main__":
    unittest.main()
