"""製品版 認証クライアント（auth_client）のテスト。

httpx.MockTransport でサーバーを模擬し、実通信・実 keyring に触れない。
サーバー契約（パス・ボディ・レスポンス）が voicekey-site の
app/api/v1/auth/{exchange,refresh} と一致していることを検証する。
"""

import unittest
from unittest import mock

import httpx

from src.core import auth_client, backend_client


def _install_mock(handler):
    """MockTransport を共有クライアントに差し込む。"""
    backend_client._client = httpx.Client(transport=httpx.MockTransport(handler))


class _Base(unittest.TestCase):
    def setUp(self):
        # 共有モジュール（src.utils.secrets）を差し替え、実 keyring に触れない。
        self.saved = {}

        def _save(access_token, refresh_token, expires_at):
            self.saved = {
                "access_token": access_token,
                "refresh_token": refresh_token,
                "expires_at": expires_at,
            }
            return True

        self._patches = [
            mock.patch.object(auth_client.secrets, "get_device_id", return_value="dev-123"),
            mock.patch.object(auth_client.secrets, "get_server_base_url", return_value="http://test.local"),
            mock.patch.object(auth_client.secrets, "save_auth_session", side_effect=_save),
            mock.patch.object(auth_client.secrets, "clear_auth_session", return_value=True),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        backend_client._client = None  # 共有クライアントを元に戻す


class TestStateAndURL(unittest.TestCase):
    """state 生成とログイン URL 構築（通信なし）。"""

    def test_make_state_is_random_hex(self):
        a = auth_client.make_state()
        b = auth_client.make_state()
        self.assertEqual(len(a), 32)
        self.assertNotEqual(a, b)  # 毎回異なる
        int(a, 16)  # hex として解釈できる（例外が出れば失敗）

    def test_make_login_url_contains_params(self):
        with mock.patch.object(auth_client.secrets, "get_server_base_url", return_value="http://test.local"), \
             mock.patch.object(auth_client.secrets, "get_device_id", return_value="dev-xyz"):
            url = auth_client.make_login_url("st-123")
        self.assertTrue(url.startswith("http://test.local/auth/app?"))
        self.assertIn("state=st-123", url)
        self.assertIn("device_id=dev-xyz", url)
        self.assertIn("platform=windows", url)


class TestExchange(_Base):
    def test_success_saves_session(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["path"] = request.url.path
            seen["method"] = request.method
            seen["body"] = request.content
            return httpx.Response(200, json={
                "access_token": "acc-1",
                "refresh_token": "ref-1",
                "expires_at": 1_900_000_000,
                "token_type": "bearer",
            })

        _install_mock(handler)
        result = auth_client.exchange_code("one-time-code")
        self.assertEqual(seen["path"], "/api/v1/auth/exchange")
        self.assertEqual(seen["method"], "POST")
        self.assertIn(b"one-time-code", seen["body"])
        self.assertIn(b"dev-123", seen["body"])
        self.assertIn(b"windows", seen["body"])
        # 返り値と保存内容が一致
        self.assertEqual(result["access_token"], "acc-1")
        self.assertEqual(self.saved["access_token"], "acc-1")
        self.assertEqual(self.saved["refresh_token"], "ref-1")
        self.assertEqual(self.saved["expires_at"], 1_900_000_000.0)

    def test_invalid_code_410(self):
        _install_mock(lambda r: httpx.Response(410, json={"error": "expired"}))
        with self.assertRaises(backend_client.BackendError) as cm:
            auth_client.exchange_code("bad")
        self.assertEqual(cm.exception.status, 410)

    def test_missing_expires_at_defaults(self):
        _install_mock(lambda r: httpx.Response(200, json={
            "access_token": "a", "refresh_token": "b",
        }))
        result = auth_client.exchange_code("c")
        # expires_at 欠落でも未来の値が入る（近い将来でないこと＝現在以降）
        self.assertGreater(result["expires_at"], 0)


class TestRefresh(_Base):
    def test_success_saves_new_session(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["path"] = request.url.path
            seen["body"] = request.content
            return httpx.Response(200, json={
                "access_token": "acc-2",
                "refresh_token": "ref-2",
                "expires_at": 1_900_000_100,
            })

        _install_mock(handler)
        with mock.patch.object(auth_client.secrets, "get_auth_session",
                               return_value={"access_token": "old", "refresh_token": "ref-old", "expires_at": 0}):
            result = auth_client.refresh()
        self.assertEqual(seen["path"], "/api/v1/auth/refresh")
        self.assertIn(b"ref-old", seen["body"])
        self.assertEqual(result["access_token"], "acc-2")
        self.assertEqual(self.saved["refresh_token"], "ref-2")

    def test_no_session_raises_401(self):
        called = {"n": 0}

        def handler(request):
            called["n"] += 1
            return httpx.Response(200, json={})

        _install_mock(handler)
        with mock.patch.object(auth_client.secrets, "get_auth_session", return_value=None):
            with self.assertRaises(backend_client.BackendError) as cm:
                auth_client.refresh()
        self.assertEqual(cm.exception.status, 401)
        self.assertEqual(called["n"], 0)  # 通信していない

    def test_expired_refresh_clears_session(self):
        _install_mock(lambda r: httpx.Response(401, json={"error": "expired"}))
        cleared = {"n": 0}

        def _clear():
            cleared["n"] += 1
            return True

        with mock.patch.object(auth_client.secrets, "get_auth_session",
                               return_value={"refresh_token": "dead", "access_token": "a", "expires_at": 0}), \
             mock.patch.object(auth_client.secrets, "clear_auth_session", side_effect=_clear):
            with self.assertRaises(backend_client.BackendError) as cm:
                auth_client.refresh()
        self.assertEqual(cm.exception.status, 401)
        self.assertEqual(cleared["n"], 1)  # セッションを破棄した

    def test_conflict_409_keeps_session(self):
        # 409（refresh 競合）＝他経路が既に refresh_token を回転済み＝セッションは有効。
        # 破棄も例外もせず、最新の保存済みセッションをそのまま返す（誤って期限切れにしない）。
        _install_mock(lambda r: httpx.Response(409, json={"error": "conflict", "code": "refresh_conflict"}))
        current = {"access_token": "still-valid", "refresh_token": "rot-by-other", "expires_at": 1_900_000_000}
        with mock.patch.object(auth_client.secrets, "get_auth_session", return_value=current):
            result = auth_client.refresh()
        self.assertEqual(result["access_token"], "still-valid")  # 現行セッションを返す（例外なし）
        auth_client.secrets.clear_auth_session.assert_not_called()  # セッションを破棄していない


class TestEnsureValidSession(_Base):
    def test_fresh_token_no_refresh(self):
        """失効まで十分余裕があればリフレッシュしない（通信もしない）。"""
        called = {"n": 0}
        _install_mock(lambda r: (called.__setitem__("n", called["n"] + 1), httpx.Response(200, json={}))[1])
        import time as _t
        future = _t.time() + 3600
        with mock.patch.object(auth_client.secrets, "get_auth_session",
                               return_value={"access_token": "a", "refresh_token": "r", "expires_at": future}):
            auth_client.ensure_valid_session()
        self.assertEqual(called["n"], 0)

    def test_near_expiry_triggers_refresh(self):
        """失効 60 秒前を切っていればリフレッシュする。"""
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "access_token": "new", "refresh_token": "newr", "expires_at": 1_900_000_000,
            })

        _install_mock(handler)
        import time as _t
        soon = _t.time() + 10  # 10 秒後に失効
        with mock.patch.object(auth_client.secrets, "get_auth_session",
                               return_value={"access_token": "a", "refresh_token": "r", "expires_at": soon}):
            auth_client.ensure_valid_session()
        self.assertEqual(self.saved["access_token"], "new")

    def test_not_logged_in_raises(self):
        with mock.patch.object(auth_client.secrets, "get_auth_session", return_value=None):
            with self.assertRaises(backend_client.BackendError) as cm:
                auth_client.ensure_valid_session()
        self.assertEqual(cm.exception.status, 401)


if __name__ == "__main__":
    unittest.main()
