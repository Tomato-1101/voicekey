"""設定 › API キー の読み順・状態・正規化（utils/secrets.py）のテスト。

本物の資格情報マネージャーには触れず、偽の keyring で検証する。
"""

import os
import unittest
from unittest import mock

from src.utils import secrets


class _FakeKeyring:
    """keyring の get/set/delete を辞書で模した偽物（(service, username) → 値）。"""

    def __init__(self, data=None):
        self.data = dict(data or {})

    def get_password(self, service, username):
        return self.data.get((service, username))

    def set_password(self, service, username, value):
        self.data[(service, username)] = value

    def delete_password(self, service, username):
        self.data.pop((service, username), None)


class TestResolveOrder(unittest.TestCase):
    def setUp(self):
        self._orig = secrets._keyring_module
        secrets._cache.clear()
        self.addCleanup(self._restore)

    def _restore(self):
        secrets._keyring_module = self._orig
        secrets._cache.clear()

    def test_pure_resolve_is_lazy_and_ordered(self):
        calls = []

        def reader(name, value):
            def _r():
                calls.append(name)
                return value
            return _r

        found = secrets.resolve(reader("app", "A"), reader("env", "E"), reader("central", "C"))
        self.assertEqual(found, ("A", secrets.SOURCE_APP))
        self.assertEqual(calls, ["app"])  # 手前で見つかれば後段は読まない
        found = secrets.resolve(reader("app", ""), reader("env", None), reader("central", "C"))
        self.assertEqual(found, ("C", secrets.SOURCE_CENTRAL))
        self.assertIsNone(secrets.resolve(lambda: None, lambda: "", lambda: None))

    def test_app_then_env_then_central(self):
        fake = _FakeKeyring({("SONIOX_API_KEY", "shared"): "central-key"})
        secrets._keyring_module = fake
        env = {k: v for k, v in os.environ.items() if k != "SONIOX_API_KEY"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(secrets.api_key("soniox"), "central-key")
            self.assertEqual(secrets.api_key_source("soniox"), secrets.SOURCE_CENTRAL)
        with mock.patch.dict(os.environ, {"SONIOX_API_KEY": "env-key"}):
            self.assertEqual(secrets.api_key("soniox"), "env-key")
            self.assertEqual(secrets.api_key_source("soniox"), secrets.SOURCE_ENVIRONMENT)
            self.assertTrue(secrets.save_item("soniox", "app-key"))
            self.assertEqual(secrets.api_key("soniox"), "app-key")
            self.assertEqual(secrets.api_key_source("soniox"), secrets.SOURCE_APP)
            # アプリの値を消すと環境変数へ戻る（共有の置き場には触らない）
            self.assertTrue(secrets.delete_item("soniox"))
            self.assertEqual(secrets.api_key("soniox"), "env-key")
        self.assertEqual(fake.data[("SONIOX_API_KEY", "shared")], "central-key")
        # アプリの保存先は Mac と同じサービス名
        self.assertNotIn(("voicekey.Soniox", "default"), fake.data)

    def test_save_sanitizes_pasted_variable(self):
        fake = _FakeKeyring()
        secrets._keyring_module = fake
        self.assertTrue(secrets.save_item("groq", '  export GROQ_API_KEY="gsk_abc"  '))
        self.assertEqual(fake.data[("voicekey.Groq", "default")], "gsk_abc")
        # 空になる入力は保存しない
        self.assertFalse(secrets.save_item("groq", "   "))
        self.assertFalse(secrets.save_item("unknown-item", "x"))

    def test_sanitize_rules(self):
        self.assertEqual(secrets.sanitize_key("SONIOX_API_KEY=abc"), "abc")
        self.assertEqual(secrets.sanitize_key("'abc'"), "abc")
        self.assertEqual(secrets.sanitize_key(" abc \n"), "abc")
        self.assertEqual(secrets.sanitize_key('"'), '"')

    def test_stale_read_does_not_overwrite_saved_value(self):
        """読み出し中に保存が挟まっても、古い値でキャッシュを上書きしない（世代つき）。"""
        fake = _FakeKeyring({("voicekey.Groq", "default"): "old"})
        secrets._keyring_module = fake
        original_get = fake.get_password

        def slow_get(service, username):
            value = original_get(service, username)
            # 読み出しの最中に新しいキーが保存された状況を作る
            secrets.set_api_key("voicekey.Groq", "new")
            return value

        fake.get_password = slow_get
        self.assertEqual(secrets.get_api_key("voicekey.Groq"), "old")
        fake.get_password = original_get
        self.assertEqual(secrets.get_api_key("voicekey.Groq"), "new")

    def test_items_cover_all_engines(self):
        from src.config import backends
        for backend in backends.SUPPORTED:
            self.assertIn(backends.key_item(backend), secrets.API_KEY_ITEMS)
        self.assertIn("gemini", secrets.API_KEY_ITEMS)
        self.assertIn("azure_endpoint", secrets.API_KEY_ITEMS)


if __name__ == "__main__":
    unittest.main()
