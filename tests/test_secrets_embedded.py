"""secrets の API キー取得と、配布生成物にキーが埋め込まれないことのテスト。

実 keyring / Keychain には絶対に触れない（全て偽モジュールに差し替える）。
ユーザーの画面に資格情報ダイアログを出さないための恒久ルール。

get_api_key はキーを OS のシークレットストア（keyring）からのみ取得する
（配布物にキーを埋め込まないので、埋め込みへ落ちる経路が無いことを固定する）。
"""

import unittest

from src.utils import secrets


class _FakeKeyring:
    """keyring モジュールの偽物（メモリ辞書）。"""

    def __init__(self, store=None, raise_on_get=False):
        self.store = store or {}
        self.raise_on_get = raise_on_get

    def get_password(self, service, username):
        if self.raise_on_get:
            raise RuntimeError("backend unavailable")
        return self.store.get(service)


class TestApiKeyNoEmbeddedFallback(unittest.TestCase):
    """get_api_key はキーを keyring からのみ取得し、埋め込みフォールバックを持たない。"""

    def setUp(self):
        self._orig_keyring = secrets._keyring_module
        with secrets._cache_lock:
            secrets._cache.clear()

    def tearDown(self):
        secrets._keyring_module = self._orig_keyring
        with secrets._cache_lock:
            secrets._cache.clear()

    def test_keyring_value_is_returned(self):
        """keyring に保存済みのキーはそのまま返る（開発者自身の環境）。"""
        secrets._keyring_module = _FakeKeyring({secrets.SERVICE_OPENAI: "user-key"})
        self.assertEqual(secrets.get_api_key(secrets.SERVICE_OPENAI), "user-key")

    def test_empty_keyring_returns_none(self):
        """keyring が空ならキーは無い（埋め込みへ落ちない）。"""
        secrets._keyring_module = _FakeKeyring({})
        self.assertIsNone(secrets.get_api_key(secrets.SERVICE_GROQ))

    def test_no_keyring_module_returns_none(self):
        """keyring モジュール自体が無ければ None（埋め込みは存在しない）。"""
        secrets._keyring_module = None
        self.assertIsNone(secrets.get_api_key(secrets.SERVICE_GROQ))

    def test_keyring_raises_returns_none(self):
        """keyring が例外を投げても埋め込みへ落ちず None を返す。"""
        secrets._keyring_module = _FakeKeyring(raise_on_get=True)
        self.assertIsNone(secrets.get_api_key(secrets.SERVICE_OPENAI))

    def test_none_is_cached_and_stays_none(self):
        """未登録（None）はキャッシュされ、2 回目も None のまま（埋め込みへ落ちない）。"""
        secrets._keyring_module = _FakeKeyring({})
        self.assertIsNone(secrets.get_api_key(secrets.SERVICE_OPENAI))  # 1 回目で None をキャッシュ
        self.assertIsNone(secrets.get_api_key(secrets.SERVICE_OPENAI))


if __name__ == "__main__":
    unittest.main()
