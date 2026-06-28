"""secrets の製品版認証基盤（device_id / 認証セッション / サーバー URL）のテスト。

実 keyring / Keychain には絶対に触れない（メモリ辞書の偽物に差し替える）。
ユーザーの画面に資格情報ダイアログを出さないための恒久ルール。
"""

import json
import os
import unittest
from unittest import mock

from src.utils import secrets


class _FakeKeyring:
    """keyring モジュールの偽物（メモリ辞書・get/set/delete 対応）。"""

    def __init__(self, store=None):
        self.store = store or {}

    def get_password(self, service, username):
        return self.store.get((service, username))

    def set_password(self, service, username, value):
        self.store[(service, username)] = value

    def delete_password(self, service, username):
        if (service, username) not in self.store:
            raise RuntimeError("not found")
        del self.store[(service, username)]


class TestDeviceId(unittest.TestCase):
    def setUp(self):
        self._orig = secrets._keyring_module

    def tearDown(self):
        secrets._keyring_module = self._orig

    def test_generate_and_persist(self):
        """初回は生成して保存し、2 回目は同じ値を返す。"""
        kr = _FakeKeyring()
        secrets._keyring_module = kr
        first = secrets.get_device_id()
        self.assertEqual(len(first), 32)  # uuid4 hex
        self.assertEqual(kr.store[(secrets.SERVICE_DEVICE_ID, secrets._USERNAME)], first)
        # 保存済みなら同じ値
        self.assertEqual(secrets.get_device_id(), first)

    def test_no_keyring_still_returns_id(self):
        """keyring 不在でも（永続化はされないが）ID を返す。"""
        secrets._keyring_module = None
        self.assertEqual(len(secrets.get_device_id()), 32)


class TestAuthSession(unittest.TestCase):
    def setUp(self):
        self._orig = secrets._keyring_module

    def tearDown(self):
        secrets._keyring_module = self._orig

    def test_roundtrip(self):
        """save → get で同じセッションが返る。"""
        secrets._keyring_module = _FakeKeyring()
        self.assertIsNone(secrets.get_auth_session())  # 未保存
        ok = secrets.save_auth_session("acc", "ref", 1234.5)
        self.assertTrue(ok)
        got = secrets.get_auth_session()
        self.assertEqual(got, {"access_token": "acc", "refresh_token": "ref", "expires_at": 1234.5})

    def test_clear(self):
        """clear で削除され、以後は None。"""
        secrets._keyring_module = _FakeKeyring()
        secrets.save_auth_session("acc", "ref", 1.0)
        self.assertTrue(secrets.clear_auth_session())
        self.assertIsNone(secrets.get_auth_session())

    def test_corrupt_json_returns_none(self):
        """壊れた JSON は未ログイン扱い（None）。"""
        kr = _FakeKeyring({(secrets.SERVICE_AUTH, secrets._USERNAME): "not-json{"})
        secrets._keyring_module = kr
        self.assertIsNone(secrets.get_auth_session())

    def test_no_keyring(self):
        """keyring 不在では保存不可・取得は None。"""
        secrets._keyring_module = None
        self.assertFalse(secrets.save_auth_session("a", "r", 1.0))
        self.assertIsNone(secrets.get_auth_session())


class TestServerBaseURL(unittest.TestCase):
    def setUp(self):
        self._orig_embedded = secrets._embedded

    def tearDown(self):
        secrets._embedded = self._orig_embedded

    def test_dev_default_localhost(self):
        """開発ビルド（埋め込みなし）は localhost。"""
        secrets._embedded = None
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("VOICEKEY_SERVER_URL", None)
            self.assertEqual(secrets.get_server_base_url(), secrets_local_url())

    def test_dist_default_production(self):
        """配布ビルドは本番 URL。"""
        import types

        secrets._embedded = types.SimpleNamespace(IS_DIST=True, get_key=lambda s: None)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("VOICEKEY_SERVER_URL", None)
            self.assertEqual(secrets.get_server_base_url(), secrets_product_url())

    def test_env_override_wins_in_dev(self):
        """開発ビルドでは環境変数 VOICEKEY_SERVER_URL が最優先（末尾スラッシュ除去）。"""
        secrets._embedded = None
        with mock.patch.dict(os.environ, {"VOICEKEY_SERVER_URL": "https://preview.example/"}, clear=False):
            self.assertEqual(secrets.get_server_base_url(), "https://preview.example")

    def test_env_override_ignored_in_dist(self):
        """配布ビルドでは override を無視して本番 URL に固定する（接続先汚染の防止）。"""
        import types

        secrets._embedded = types.SimpleNamespace(IS_DIST=True, get_key=lambda s: None)
        with mock.patch.dict(os.environ, {"VOICEKEY_SERVER_URL": "https://evil.example/"}, clear=False):
            self.assertEqual(secrets.get_server_base_url(), secrets_product_url())


def secrets_local_url() -> str:
    from src.config.constants import LOCAL_SERVER_URL

    return LOCAL_SERVER_URL


def secrets_product_url() -> str:
    from src.config.constants import PRODUCT_SERVER_URL

    return PRODUCT_SERVER_URL


if __name__ == "__main__":
    unittest.main()
