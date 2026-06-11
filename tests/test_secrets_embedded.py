"""secrets の埋め込みキーフォールバックと生成スクリプトのテスト。

実 keyring / Keychain には絶対に触れない（全て偽モジュールに差し替える）。
ユーザーの画面に資格情報ダイアログを出さないための恒久ルール。
"""

import importlib.util
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from src.utils import secrets


def _fake_embedded(keys: dict, is_dist: bool = True) -> types.SimpleNamespace:
    """embedded_keys モジュールの偽物を作る。"""
    return types.SimpleNamespace(IS_DIST=is_dist, get_key=lambda svc: keys.get(svc))


class _FakeKeyring:
    """keyring モジュールの偽物（メモリ辞書）。"""

    def __init__(self, store=None, raise_on_get=False):
        self.store = store or {}
        self.raise_on_get = raise_on_get

    def get_password(self, service, username):
        if self.raise_on_get:
            raise RuntimeError("backend unavailable")
        return self.store.get(service)


class TestEmbeddedFallback(unittest.TestCase):
    """get_api_key のキー解決チェーン（keyring → 埋め込み）のテスト。"""

    def setUp(self):
        # モジュールグローバルを退避してテストごとに初期化
        self._orig_embedded = secrets._embedded
        self._orig_keyring = secrets._keyring_module
        with secrets._cache_lock:
            secrets._cache.clear()

    def tearDown(self):
        secrets._embedded = self._orig_embedded
        secrets._keyring_module = self._orig_keyring
        with secrets._cache_lock:
            secrets._cache.clear()

    def test_is_dist_build_false_in_dev(self):
        """埋め込みモジュールが無い開発環境では DIST 判定が False。"""
        secrets._embedded = None
        self.assertFalse(secrets.is_dist_build())

    def test_is_dist_build_true_with_embedded(self):
        """IS_DIST=True の埋め込みモジュールがあれば DIST 判定が True。"""
        secrets._embedded = _fake_embedded({})
        self.assertTrue(secrets.is_dist_build())

    def test_keyring_value_wins_over_embedded(self):
        """keyring に保存済みのキーは埋め込みより優先される（開発者自身の環境）。"""
        secrets._keyring_module = _FakeKeyring({secrets.SERVICE_OPENAI: "user-key"})
        secrets._embedded = _fake_embedded({secrets.SERVICE_OPENAI: "embedded-key"})
        self.assertEqual(secrets.get_api_key(secrets.SERVICE_OPENAI), "user-key")

    def test_embedded_used_when_keyring_empty(self):
        """keyring 未登録（テスター環境）では埋め込みキーへフォールバックする。"""
        secrets._keyring_module = _FakeKeyring({})
        secrets._embedded = _fake_embedded({secrets.SERVICE_OPENAI: "embedded-key"})
        self.assertEqual(secrets.get_api_key(secrets.SERVICE_OPENAI), "embedded-key")

    def test_embedded_used_when_keyring_missing(self):
        """keyring モジュール自体が無くても埋め込みキーが使える。"""
        secrets._keyring_module = None
        secrets._embedded = _fake_embedded({secrets.SERVICE_GROQ: "embedded-key"})
        self.assertEqual(secrets.get_api_key(secrets.SERVICE_GROQ), "embedded-key")

    def test_embedded_used_when_keyring_raises(self):
        """keyring が例外を投げても埋め込みキーへフォールバックする。"""
        secrets._keyring_module = _FakeKeyring(raise_on_get=True)
        secrets._embedded = _fake_embedded({secrets.SERVICE_OPENAI: "embedded-key"})
        self.assertEqual(secrets.get_api_key(secrets.SERVICE_OPENAI), "embedded-key")

    def test_cached_none_still_falls_to_embedded(self):
        """未登録（None）キャッシュ後の 2 回目呼び出しでも埋め込みへ落ちる。"""
        secrets._keyring_module = _FakeKeyring({})
        secrets._embedded = _fake_embedded({secrets.SERVICE_OPENAI: "embedded-key"})
        secrets.get_api_key(secrets.SERVICE_OPENAI)  # 1 回目で None がキャッシュされる
        self.assertEqual(secrets.get_api_key(secrets.SERVICE_OPENAI), "embedded-key")

    def test_dev_returns_none_without_embedded(self):
        """開発環境（埋め込みなし・keyring 空）では従来どおり None。"""
        secrets._keyring_module = _FakeKeyring({})
        secrets._embedded = None
        self.assertIsNone(secrets.get_api_key(secrets.SERVICE_OPENAI))


class TestGenerateEmbeddedKeys(unittest.TestCase):
    """scripts/build/generate_embedded_keys.py の生成物ラウンドトリップテスト。"""

    SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "build" / "generate_embedded_keys.py"

    def _load_script(self):
        """生成スクリプトをモジュールとして読み込む（scripts/ はパッケージ外のため）。"""
        spec = importlib.util.spec_from_file_location("gen_keys", self.SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_roundtrip_and_no_plaintext(self):
        """生成 → 読み込みで元のキーに戻り、ファイルに平文キーが残らない。"""
        gen = self._load_script()
        fake_key = "sk-test-roundtrip-key-1234567890"
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "embedded_keys.py"
            with mock.patch.object(gen, "ENV_DIST", Path(tmp) / "no-such-env"), \
                 mock.patch.object(gen, "OUT", out), \
                 mock.patch.dict("os.environ", {"OPENAI_API_KEY": fake_key}, clear=False):
                self.assertEqual(gen.main(), 0)

            # 生成ファイルに平文キーが含まれない（XOR 難読化の確認）
            text = out.read_text(encoding="utf-8")
            self.assertNotIn(fake_key, text)

            # 生成モジュールを読み込むと元のキーに復元できる
            spec = importlib.util.spec_from_file_location("embedded_test", out)
            embedded = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(embedded)
            self.assertTrue(embedded.IS_DIST)
            self.assertEqual(embedded.get_key("voicekey.OpenAI"), fake_key)
            self.assertIsNone(embedded.get_key("voicekey.Groq"))

    def test_no_keys_returns_error(self):
        """キーが 1 件も無ければエラー終了（空の embedded_keys を作らない）。"""
        gen = self._load_script()
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "embedded_keys.py"
            env = {name: "" for name in gen.SERVICES.values()}
            with mock.patch.object(gen, "ENV_DIST", Path(tmp) / "no-such-env"), \
                 mock.patch.object(gen, "OUT", out), \
                 mock.patch.dict("os.environ", env, clear=False):
                self.assertEqual(gen.main(), 1)
            self.assertFalse(out.exists())


if __name__ == "__main__":
    unittest.main()
