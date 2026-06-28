"""バージョン定義の一元化チェック（#27）。

`src/config/constants.py` の `APP_VERSION` を単一ソースとし、
- `src/__init__.py` の `__version__`（そこから参照する）
- macOS バンドル（`macos/Resources/Info.plist` の CFBundleShortVersionString）
- README の配布ステータス表
がすべて一致することを保証する。実態（main=1.2 系 / release=1.5 系）はブランチで
異なるため、特定の値ではなく「相互一致」を検証する（どのブランチでも通る）。

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

    def test_macos_info_plist_matches(self):
        """macOS Info.plist の表示バージョンが APP_VERSION と一致する。"""
        with open(_ROOT / "macos" / "Resources" / "Info.plist", "rb") as f:
            plist = plistlib.load(f)
        self.assertEqual(
            plist.get("CFBundleShortVersionString"), self.version,
            "Info.plist の CFBundleShortVersionString が APP_VERSION と不一致",
        )

    def test_readme_status_table_matches(self):
        """README 配布ステータス表（macOS/Windows 行）のバージョンが APP_VERSION と一致する。"""
        rows = [
            ln for ln in _read("README.md").splitlines()
            if ln.startswith("| 🍎") or ln.startswith("| 🪟")
        ]
        self.assertTrue(rows, "README 配布ステータス表の OS 行が見つからない")
        for row in rows:
            m = re.search(r"\*\*v(\d+\.\d+\.\d+)\*\*", row)
            self.assertIsNotNone(m, f"行にバージョン表記が無い: {row[:40]}")
            self.assertEqual(
                m.group(1), self.version,
                f"README のバージョンが APP_VERSION と不一致: {row[:30]}",
            )


if __name__ == "__main__":
    unittest.main()
