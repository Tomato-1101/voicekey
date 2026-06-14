"""自動アップデータ（src/utils/updater.py）のロジックテスト。

ネットワーク・サブプロセスは全てモックし、実際の DL / インストーラ起動は行わない。
Qt のイベントループは使わず、シグナルの直接接続（同期発火）で検証する。
"""

import hashlib
import io
import json
import tempfile
import unittest
from unittest import mock

from src.utils.updater import Updater, parse_version


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

    def test_valid_sha256_launches_installer_and_quits(self):
        """SHA256 が一致すればサイレントインストーラを起動してアプリ終了を要求する。"""
        self.updater._info = {"version": "9.9.9", "url": "http://example/s.exe", "sha256": self.sha256}
        with mock.patch("urllib.request.urlopen", self._fake_install_urlopen()), \
             mock.patch("subprocess.Popen") as popen, \
             mock.patch("tempfile.gettempdir", return_value=self._tmp.name):
            self.updater._install()

        popen.assert_called_once()
        args = popen.call_args[0][0]
        # サイレント更新フラグ（UAC なし・旧プロセスを閉じる）が付いていること
        self.assertIn("/VERYSILENT", args)
        self.assertIn("/CLOSEAPPLICATIONS", args)
        self.assertEqual(self.quit_requests, [True])
        self.assertEqual(self.failed, [])

    def test_sha256_mismatch_fails_without_launch(self):
        """SHA256 不一致なら起動せず update_failed を出し、再試行可能な状態に戻る。"""
        self.updater._info = {"version": "9.9.9", "url": "http://example/s.exe", "sha256": "0" * 64}
        self.updater._installing = True  # download_and_install 経由のフラグを再現
        with mock.patch("urllib.request.urlopen", self._fake_install_urlopen()), \
             mock.patch("subprocess.Popen") as popen, \
             mock.patch("tempfile.gettempdir", return_value=self._tmp.name):
            self.updater._install()

        popen.assert_not_called()
        self.assertEqual(self.quit_requests, [])
        self.assertEqual(len(self.failed), 1)
        self.assertIn("SHA256", self.failed[0])
        # フラグが戻り、次回のインストール試行が可能なこと
        self.assertFalse(self.updater._installing)

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
