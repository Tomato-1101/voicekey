"""バージョン定義の一元化チェック（#27）。

Windows は `src/config/constants.py` の `APP_VERSION`、macOS は `macos/Resources/Info.plist` の
CFBundleShortVersionString をそれぞれの単一ソースとし、
- `src/__init__.py` の `__version__`（APP_VERSION を参照する）
- README の配布ステータス表（🪟 行は APP_VERSION、🍎 行は Info.plist）
が一致することを保証する。特定の値ではなく「相互一致」を検証する。

2026-10-01 に Windows の開発を止め、macOS だけを GitHub Releases で配布するようになったため、
両 OS の版は別々に進む（以前は APP_VERSION 1 つで両 OS をそろえていた）。

インストーラー（.iss は AppVersion=0.0.0 プレースホルダを build 時に /DAppVersion で上書き）と
更新フィード（Vercel 配信。リポジトリ内の dist/ci/version.json は CI 生成物）は、
リリース時に APP_VERSION から注入・公開される build/deploy 成果物のためここでは検証しない。
"""

import plistlib
import re
import unittest
from pathlib import Path

_ROOT = Path(__file__).parent.parent


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


class TestVersionConsistency(unittest.TestCase):
    def setUp(self):
        # 単一ソース: constants.APP_VERSION
        m = re.search(r'APP_VERSION:\s*str\s*=\s*"([^"]+)"', _read("src/config/constants.py"))
        self.assertIsNotNone(m, "constants.py に APP_VERSION 定義が見つからない")
        self.version = m.group(1)
        # x.y.z 形式であること
        self.assertRegex(self.version, r"^\d+\.\d+\.\d+$", f"不正なバージョン形式: {self.version}")

    def test_init_references_single_source(self):
        """src/__init__.py は APP_VERSION を参照し、バージョンをハードコードしない。"""
        src = _read("src/__init__.py")
        self.assertIn(
            "from .config.constants import APP_VERSION as __version__", src,
            "__init__.py が APP_VERSION を単一ソースとして参照していない",
        )
        # `__version__ = "x.y.z"` のようなハードコードが残っていないこと
        self.assertNotRegex(
            src, r'__version__\s*=\s*[\'"]',
            "__init__.py にバージョンのハードコードが残っている",
        )

    def _readme_row_version(self, prefix: str) -> str:
        """README 配布ステータス表で prefix から始まる行の `**vX.Y.Z**` を返す。"""
        rows = [ln for ln in _read("README.md").splitlines() if ln.startswith(prefix)]
        self.assertEqual(len(rows), 1, f"README 配布ステータス表の {prefix} 行が 1 行でない")
        m = re.search(r"\*\*v(\d+\.\d+\.\d+)\*\*", rows[0])
        self.assertIsNotNone(m, f"行にバージョン表記が無い: {rows[0][:40]}")
        return m.group(1)

    def test_readme_macos_row_matches_info_plist(self):
        """README の macOS 行のバージョンが Info.plist の CFBundleShortVersionString と一致する。"""
        with open(_ROOT / "macos" / "Resources" / "Info.plist", "rb") as f:
            plist = plistlib.load(f)
        mac_version = plist.get("CFBundleShortVersionString")
        self.assertRegex(mac_version or "", r"^\d+\.\d+\.\d+$", f"不正なバージョン形式: {mac_version}")
        self.assertEqual(
            self._readme_row_version("| 🍎"), mac_version,
            "README の macOS 行が Info.plist の CFBundleShortVersionString と不一致",
        )

    def test_readme_windows_row_matches_app_version(self):
        """README の Windows 行のバージョンが APP_VERSION と一致する。"""
        self.assertEqual(
            self._readme_row_version("| 🪟"), self.version,
            "README の Windows 行が APP_VERSION と不一致",
        )


if __name__ == "__main__":
    unittest.main()
