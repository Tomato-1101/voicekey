"""secrets の DIST 判定と、配布生成物にキーが埋め込まれないことのテスト。

実 keyring / Keychain には絶対に触れない（全て偽モジュールに差し替える）。
ユーザーの画面に資格情報ダイアログを出さないための恒久ルール。

2026-06-28 のセキュリティ修正で、配布バイナリには長期プロバイダーキーを 1 バイトも
埋め込まなくなった（製品版は自社サーバー経由）。よって get_api_key の埋め込みキー
フォールバックは撤去され、生成スクリプトは IS_DIST フラグだけのマーカーを出す。
ここではその「キーは keyring からのみ」「生成物はキーレス」を回帰として固定する。
"""

import importlib.util
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from src.utils import secrets


def _fake_marker(is_dist: bool = True, is_personal: bool = False) -> types.SimpleNamespace:
    """ビルド種別マーカーモジュール（embedded_keys）の偽物。IS_DIST / IS_PERSONAL フラグだけを持つ。"""
    return types.SimpleNamespace(IS_DIST=is_dist, IS_PERSONAL=is_personal)


class _FakeKeyring:
    """keyring モジュールの偽物（メモリ辞書）。"""

    def __init__(self, store=None, raise_on_get=False):
        self.store = store or {}
        self.raise_on_get = raise_on_get

    def get_password(self, service, username):
        if self.raise_on_get:
            raise RuntimeError("backend unavailable")
        return self.store.get(service)


class TestIsDistBuild(unittest.TestCase):
    """is_dist_build はマーカーモジュールの IS_DIST だけで判定する。"""

    def setUp(self):
        self._orig_embedded = secrets._embedded

    def tearDown(self):
        secrets._embedded = self._orig_embedded

    def test_false_in_dev(self):
        """マーカーが無い開発環境では DIST 判定が False。"""
        secrets._embedded = None
        self.assertFalse(secrets.is_dist_build())

    def test_true_with_marker(self):
        """IS_DIST=True のマーカーがあれば DIST 判定が True。"""
        secrets._embedded = _fake_marker(True)
        self.assertTrue(secrets.is_dist_build())

    def test_false_when_marker_is_dist_false(self):
        """IS_DIST=False のスタブでは DIST 判定が False。"""
        secrets._embedded = _fake_marker(False)
        self.assertFalse(secrets.is_dist_build())


class TestIsPersonalBuild(unittest.TestCase):
    """personal（個人用最速版）はマーカーの IS_PERSONAL で判定し、認証セッションを常に無視する。

    Mac の Keychain.authSession()（personal なら nil）と TranscribeRouteTests に対応する。
    旧 release（DIST）利用時の失効済みトークンが Credential Manager に残っていても、
    personal ではログイン扱いにならず、Groq/Deepgram が応答しないサーバー経由へ送られない
    （2026-09-02 の Windows 実機で文字起こしが全滅した不具合の回帰テスト）。
    """

    _SESSION_JSON = '{"access_token": "stale", "refresh_token": "r", "expires_at": 1.0}'

    def setUp(self):
        self._orig_embedded = secrets._embedded
        self._orig_keyring = secrets._keyring_module

    def tearDown(self):
        secrets._embedded = self._orig_embedded
        secrets._keyring_module = self._orig_keyring

    def test_false_in_dev_and_dist(self):
        """マーカー無し・DIST マーカー・IS_PERSONAL 属性の無い旧マーカーはいずれも personal でない。"""
        secrets._embedded = None
        self.assertFalse(secrets.is_personal_build())
        secrets._embedded = _fake_marker(is_dist=True)
        self.assertFalse(secrets.is_personal_build())
        secrets._embedded = types.SimpleNamespace(IS_DIST=True)  # IS_PERSONAL を持たない旧生成物
        self.assertFalse(secrets.is_personal_build())

    def test_true_with_personal_marker(self):
        """IS_PERSONAL=True のマーカーがあれば personal 判定が True（DIST は False のまま）。"""
        secrets._embedded = _fake_marker(is_dist=False, is_personal=True)
        self.assertTrue(secrets.is_personal_build())
        self.assertFalse(secrets.is_dist_build())

    def test_personal_ignores_stored_auth_session(self):
        """personal では keyring に認証セッションが残っていても get_auth_session は None。"""
        secrets._keyring_module = _FakeKeyring({secrets.SERVICE_AUTH: self._SESSION_JSON})
        secrets._embedded = _fake_marker(is_dist=False, is_personal=True)
        self.assertIsNone(secrets.get_auth_session())

    def test_non_personal_still_reads_auth_session(self):
        """personal でなければ同じ keyring 内容からセッションが読める（挙動を変えていない）。"""
        secrets._keyring_module = _FakeKeyring({secrets.SERVICE_AUTH: self._SESSION_JSON})
        secrets._embedded = None
        self.assertEqual(secrets.get_auth_session()["access_token"], "stale")

    def test_personal_is_never_logged_in(self):
        """personal では backend_client.is_logged_in() が False＝サーバー経路（プロキシ/短命JWT）へ行かない。"""
        from src.core import backend_client

        secrets._keyring_module = _FakeKeyring({secrets.SERVICE_AUTH: self._SESSION_JSON})
        secrets._embedded = _fake_marker(is_dist=False, is_personal=True)
        self.assertFalse(backend_client.is_logged_in())


class TestApiKeyNoEmbeddedFallback(unittest.TestCase):
    """get_api_key はキーを keyring からのみ取得し、埋め込みフォールバックを持たない。"""

    def setUp(self):
        self._orig_embedded = secrets._embedded
        self._orig_keyring = secrets._keyring_module
        with secrets._cache_lock:
            secrets._cache.clear()

    def tearDown(self):
        secrets._embedded = self._orig_embedded
        secrets._keyring_module = self._orig_keyring
        with secrets._cache_lock:
            secrets._cache.clear()

    def test_keyring_value_is_returned(self):
        """keyring に保存済みのキーはそのまま返る（開発者自身の環境）。"""
        secrets._keyring_module = _FakeKeyring({secrets.SERVICE_OPENAI: "user-key"})
        secrets._embedded = _fake_marker(True)
        self.assertEqual(secrets.get_api_key(secrets.SERVICE_OPENAI), "user-key")

    def test_dist_with_empty_keyring_returns_none(self):
        """配布ビルドでも keyring が空ならキーは無い（埋め込みへ落ちない）。"""
        secrets._keyring_module = _FakeKeyring({})
        secrets._embedded = _fake_marker(True)
        self.assertIsNone(secrets.get_api_key(secrets.SERVICE_GROQ))

    def test_no_keyring_module_returns_none(self):
        """keyring モジュール自体が無ければ None（埋め込みは存在しない）。"""
        secrets._keyring_module = None
        secrets._embedded = _fake_marker(True)
        self.assertIsNone(secrets.get_api_key(secrets.SERVICE_GROQ))

    def test_keyring_raises_returns_none(self):
        """keyring が例外を投げても埋め込みへ落ちず None を返す。"""
        secrets._keyring_module = _FakeKeyring(raise_on_get=True)
        secrets._embedded = _fake_marker(True)
        self.assertIsNone(secrets.get_api_key(secrets.SERVICE_OPENAI))

    def test_none_is_cached_and_stays_none(self):
        """未登録（None）はキャッシュされ、2 回目も None のまま（埋め込みへ落ちない）。"""
        secrets._keyring_module = _FakeKeyring({})
        secrets._embedded = _fake_marker(True)
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
