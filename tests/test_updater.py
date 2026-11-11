"""自動アップデータ（src/utils/updater.py）のロジックテスト。

ネットワーク・サブプロセスは全てモックし、実際の DL / インストーラ起動は行わない。
Qt のイベントループは使わず、シグナルの直接接続（同期発火）で検証する。
"""

import sys
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
from src.utils.updater import Updater, parse_version, windows_asset_name


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


class _Response(io.BytesIO):
    """urlopen の with 文で使える偽の HTTP 応答。"""

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def _fake_urlopen(payload: dict):
    """releases/latest の HTTP 応答を偽装する urlopen のモックを返す。"""
    body = json.dumps(payload).encode("utf-8")
    return mock.MagicMock(return_value=_Response(body))


def _release(tag: str, asset_names=()):
    """GitHub の releases/latest 応答（必要な項目だけ）を作る。"""
    return {
        "tag_name": tag,
        "assets": [
            {
                "name": n,
                "size": 123,
                "digest": "sha256:" + "a" * 64,
                "browser_download_url": f"https://github.com/x/y/releases/download/{tag}/{n}",
            }
            for n in asset_names
        ],
    }


class TestUpdaterCheck(unittest.TestCase):
    """バージョンチェック（_check）のテスト。"""

    def setUp(self):
        self.updater = Updater()
        self.available = []
        self.up_to_date = []
        self.updater.update_available.connect(self.available.append)
        self.updater.up_to_date.connect(lambda: self.up_to_date.append(True))

    def test_asset_name_rule(self):
        """Windows 用資産名は voicekey-<版>-windows-setup.exe。"""
        self.assertEqual(windows_asset_name("2.4.0"), "voicekey-2.4.0-windows-setup.exe")

    def test_request_goes_to_github_latest(self):
        """取得先は本リポジトリの releases/latest（User-Agent 付き）。"""
        opener = _fake_urlopen(_release("v1.0.0"))
        with mock.patch("src.utils.updater.APP_VERSION", "1.0.0"), \
             mock.patch("urllib.request.urlopen", opener):
            self.updater._check()
        req = opener.call_args[0][0]
        self.assertEqual(
            req.full_url, "https://api.github.com/repos/Tomato-1101/voicekey/releases/latest"
        )
        self.assertTrue(req.get_header("User-agent"))

    def test_newer_version_with_assets_emits_signal(self):
        """新しい版で exe と署名の資産が揃っていれば update_available が発火する。"""
        name = windows_asset_name("99.0.0")
        info = _release("v99.0.0", [name, name + ".sig", "voicekey-99.0.0.zip"])
        with mock.patch("urllib.request.urlopen", _fake_urlopen(info)):
            self.updater._check()
        self.assertEqual(self.available, ["99.0.0"])
        self.assertTrue(self.updater._info["url"].endswith(name))
        self.assertTrue(self.updater._info["signature_url"].endswith(name + ".sig"))
        self.assertEqual(self.updater._info["size"], 123)
        self.assertEqual(self.updater._info["digest"], "sha256:" + "a" * 64)

    def test_mac_only_release_is_no_update(self):
        """Windows 用資産が無い Latest（Mac だけのリリース）は更新なしとして黙って終える。"""
        info = _release("v99.0.0", ["voicekey-99.0.0.zip", "appcast.xml"])
        with mock.patch("urllib.request.urlopen", _fake_urlopen(info)):
            self.updater._check(manual=True)
        self.assertEqual(self.available, [])
        self.assertIsNone(self.updater._info)
        self.assertEqual(self.up_to_date, [True])

    def test_missing_signature_asset_is_no_update(self):
        """exe はあっても署名がまだ足されていなければ更新扱いにしない。"""
        info = _release("v99.0.0", [windows_asset_name("99.0.0")])
        with mock.patch("urllib.request.urlopen", _fake_urlopen(info)):
            self.updater._check()
        self.assertEqual(self.available, [])
        self.assertIsNone(self.updater._info)

    def test_same_version_no_signal(self):
        """現行と同じバージョンでは何も起きない。"""
        name = windows_asset_name("1.0.0")
        with mock.patch("src.utils.updater.APP_VERSION", "1.0.0"), \
             mock.patch("urllib.request.urlopen", _fake_urlopen(_release("v1.0.0", [name, name + ".sig"]))):
            self.updater._check()
        self.assertEqual(self.available, [])
        self.assertIsNone(self.updater._info)

    def test_older_version_no_signal(self):
        """Latest が古い（ロールバック）場合は無視する。"""
        name = windows_asset_name("1.0.0")
        with mock.patch("src.utils.updater.APP_VERSION", "2.0.0"), \
             mock.patch("urllib.request.urlopen", _fake_urlopen(_release("v1.0.0", [name, name + ".sig"]))):
            self.updater._check()
        self.assertEqual(self.available, [])

    def test_network_error_is_swallowed(self):
        """ネットワーク断では例外を投げず、シグナルも出さない（次回チェックに任せる）。"""
        with mock.patch("urllib.request.urlopen", side_effect=OSError("offline")):
            self.updater._check()  # 例外が漏れないこと
        self.assertEqual(self.available, [])

    def test_network_error_manual_reports_failure(self):
        """手動チェックの失敗は update_failed で知らせる。"""
        failed = []
        self.updater.update_failed.connect(failed.append)
        with mock.patch("urllib.request.urlopen", side_effect=OSError("offline")):
            self.updater._check(manual=True)
        self.assertEqual(len(failed), 1)

    def test_start_noop_in_dev_build(self):
        """ソースから実行しているとき（sys.frozen が無い）は start() が定期チェックを開始しない。"""
        self.assertFalse(getattr(sys, "frozen", False))
        self.updater.start()
        self.assertFalse(self.updater._timer.isActive())


class TestUpdaterInstall(unittest.TestCase):
    """ダウンロード・検証・インストーラ起動（_install）のテスト。"""

    EXE_URL = "https://github.com/x/y/releases/download/v9.9.9/voicekey-9.9.9-windows-setup.exe"
    SIG_URL = EXE_URL + ".sig"

    def setUp(self):
        self.updater = Updater()
        self.failed = []
        self.quit_requests = []
        self.updater.update_failed.connect(self.failed.append)
        self.updater.quit_requested.connect(lambda: self.quit_requests.append(True))

        # ダウンロードされるインストーラの中身を偽装
        self.installer_bytes = b"fake-installer-binary"
        self.digest = "sha256:" + hashlib.sha256(self.installer_bytes).hexdigest()
        self._tmp = tempfile.TemporaryDirectory()

        # テスト用 Ed25519 鍵で偽インストーラを署名（本物の秘密鍵には触れない）
        self.seed_b64, self.pub_b64 = _make_test_keypair()
        self.signature = sign_ed25519(self.seed_b64, self.installer_bytes)

    def tearDown(self):
        self._tmp.cleanup()

    def _info(self, **overrides):
        """_check が作る _info と同じ形の辞書を作る。"""
        info = {
            "version": "9.9.9", "url": self.EXE_URL, "size": len(self.installer_bytes),
            "digest": self.digest, "signature_url": self.SIG_URL,
        }
        info.update(overrides)
        return info

    def _run_install(self, info, pubkey, signature=None):
        """指定した _info・公開鍵・署名ファイルの中身で _install を実行する共通ヘルパ。"""
        sig_text = self.signature if signature is None else signature

        def _urlopen(req, timeout=None):
            # exe と署名ファイルを URL で出し分ける（実通信はしない）
            if req.full_url == self.SIG_URL:
                return _Response((sig_text + "\n").encode("ascii"))
            return _Response(self.installer_bytes)

        self.updater._info = info
        self.updater._installing = True
        with mock.patch("src.utils.updater.UPDATE_PUBLIC_KEY_ED25519", pubkey), \
             mock.patch("urllib.request.urlopen", side_effect=_urlopen), \
             mock.patch("subprocess.Popen") as popen, \
             mock.patch("tempfile.gettempdir", return_value=self._tmp.name):
            self.updater._install()
        return popen

    def _assert_failed(self, popen, word):
        """起動せず失敗を通知し、再試行できる状態に戻っていることを確かめる。"""
        popen.assert_not_called()
        self.assertEqual(self.quit_requests, [])
        self.assertEqual(len(self.failed), 1)
        self.assertIn(word, self.failed[0])
        self.assertFalse(self.updater._installing)

    def test_valid_signature_launches_installer_and_quits(self):
        """サイズ・署名・SHA256 が全て一致すればサイレントインストーラを起動してアプリ終了を要求する。"""
        popen = self._run_install(self._info(), self.pub_b64)

        popen.assert_called_once()
        args = popen.call_args[0][0]
        # サイレント更新フラグ（UAC なし・旧プロセスを閉じる）が付いていること
        self.assertIn("/VERYSILENT", args)
        self.assertIn("/CLOSEAPPLICATIONS", args)
        self.assertEqual(self.quit_requests, [True])
        self.assertEqual(self.failed, [])

    def test_missing_signature_fails_without_launch(self):
        """公開鍵が設定されているのに署名ファイルが無ければ起動せず失敗にする。"""
        popen = self._run_install(self._info(signature_url=None), self.pub_b64)
        self._assert_failed(popen, "署名")

    def test_tampered_installer_with_matching_sha256_fails(self):
        """MITM 対策の核心: digest を改竄に合わせても、正規署名が無ければ弾く。

        リリースを掌握した攻撃者は exe と digest を整合させられるが、秘密鍵を
        持たないため署名は作れない。別バイト列に対する正規署名を載せても失敗する。
        """
        wrong_sig = sign_ed25519(self.seed_b64, b"malicious-installer")
        popen = self._run_install(self._info(), self.pub_b64, signature=wrong_sig)
        self._assert_failed(popen, "署名")

    def test_signature_from_other_key_fails(self):
        """別の鍵で署名されていれば（公開鍵不一致）起動しない。"""
        other_seed, _ = _make_test_keypair()
        popen = self._run_install(
            self._info(), self.pub_b64, signature=sign_ed25519(other_seed, self.installer_bytes)
        )
        self._assert_failed(popen, "署名")

    def test_sha256_mismatch_fails_without_launch(self):
        """署名が通っても digest 不一致なら起動せず失敗にする（破損検出の補助）。"""
        popen = self._run_install(self._info(digest="sha256:" + "0" * 64), self.pub_b64)
        self._assert_failed(popen, "SHA256")

    def test_size_mismatch_fails_without_launch(self):
        """途中で切れたダウンロード（サイズ不一致）は起動しない。"""
        popen = self._run_install(self._info(size=len(self.installer_bytes) + 1), self.pub_b64)
        self._assert_failed(popen, "サイズ")

    def test_no_digest_relies_on_signature(self):
        """digest の無いリリースでも、署名が通れば起動する。"""
        popen = self._run_install(self._info(digest=None), self.pub_b64)
        popen.assert_called_once()
        self.assertEqual(self.failed, [])

    def test_no_pubkey_falls_back_to_sha256_only(self):
        """公開鍵が未設定（移行期）なら署名検証をスキップし SHA256 のみで起動する。"""
        popen = self._run_install(self._info(signature_url=None), "")

        popen.assert_called_once()
        self.assertEqual(self.quit_requests, [True])
        self.assertEqual(self.failed, [])

    def test_no_pubkey_and_no_digest_fails(self):
        """署名も SHA256 も無ければ検証できないのでインストールしない。"""
        popen = self._run_install(self._info(signature_url=None, digest=None), "")
        self._assert_failed(popen, "検証できません")

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
