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
        backend_client.clear_token_cache()  # 短命トークンのモジュールキャッシュをテスト間で持ち越さない

    def tearDown(self):
        for p in self._patches:
            p.stop()
        backend_client._client = None  # 共有クライアントを元に戻す
        backend_client.clear_token_cache()  # 次テストにキャッシュを残さない


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


class TestSyncStats(_Base):
    """sync_stats（実績の送信）の契約を検証する。"""

    def test_posts_days_with_headers(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["path"] = request.url.path
            seen["method"] = request.method
            seen["auth"] = request.headers.get("authorization")
            seen["device"] = request.headers.get("x-device-id")
            import json as _json
            seen["body"] = _json.loads(request.content)
            return httpx.Response(200, json={"ok": True, "upserted": 1})

        _install_mock(handler)
        backend_client.sync_stats([{"day": "2026-06-27", "chars": 50, "sessions": 2, "duration_ms": 3000}])
        self.assertEqual(seen["path"], "/api/v1/stats/sync")
        self.assertEqual(seen["method"], "POST")
        self.assertEqual(seen["auth"], "Bearer tok-abc")
        self.assertEqual(seen["device"], "dev-123")
        self.assertEqual(seen["body"]["days"][0]["chars"], 50)

    def test_empty_days_no_request(self):
        """空リストは通信せず即 return（無駄打ちしない）。"""
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("空リストで通信してはいけない")

        _install_mock(handler)
        backend_client.sync_stats([])  # 例外が出なければ OK

    def test_caps_at_60_days(self):
        """61 日以上送っても 60 日に切り詰める（サーバー上限に合わせる）。"""
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            import json as _json
            seen["body"] = _json.loads(request.content)
            return httpx.Response(200, json={"ok": True})

        _install_mock(handler)
        days = [{"day": f"2026-01-{i:02d}", "chars": 1, "sessions": 1, "duration_ms": 1} for i in range(1, 32)]
        days += [{"day": f"2026-02-{i:02d}", "chars": 1, "sessions": 1, "duration_ms": 1} for i in range(1, 32)]
        backend_client.sync_stats(days)  # 62 日
        self.assertEqual(len(seen["body"]["days"]), 60)


class TestFetchStats(_Base):
    """fetch_stats（実績の取得）の解釈を検証する（ms→秒変換含む）。"""

    def test_parses_daily_and_totals(self):
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, "/api/v1/stats")
            self.assertEqual(request.method, "GET")
            return httpx.Response(200, json={
                "ok": True,
                "daily": [
                    {"day": "2026-06-26", "chars": 100, "sessions": 3, "duration_ms": 12000},
                    {"day": "2026-06-27", "chars": 40, "sessions": 1, "duration_ms": 5000},
                ],
                "totals": {"chars": 140, "sessions": 4, "duration_ms": 17000, "days_active": 2},
            })

        _install_mock(handler)
        r = backend_client.fetch_stats()
        self.assertEqual(r["total_characters"], 140)
        self.assertEqual(r["total_sessions"], 4)
        self.assertAlmostEqual(r["total_recording_seconds"], 17.0)  # 17000ms→17s
        self.assertAlmostEqual(r["daily"]["2026-06-26"]["recording_seconds"], 12.0)
        self.assertEqual(r["daily"]["2026-06-27"]["characters"], 40)

    def test_empty_response(self):
        """daily/totals が無くても落ちず 0 で返す。"""
        _install_mock(lambda r: httpx.Response(200, json={"ok": True}))
        r = backend_client.fetch_stats()
        self.assertEqual(r["total_characters"], 0)
        self.assertEqual(r["daily"], {})


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

    def test_no_entitlement_403(self):
        _install_mock(lambda r: httpx.Response(403, json={"error": "no entitlement"}))
        with self.assertRaises(backend_client.BackendError) as cm:
            backend_client.fetch_ephemeral_token()
        self.assertEqual(cm.exception.status, 403)
        # 未契約は「アクティベーションキーの登録が必要」を案内する（無料配布ゲート）
        self.assertIn("アクティベーション", str(cm.exception))

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


class TestWarmFormatProxy(_Base):
    """整形プロキシ暖機（録音開始時の cold start 対策）を検証する。"""

    def test_logged_in_posts_empty_text(self):
        """ログイン済みなら /api/v1/format へ空テキストを POST して温める。"""
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["path"] = request.url.path
            seen["method"] = request.method
            seen["body"] = request.content
            return httpx.Response(400, json={"error": "text がありません"})  # 空は 400

        _install_mock(handler)
        backend_client.warm_format_proxy()  # 400 でも例外を出さない（暖機が目的）
        self.assertEqual(seen["path"], "/api/v1/format")
        self.assertEqual(seen["method"], "POST")
        self.assertIn(b"text", seen["body"])

    def test_not_logged_in_does_nothing(self):
        """未ログインなら何も送らない（実 keyring にも触れない）。"""
        called = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            called["n"] += 1
            return httpx.Response(200, json={})

        _install_mock(handler)
        with mock.patch.object(backend_client, "is_logged_in", return_value=False):
            backend_client.warm_format_proxy()
        self.assertEqual(called["n"], 0)


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


class TestAccountStatus(_Base):
    """/api/v1/me（アカウント状態）の取得を検証する。"""

    def test_active_with_until(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["path"] = request.url.path
            seen["method"] = request.method
            seen["auth"] = request.headers.get("authorization")
            return httpx.Response(200, json={
                "email": "a@example.com", "active": True,
                "active_until": "2030-01-01T00:00:00Z",
            })

        _install_mock(handler)
        s = backend_client.fetch_account_status()
        self.assertEqual(seen["path"], "/api/v1/me")
        self.assertEqual(seen["method"], "GET")
        self.assertEqual(seen["auth"], "Bearer tok-abc")
        self.assertEqual(s["email"], "a@example.com")
        self.assertTrue(s["active"])
        self.assertEqual(s["active_until"], "2030-01-01T00:00:00Z")

    def test_inactive(self):
        """未契約でも 200 で active:False が返る（呼び出し側はキー入力を促す）。"""
        _install_mock(lambda r: httpx.Response(200, json={"email": "b@x.io", "active": False, "active_until": None}))
        s = backend_client.fetch_account_status()
        self.assertFalse(s["active"])
        self.assertIsNone(s["active_until"])

    def test_unauthenticated_raises(self):
        _install_mock(lambda r: httpx.Response(401, json={"error": "認証が必要です"}))
        with mock.patch.object(backend_client.secrets, "get_auth_session", return_value=None):
            with self.assertRaises(backend_client.BackendError) as cm:
                backend_client.fetch_account_status()
        self.assertEqual(cm.exception.status, 401)


class TestRedeem(_Base):
    """/api/v1/activation/redeem（キー登録）を検証する。"""

    def test_success_returns_active_until(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["path"] = request.url.path
            seen["method"] = request.method
            seen["auth"] = request.headers.get("authorization")
            seen["body"] = request.content
            return httpx.Response(200, json={"ok": True, "active_until": "2031-06-01T00:00:00Z"})

        _install_mock(handler)
        until = backend_client.redeem_activation_key("  KEY-1234  ")
        self.assertEqual(seen["path"], "/api/v1/activation/redeem")
        self.assertEqual(seen["method"], "POST")
        self.assertEqual(seen["auth"], "Bearer tok-abc")
        self.assertIn(b"KEY-1234", seen["body"])          # 送信前に trim される
        self.assertNotIn(b"  KEY-1234  ", seen["body"])
        self.assertEqual(until, "2031-06-01T00:00:00Z")

    def test_server_error_message_surfaced(self):
        """400 でもサーバーの日本語 {error} 本文をそのままメッセージにする。"""
        _install_mock(lambda r: httpx.Response(400, json={"error": "このキーは使用済みです"}))
        with self.assertRaises(backend_client.BackendError) as cm:
            backend_client.redeem_activation_key("USED")
        self.assertEqual(cm.exception.status, 400)
        self.assertIn("使用済み", str(cm.exception))

    def test_error_without_body_falls_back_to_status_message(self):
        """本文が空でもステータスから既定メッセージを引く。"""
        _install_mock(lambda r: httpx.Response(500, text=""))
        with self.assertRaises(backend_client.BackendError) as cm:
            backend_client.redeem_activation_key("X")
        self.assertEqual(cm.exception.status, 500)
        self.assertTrue(str(cm.exception))


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
            # ensure_valid_session は並行リフレッシュ競合を避けるため、ロック保持下で
            # 実体ワーカー _perform_refresh を直接呼ぶ（公開 refresh は経由しない）。
            with mock.patch("src.core.auth_client._perform_refresh",
                            return_value={"access_token": "tok-fresh", "refresh_token": "r2",
                                          "expires_at": time.time() + 3600}) as m_refresh:
                backend_client.fetch_ephemeral_token()
        m_refresh.assert_called_once()


if __name__ == "__main__":
    unittest.main()
