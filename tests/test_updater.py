"""自動アップデータ（src/utils/updater.py）のロジックテスト。

ネットワーク・サブプロセスは全てモックし、実際の DL / インストーラ起動は行わない。
Qt のイベントループは使わず、シグナルの直接接続（同期発火）で検証する。
"""

import base64
import hashlib
import io
import json
import tempfile
import unittest
from unittest import mock

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from src.utils.update_signing import sign_ed25519
from src.utils.updater import Updater, parse_version


def _make_test_keypair():
    """テスト用 Ed25519 鍵を生成し (秘密 seed の base64, 公開鍵の base64) を返す。"""
    key = Ed25519PrivateKey.generate()
    seed = key.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
    )
    pub = key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return base64.b64encode(seed).decode("ascii"), base64.b64encode(pub).decode("ascii")


class TestParseVersion(unittest.TestCase):
    """バージョン文字列パースのテスト。"""

    def test_basic(self):
        """"1.2.3" がタプル (1, 2, 3) になる。"""
        self.assertEqual(parse_version("1.2.3"), (1, 2, 3))

    def test_comparison(self):
        """新旧比較が数値として正しい（文字列比較だと 1.10 < 1.9 になる罠の確認）。"""
        self.assertGreater(parse_version("1.10.0"), parse_version("1.9.9"))
        self.assertGreater(parse_version("2.0.0"), parse_version("1.99.99"))
        self.assertEqual(parse_version("1.0.0"), parse_version("1.0.0"))

    def test_invalid_raises(self):
        """数値でないバージョンは ValueError。"""
        with self.assertRaises(ValueError):
            parse_version("abc")
        with self.assertRaises(ValueError):
            parse_version("")

    def test_v_prefix_and_prerelease_tolerated(self):
        """先頭 v とプレリリース/ビルドメタ（-beta, +build）を許容して数値化する。"""
        self.assertEqual(parse_version("v1.2.3"), (1, 2, 3))
        self.assertEqual(parse_version("V2.0.0"), (2, 0, 0))
        self.assertEqual(parse_version("1.2.3-beta"), (1, 2, 3))
        self.assertEqual(parse_version("1.2.3+build9"), (1, 2, 3))


def _fake_urlopen(payload: dict):
    """version.json の HTTP 応答を偽装する urlopen のモックを返す。"""
    body = json.dumps(payload).encode("utf-8")

    class _Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.close()

    return mock.MagicMock(return_value=_Response(body))


class TestUpdaterCheck(unittest.TestCase):
    """バージョンチェック（_check）のテスト。"""

    def setUp(self):
        self.updater = Updater()
        self.available = []
        self.updater.update_available.connect(self.available.append)

    def test_newer_version_emits_signal(self):
        """現行より新しいバージョンで update_available が発火する。"""
        info = {"version": "99.0.0", "url": "http://example/setup.exe", "sha256": "x"}
        with mock.patch("urllib.request.urlopen", _fake_urlopen(info)):
            self.updater._check()
        self.assertEqual(self.available, ["99.0.0"])
        self.assertEqual(self.updater._info, info)

    def test_same_version_no_signal(self):
        """現行と同じバージョンでは何も起きない。"""
        with mock.patch("src.utils.updater.APP_VERSION", "1.0.0"), \
             mock.patch("urllib.request.urlopen", _fake_urlopen({"version": "1.0.0"})):
            self.updater._check()
        self.assertEqual(self.available, [])
        self.assertIsNone(self.updater._info)

    def test_older_version_no_signal(self):
        """フィードが古い（ロールバック）場合は無視する。"""
        with mock.patch("src.utils.updater.APP_VERSION", "2.0.0"), \
             mock.patch("urllib.request.urlopen", _fake_urlopen({"version": "1.0.0"})):
            self.updater._check()
        self.assertEqual(self.available, [])

    def test_network_error_is_swallowed(self):
        """ネットワーク断では例外を投げず、シグナルも出さない（次回チェックに任せる）。"""
        with mock.patch("urllib.request.urlopen", side_effect=OSError("offline")):
            self.updater._check()  # 例外が漏れないこと
        self.assertEqual(self.available, [])

    def test_start_noop_in_dev_build(self):
        """開発ビルドでは start() が定期チェックを開始しない。"""
        with mock.patch("src.utils.updater.secrets.is_dist_build", return_value=False):
            self.updater.start()
        self.assertFalse(self.updater._timer.isActive())


class TestUpdaterInstall(unittest.TestCase):
    """ダウンロード・検証・インストーラ起動（_install）のテスト。"""

    def setUp(self):
        self.updater = Updater()
        self.failed = []
        self.quit_requests = []
        self.updater.update_failed.connect(self.failed.append)
        self.updater.quit_requested.connect(lambda: self.quit_requests.append(True))

        # ダウンロードされるインストーラの中身を偽装
        self.installer_bytes = b"fake-installer-binary"
        self.sha256 = hashlib.sha256(self.installer_bytes).hexdigest()
        self._tmp = tempfile.TemporaryDirectory()

        # テスト用 Ed25519 鍵で偽インストーラを署名（本物の秘密鍵には触れない）
        self.seed_b64, self.pub_b64 = _make_test_keypair()
        self.signature = sign_ed25519(self.seed_b64, self.installer_bytes)

    def tearDown(self):
        self._tmp.cleanup()

    def _fake_install_urlopen(self):
        """_install のダウンロード用 urlopen を偽装する（偽インストーラのバイト列を返す）。"""

        class _Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.close()

        return mock.MagicMock(return_value=_Response(self.installer_bytes))

    def _run_install(self, info, pubkey):
        """指定した _info と公開鍵設定で _install を実行する共通ヘルパ。"""
        self.updater._info = info
        self.updater._installing = True
        with mock.patch("src.utils.updater.UPDATE_PUBLIC_KEY_ED25519", pubkey), \
             mock.patch("urllib.request.urlopen", self._fake_install_urlopen()), \
             mock.patch("subprocess.Popen") as popen, \
             mock.patch("tempfile.gettempdir", return_value=self._tmp.name):
            self.updater._install()
        return popen

    def test_valid_signature_launches_installer_and_quits(self):
        """署名と SHA256 が両方一致すればサイレントインストーラを起動してアプリ終了を要求する。"""
        info = {
            "version": "9.9.9", "url": "http://example/s.exe",
            "sha256": self.sha256, "ed25519": self.signature,
        }
        popen = self._run_install(info, self.pub_b64)

        popen.assert_called_once()
        args = popen.call_args[0][0]
        # サイレント更新フラグ（UAC なし・旧プロセスを閉じる）が付いていること
        self.assertIn("/VERYSILENT", args)
        self.assertIn("/CLOSEAPPLICATIONS", args)
        self.assertEqual(self.quit_requests, [True])
        self.assertEqual(self.failed, [])

    def test_missing_signature_fails_without_launch(self):
        """公開鍵が設定されているのに署名が無ければ起動せず失敗にする。"""
        info = {"version": "9.9.9", "url": "http://example/s.exe", "sha256": self.sha256}
        popen = self._run_install(info, self.pub_b64)

        popen.assert_not_called()
        self.assertEqual(self.quit_requests, [])
        self.assertEqual(len(self.failed), 1)
        self.assertIn("署名", self.failed[0])
        self.assertFalse(self.updater._installing)

    def test_tampered_installer_with_matching_sha256_fails(self):
        """MITM 対策の核心: SHA256 を改竄に合わせても、正規署名が無ければ弾く。

        フィードを掌握した攻撃者は exe と sha256 を整合させられるが、秘密鍵を
        持たないため署名は作れない。別バイト列に対する正規署名を載せても失敗する。
        """
        wrong_sig = sign_ed25519(self.seed_b64, b"malicious-installer")
        info = {
            "version": "9.9.9", "url": "http://example/s.exe",
            "sha256": self.sha256, "ed25519": wrong_sig,
        }
        popen = self._run_install(info, self.pub_b64)

        popen.assert_not_called()
        self.assertEqual(self.quit_requests, [])
        self.assertEqual(len(self.failed), 1)
        self.assertIn("署名", self.failed[0])
        self.assertFalse(self.updater._installing)

    def test_signature_from_other_key_fails(self):
        """別の鍵で署名されていれば（公開鍵不一致）起動しない。"""
        other_seed, _ = _make_test_keypair()
        info = {
            "version": "9.9.9", "url": "http://example/s.exe",
            "sha256": self.sha256, "ed25519": sign_ed25519(other_seed, self.installer_bytes),
        }
        popen = self._run_install(info, self.pub_b64)

        popen.assert_not_called()
        self.assertEqual(len(self.failed), 1)
        self.assertIn("署名", self.failed[0])

    def test_sha256_mismatch_fails_without_launch(self):
        """署名が通っても SHA256 不一致なら起動せず失敗にする（破損検出の補助）。"""
        info = {
            "version": "9.9.9", "url": "http://example/s.exe",
            "sha256": "0" * 64, "ed25519": self.signature,
        }
        popen = self._run_install(info, self.pub_b64)

        popen.assert_not_called()
        self.assertEqual(self.quit_requests, [])
        self.assertEqual(len(self.failed), 1)
        self.assertIn("SHA256", self.failed[0])
        # フラグが戻り、次回のインストール試行が可能なこと
        self.assertFalse(self.updater._installing)

    def test_no_pubkey_falls_back_to_sha256_only(self):
        """公開鍵が未設定（移行期）なら署名検証をスキップし SHA256 のみで起動する。"""
        info = {"version": "9.9.9", "url": "http://example/s.exe", "sha256": self.sha256}
        popen = self._run_install(info, "")

        popen.assert_called_once()
        self.assertEqual(self.quit_requests, [True])
        self.assertEqual(self.failed, [])

    def test_download_and_install_ignores_double_call(self):
        """インストール中の二重呼び出しは無視される。"""
        self.updater._info = {"version": "9.9.9"}
        self.updater._installing = True
        with mock.patch("threading.Thread") as thread:
            self.updater.download_and_install()
        thread.assert_not_called()

    def test_download_and_install_without_info_is_noop(self):
        """update_available 前（_info なし）の呼び出しは何もしない。"""
        with mock.patch("threading.Thread") as thread:
            self.updater.download_and_install()
        thread.assert_not_called()


if __name__ == "__main__":
    unittest.main()
