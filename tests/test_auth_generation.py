"""項目9: ログアウト後のセッション復活防止（認証世代 / in-flight 無効化）のテスト。

実 keyring / 実通信には触れない（メモリ辞書の偽 keyring と httpx.MockTransport）。
検証する不変条件:
- ログアウトがリフレッシュの往復に割り込んでも、後から完了したリフレッシュが
  セッションを保存し直さない（＝ログアウト後の復活防止）。
- 別アカウントのログイン（＝認証世代 +1）が短命トークン取得の往復に割り込んだら、
  旧アカウントのトークンをキャッシュ・返却しない。
- device_id の初回生成を同時呼び出しで直列化する（別々の ID を作らない）。
"""

import threading
import time
import unittest
from unittest import mock

import httpx

from src.core import auth_client, backend_client
from src.utils import secrets


class _FakeKeyring:
    """keyring モジュールの偽物（メモリ辞書）。

    内部 _lock は「辞書の破損防止」だけが目的で、get→set の check-then-act
    （device_id 生成の競合）は保護しない＝直列化ロックの検証を阻害しない。
    """

    def __init__(self, store=None, delay=0.0):
        self.store = store or {}
        self.delay = delay          # get/set に挟む遅延（device_id の競合窓を広げる）
        self.set_calls = 0
        self._lock = threading.Lock()

    def get_password(self, service, username):
        if self.delay:
            time.sleep(self.delay)
        with self._lock:
            return self.store.get((service, username))

    def set_password(self, service, username, value):
        if self.delay:
            time.sleep(self.delay)
        with self._lock:
            self.set_calls += 1
            self.store[(service, username)] = value

    def delete_password(self, service, username):
        with self._lock:
            self.store.pop((service, username), None)


class _AuthBase(unittest.TestCase):
    """実 keyring を偽物へ、サーバー URL を固定し、共有 HTTP クライアントを後始末する。"""

    def setUp(self):
        self._orig_keyring = secrets._keyring_module
        self._fake = _FakeKeyring()
        secrets._keyring_module = self._fake
        with secrets._cache_lock:
            secrets._cache.clear()
        self._url_patch = mock.patch.object(
            secrets, "get_server_base_url", return_value="http://test.local"
        )
        self._url_patch.start()
        backend_client.clear_token_cache()

    def tearDown(self):
        self._url_patch.stop()
        secrets._keyring_module = self._orig_keyring
        backend_client._client = None
        backend_client.clear_token_cache()

    def _install_mock(self, handler):
        backend_client._client = httpx.Client(transport=httpx.MockTransport(handler))


class TestLogoutDuringRefresh(_AuthBase):
    """ログアウトがリフレッシュの往復に割り込んだら、セッションを復活させない。"""

    def test_refresh_does_not_revive_after_logout(self):
        # 旧アカウントのセッションを保存しておく（失効間際にして refresh 対象にする必要はない）
        secrets.save_auth_session("acc-old", "ref-old", time.time() + 3600)

        def handler(request: httpx.Request) -> httpx.Response:
            # サーバー往復の最中にユーザーがログアウトした状況を作る
            # （別スレッドの logout と同じ：世代 +1 ＋ セッション破棄）。
            auth_client.logout()
            return httpx.Response(200, json={
                "access_token": "acc-new", "refresh_token": "ref-new",
                "expires_at": time.time() + 3600,
            })

        self._install_mock(handler)
        with self.assertRaises(backend_client.BackendError) as cm:
            auth_client.refresh()
        self.assertEqual(cm.exception.status, 401)
        # ログアウトで消えたまま＝復活していない
        self.assertIsNone(secrets.get_auth_session())

    def test_refresh_saves_when_no_logout(self):
        """対照: 割り込みが無ければ通常どおり新セッションを保存する（過剰防御でない）。"""
        secrets.save_auth_session("acc-old", "ref-old", time.time() + 3600)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "access_token": "acc-new", "refresh_token": "ref-new",
                "expires_at": time.time() + 3600,
            })

        self._install_mock(handler)
        result = auth_client.refresh()
        self.assertEqual(result["access_token"], "acc-new")
        self.assertEqual(secrets.get_auth_session()["access_token"], "acc-new")


class TestAccountSwitchDuringTokenFetch(_AuthBase):
    """別アカウントのログイン（世代 +1）が短命トークン取得に割り込んだら旧トークンを採らない。"""

    def test_ephemeral_token_not_cached_after_generation_bump(self):
        secrets.save_auth_session("acc", "ref", time.time() + 3600)

        def handler(request: httpx.Request) -> httpx.Response:
            # 取得の往復中に別アカウントのログインが完了した状況を作る。
            # 実際の exchange_code は別スレッドで世代 +1 する（同期モック内では
            # clear_token_cache の _token_lock 再取得で詰まるため、世代だけ直接進める）。
            with auth_client._generation_lock:
                auth_client._auth_generation += 1
            return httpx.Response(200, json={"token": "dg-old", "expires_in": 60})

        self._install_mock(handler)
        with self.assertRaises(backend_client.BackendError) as cm:
            backend_client.fetch_ephemeral_token()
        self.assertEqual(cm.exception.status, 401)
        # 旧アカウントのトークンでキャッシュが汚染されていない
        self.assertIsNone(backend_client._cached_token)

    def test_ephemeral_token_cached_when_no_switch(self):
        """対照: 割り込みが無ければ通常どおり取得・キャッシュする。"""
        secrets.save_auth_session("acc", "ref", time.time() + 3600)
        self._install_mock(
            lambda r: httpx.Response(200, json={"token": "dg-1", "expires_in": 60})
        )
        result = backend_client.fetch_ephemeral_token()
        self.assertEqual(result["token"], "dg-1")
        self.assertIsNotNone(backend_client._cached_token)


class TestConcurrentDeviceIdGeneration(unittest.TestCase):
    """device_id の初回生成を同時呼び出しでも 1 本に直列化する（別々の ID を作らない）。"""

    def setUp(self):
        self._orig = secrets._keyring_module

    def tearDown(self):
        secrets._keyring_module = self._orig

    def test_concurrent_callers_get_same_id(self):
        # get に遅延を入れて「複数スレッドが同時に未登録を観測する」競合窓を広げる
        kr = _FakeKeyring(delay=0.02)
        secrets._keyring_module = kr

        results = []
        results_lock = threading.Lock()

        def worker():
            value = secrets.get_device_id()
            with results_lock:
                results.append(value)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(results), 10)
        self.assertEqual(len(set(results)), 1)   # 全員が同じ ID を共有した
        self.assertEqual(kr.set_calls, 1)        # 生成・保存は 1 回だけ（直列化された）


if __name__ == "__main__":
    unittest.main()
