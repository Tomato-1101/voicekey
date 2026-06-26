"""ブラウザ経由ログイン司令塔（login_coordinator）のテスト。

deep link の解析・state 照合・コード交換の呼び分けを検証する。
ネットワーク・実 keyring には触れない（auth_client をモック）。
"""

import unittest
from unittest import mock

from src.core import auth_client, backend_client
from src.core.backend_client import BackendError
from src.core.login_coordinator import LoginCoordinator


class TestParseAuthURL(unittest.TestCase):
    """URL 解析（純粋関数）。"""

    def test_valid(self):
        result = LoginCoordinator.parse_auth_url("voicekey://auth?code=abc&state=xyz")
        self.assertEqual(result, ("abc", "xyz"))

    def test_wrong_scheme(self):
        self.assertIsNone(LoginCoordinator.parse_auth_url("https://auth?code=a&state=b"))

    def test_wrong_host(self):
        self.assertIsNone(LoginCoordinator.parse_auth_url("voicekey://other?code=a&state=b"))

    def test_missing_code(self):
        self.assertIsNone(LoginCoordinator.parse_auth_url("voicekey://auth?state=b"))

    def test_missing_state(self):
        self.assertIsNone(LoginCoordinator.parse_auth_url("voicekey://auth?code=a"))

    def test_garbage(self):
        self.assertIsNone(LoginCoordinator.parse_auth_url("not a url"))


class TestBeginLogin(unittest.TestCase):
    def setUp(self):
        # 初期化時の get_auth_session を未ログイン固定
        self._p = mock.patch.object(auth_client.secrets, "get_auth_session", return_value=None)
        self._p.start()

    def tearDown(self):
        self._p.stop()

    def test_returns_url_and_sets_pending(self):
        coord = LoginCoordinator()
        self.assertEqual(coord.status, LoginCoordinator.IDLE)
        with mock.patch.object(auth_client, "make_state", return_value="st-fixed"), \
             mock.patch.object(auth_client, "make_login_url", return_value="http://x/auth/app?state=st-fixed"):
            url = coord.begin_login()
        self.assertEqual(url, "http://x/auth/app?state=st-fixed")
        self.assertEqual(coord.status, LoginCoordinator.WAITING)


class TestCompleteLogin(unittest.TestCase):
    def setUp(self):
        self._p = mock.patch.object(auth_client.secrets, "get_auth_session", return_value=None)
        self._p.start()
        self.coord = LoginCoordinator()

    def tearDown(self):
        self._p.stop()

    def _begin_with_state(self, state):
        with mock.patch.object(auth_client, "make_state", return_value=state), \
             mock.patch.object(auth_client, "make_login_url", return_value="http://x"):
            self.coord.begin_login()

    def test_success_exchanges_and_logs_in(self):
        self._begin_with_state("st-1")
        with mock.patch.object(auth_client, "exchange_code", return_value={"access_token": "a"}) as ex:
            handled = self.coord.complete_login("voicekey://auth?code=CODE1&state=st-1")
        self.assertTrue(handled)
        ex.assert_called_once_with("CODE1")
        self.assertEqual(self.coord.status, LoginCoordinator.LOGGED_IN)

    def test_state_mismatch_rejected(self):
        self._begin_with_state("st-1")
        with mock.patch.object(auth_client, "exchange_code") as ex:
            handled = self.coord.complete_login("voicekey://auth?code=CODE1&state=ATTACKER")
        self.assertTrue(handled)          # 自分宛て URL ではあるが
        ex.assert_not_called()            # state 不一致なので交換しない
        self.assertEqual(self.coord.status, LoginCoordinator.FAILED)

    def test_no_pending_state_rejected(self):
        # begin_login していない（保留 state が無い）状態で来た deep link は拒否
        with mock.patch.object(auth_client, "exchange_code") as ex:
            handled = self.coord.complete_login("voicekey://auth?code=CODE1&state=st-1")
        self.assertTrue(handled)
        ex.assert_not_called()
        self.assertEqual(self.coord.status, LoginCoordinator.FAILED)

    def test_unrelated_url_not_handled(self):
        self._begin_with_state("st-1")
        handled = self.coord.complete_login("https://example.com/other")
        self.assertFalse(handled)         # 自分宛てではない
        self.assertEqual(self.coord.status, LoginCoordinator.WAITING)  # 状態は変えない

    def test_exchange_failure_sets_failed(self):
        self._begin_with_state("st-1")
        with mock.patch.object(auth_client, "exchange_code",
                               side_effect=BackendError("コードが無効です", status=410)):
            handled = self.coord.complete_login("voicekey://auth?code=BAD&state=st-1")
        self.assertTrue(handled)
        self.assertEqual(self.coord.status, LoginCoordinator.FAILED)
        self.assertIn("コード", self.coord.error)

    def test_state_consumed_after_use(self):
        """一度使った state は消費され、二度目の deep link は拒否される（再利用防止）。"""
        self._begin_with_state("st-1")
        with mock.patch.object(auth_client, "exchange_code", return_value={"access_token": "a"}):
            self.coord.complete_login("voicekey://auth?code=CODE1&state=st-1")
        # 同じ state で再投入
        with mock.patch.object(auth_client, "exchange_code") as ex:
            self.coord.complete_login("voicekey://auth?code=CODE2&state=st-1")
        ex.assert_not_called()


class TestEntitlement(unittest.TestCase):
    """利用権（アクティベーションキー）の確認・登録を検証する。"""

    def setUp(self):
        # ログイン済み（セッションあり）で初期化する
        self._p = mock.patch.object(auth_client.secrets, "get_auth_session",
                                    return_value={"access_token": "a"})
        self._p.start()
        self.coord = LoginCoordinator()

    def tearDown(self):
        self._p.stop()

    def test_refresh_active(self):
        with mock.patch.object(backend_client, "fetch_account_status",
                               return_value={"email": "u@x.io", "active": True,
                                             "active_until": "2030-01-01T00:00:00Z"}):
            self.coord.refresh_entitlement()
        self.assertEqual(self.coord.entitlement, LoginCoordinator.ENT_ACTIVE)
        self.assertEqual(self.coord.account_email, "u@x.io")
        self.assertEqual(self.coord.entitlement_active_until, "2030-01-01T00:00:00Z")

    def test_refresh_none(self):
        with mock.patch.object(backend_client, "fetch_account_status",
                               return_value={"email": "u@x.io", "active": False, "active_until": None}):
            self.coord.refresh_entitlement()
        self.assertEqual(self.coord.entitlement, LoginCoordinator.ENT_NONE)

    def test_refresh_error(self):
        with mock.patch.object(backend_client, "fetch_account_status",
                               side_effect=BackendError("確認失敗", status=500)):
            self.coord.refresh_entitlement()
        self.assertEqual(self.coord.entitlement, LoginCoordinator.ENT_ERROR)
        self.assertIn("確認失敗", self.coord.entitlement_error)

    def test_refresh_skips_when_logged_out(self):
        """未ログインなら通信せず UNKNOWN にする。"""
        with mock.patch.object(auth_client.secrets, "get_auth_session", return_value=None), \
             mock.patch.object(backend_client, "fetch_account_status") as f:
            self.coord.refresh_entitlement()
        f.assert_not_called()
        self.assertEqual(self.coord.entitlement, LoginCoordinator.ENT_UNKNOWN)

    def test_redeem_success_sets_active(self):
        with mock.patch.object(backend_client, "redeem_activation_key",
                               return_value="2031-06-01T00:00:00Z") as r:
            ok, msg = self.coord.redeem("  KEY-1  ")
        r.assert_called_once_with("KEY-1")  # trim して渡す
        self.assertTrue(ok)
        self.assertEqual(self.coord.entitlement, LoginCoordinator.ENT_ACTIVE)
        self.assertEqual(self.coord.entitlement_active_until, "2031-06-01T00:00:00Z")

    def test_redeem_empty_returns_error_without_call(self):
        with mock.patch.object(backend_client, "redeem_activation_key") as r:
            ok, msg = self.coord.redeem("   ")
        r.assert_not_called()
        self.assertFalse(ok)

    def test_redeem_failure_surfaces_message(self):
        with mock.patch.object(backend_client, "redeem_activation_key",
                               side_effect=BackendError("このキーは使用済みです", status=400)):
            ok, msg = self.coord.redeem("USED")
        self.assertFalse(ok)
        self.assertIn("使用済み", msg)


class TestLogout(unittest.TestCase):
    def test_logout_clears_status(self):
        with mock.patch.object(auth_client.secrets, "get_auth_session", return_value={"access_token": "a"}):
            coord = LoginCoordinator()
        self.assertEqual(coord.status, LoginCoordinator.LOGGED_IN)
        with mock.patch.object(auth_client, "logout") as lo:
            coord.logout()
        lo.assert_called_once()
        self.assertEqual(coord.status, LoginCoordinator.IDLE)
        # 利用権の状態もクリアされる
        self.assertEqual(coord.entitlement, LoginCoordinator.ENT_UNKNOWN)
        self.assertIsNone(coord.account_email)


if __name__ == "__main__":
    unittest.main()
