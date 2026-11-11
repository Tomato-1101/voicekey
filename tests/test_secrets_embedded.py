"""secrets の API キー取得と、配布生成物にキーが埋め込まれないことのテスト。

実 keyring / Keychain には絶対に触れない（全て偽モジュールに差し替える）。
ユーザーの画面に資格情報ダイアログを出さないための恒久ルール。

get_api_key はキーを OS のシークレットストア（keyring）からのみ取得する。
生成スクリプト（scripts/build/generate_embedded_keys.py）はキーを含まないマーカーだけを出す。
"""

import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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


class TestGenerateEmbeddedKeys(unittest.TestCase):
    """scripts/build/generate_embedded_keys.py が IS_DIST だけのキーレスマーカーを生成する。"""

    SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "build" / "generate_embedded_keys.py"

    def _load_script(self):
        """生成スクリプトをモジュールとして読み込む（scripts/ はパッケージ外のため）。"""
        spec = importlib.util.spec_from_file_location("gen_keys", self.SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_generates_keyless_marker(self):
        """生成物は IS_DIST=True のみで、鍵復元 API（get_key）や payload を持たない。"""
        gen = self._load_script()
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "embedded_keys.py"
            with mock.patch.object(gen, "OUT", out):
                self.assertEqual(gen.main(), 0)

            text = out.read_text(encoding="utf-8")
            # キー埋め込みの痕跡（旧 XOR 実装の名残）が一切無い
            for banned in ("get_key", "_MASK", "_PAYLOAD", "payload"):
                self.assertNotIn(banned, text, f"生成物に '{banned}' が残っている")

            # 読み込むと IS_DIST=True・IS_PERSONAL=False のフラグだけを持つ
            spec = importlib.util.spec_from_file_location("embedded_test", out)
            embedded = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(embedded)
            self.assertTrue(embedded.IS_DIST)
            self.assertFalse(embedded.IS_PERSONAL)
            self.assertFalse(hasattr(embedded, "get_key"))

    def test_personal_flag_generates_personal_marker(self):
        """--personal は IS_PERSONAL=True・IS_DIST=False のキーレスマーカーを生成する。"""
        gen = self._load_script()
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "embedded_keys.py"
            with mock.patch.object(gen, "OUT", out):
                self.assertEqual(gen.main(["--personal"]), 0)
            text = out.read_text(encoding="utf-8")
            for banned in ("get_key", "_MASK", "_PAYLOAD", "payload"):
                self.assertNotIn(banned, text, f"生成物に '{banned}' が残っている")
            spec = importlib.util.spec_from_file_location("embedded_personal_test", out)
            embedded = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(embedded)
            self.assertTrue(embedded.IS_PERSONAL)
            self.assertFalse(embedded.IS_DIST)

    def test_unknown_argument_is_rejected(self):
        """不明な引数は何も生成せず終了コード 2（Mac の generate_embedded_keys.sh と同じ）。"""
        gen = self._load_script()
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "embedded_keys.py"
            with mock.patch.object(gen, "OUT", out):
                self.assertEqual(gen.main(["--dist"]), 2)
            self.assertFalse(out.exists())


if __name__ == "__main__":
    unittest.main()
