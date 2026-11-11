"""secrets の端末識別子（device_id）のテスト。

device_id は認証子ではなく、フィードバック送信で同じ端末からの送信を見分けるための識別子。
実 keyring / Keychain には絶対に触れない（メモリ辞書の偽物に差し替える）。
"""

import threading
import time
import unittest

from src.utils import secrets


class _FakeKeyring:
    """keyring モジュールの偽物（メモリ辞書）。

    内部 _lock は「辞書の破損防止」だけが目的で、get→set の check-then-act
    （device_id 生成の競合）は保護しない＝直列化ロックの検証を阻害しない。
    """

    def __init__(self, delay=0.0):
        self.store = {}
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

    def test_concurrent_callers_get_same_id(self):
        """初回生成を同時呼び出しでも 1 本に直列化する（別々の ID を作らない）。"""
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
