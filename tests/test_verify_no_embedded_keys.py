"""scripts/build/verify_no_embedded_keys.py（配布物の鍵漏洩チェック）のテスト。

セキュリティガード自体が機能することを固定する（検出漏れ＝ガードが無いのと同じ）。
ネットワーク・実鍵には触れない。
"""

import importlib.util
import tempfile
import unittest
from pathlib import Path


def _load_script():
    """検証スクリプトをモジュールとして読み込む（scripts/ はパッケージ外のため）。"""
    path = Path(__file__).resolve().parent.parent / "scripts" / "build" / "verify_no_embedded_keys.py"
    spec = importlib.util.spec_from_file_location("verify_keys", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestVerifyNoEmbeddedKeys(unittest.TestCase):
    def setUp(self):
        self.verify = _load_script()

    def test_clean_file_passes(self):
        """鍵を含まない普通のファイルは 0（合格）。"""
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "clean.txt"
            f.write_text("ただのテキスト。秘密情報は含まない。", encoding="utf-8")
            self.assertEqual(self.verify.main([str(f)]), 0)

    def test_openai_key_detected(self):
        """OpenAI 形式（sk-...）のキーを検出して 1（不合格）。"""
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "leak.txt"
            f.write_text("KEY=sk-abcdefghijklmnopqrstuvwxyz0123456789", encoding="utf-8")
            self.assertEqual(self.verify.main([str(f)]), 1)

    def test_groq_key_detected(self):
        """Groq 形式（gsk_...）のキーを検出して 1。"""
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "leak.txt"
            f.write_text("gsk_abcdefghijklmnopqrstuvwxyz0123456789", encoding="utf-8")
            self.assertEqual(self.verify.main([str(f)]), 1)

    def test_elevenlabs_key_detected(self):
        """ElevenLabs 形式（sk_...）のキーを、バイナリ中の文字列（NUL 区切り）でも検出して 1。"""
        dummy = b"sk_" + b"0123456789abcdef" * 3
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "leak.bin"
            f.write_bytes(b"\x00\x01" + dummy + b"\x00")
            self.assertEqual(self.verify.main([str(f)]), 1)

    def test_gemini_key_detected(self):
        """Gemini 形式（AIza＋35 桁）のキーを検出して 1。"""
        dummy = b"AIza" + b"0123456789abcdefghijklmnopqrstuvw_-"
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "leak.bin"
            f.write_bytes(b"\x00" + dummy + b"\x00")
            self.assertEqual(self.verify.main([str(f)]), 1)

    def test_gemini_prefix_too_short_passes(self):
        """AIza の後ろが 35 桁に満たない文字列は鍵扱いしない（0）。"""
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "clean.txt"
            f.write_text("AIzaShort", encoding="utf-8")
            self.assertEqual(self.verify.main([str(f)]), 0)

    def test_key_inside_app_bundle_resources_detected(self):
        """.app バンドル全体を渡したとき、本体バイナリ以外（Resources 等）に混ざった鍵も検出して 1。

        build_dmg.sh は zip と同じ中身（署名後の .app 全体）を走査する。本体だけを見ていると
        Resources に同梱した設定ファイル等からの漏洩を素通しするため、その回帰を固定する。
        """
        with tempfile.TemporaryDirectory() as tmp:
            app = Path(tmp) / "voicekey.app"
            (app / "Contents" / "MacOS").mkdir(parents=True)
            (app / "Contents" / "Resources").mkdir(parents=True)
            (app / "Contents" / "MacOS" / "voicekey").write_bytes(b"\x00clean\x00")
            (app / "Contents" / "Resources" / "defaults.plist").write_text(
                "<string>gsk_abcdefghijklmnopqrstuvwxyz0123456789</string>", encoding="utf-8"
            )
            self.assertEqual(self.verify.main([str(app)]), 1)

    def test_swift_symbol_containing_sk_underscore_passes(self):
        """識別子の途中に現れる sk_（Swift の translationTask のマングル名）は鍵扱いしない（0）。"""
        symbol = (
            b"_$s7SwiftUI4ViewP013_Translation_aB0E15translationTask_"
            b"6actionQr0D00D7SessionC13ConfigurationVSg_yAIYactFQO\x00"
        )
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "voicekey"
            f.write_bytes(b"\x00" + symbol)
            self.assertEqual(self.verify.main([str(f)]), 0)

    def test_keyless_marker_passes(self):
        """IS_DIST だけのキーレスマーカーは合格（0）。"""
        with tempfile.TemporaryDirectory() as tmp:
            mod = Path(tmp) / "embedded_keys.py"
            mod.write_text("IS_DIST = True\n", encoding="utf-8")
            self.assertEqual(self.verify.main([str(mod)]), 0)

    def test_marker_with_get_key_fails(self):
        """get_key / payload を持つ旧式マーカーは痕跡として 1。"""
        with tempfile.TemporaryDirectory() as tmp:
            mod = Path(tmp) / "embedded_keys.py"
            mod.write_text(
                "IS_DIST = True\n_PAYLOAD = {}\n\ndef get_key(s):\n    return None\n",
                encoding="utf-8",
            )
            self.assertEqual(self.verify.main([str(mod)]), 1)

    def test_swift_marker_with_key_func_fails(self):
        """Swift マーカーに key(forService:) が残っていれば 1。"""
        with tempfile.TemporaryDirectory() as tmp:
            mod = Path(tmp) / "EmbeddedKeys.generated.swift"
            mod.write_text(
                "enum EmbeddedKeys {\n  static let isDist = true\n"
                "  static func key(forService s: String) -> String? { nil }\n}\n",
                encoding="utf-8",
            )
            self.assertEqual(self.verify.main([str(mod)]), 1)

    def test_no_args_returns_usage_error(self):
        """引数なしは使い方エラー（2）。"""
        self.assertEqual(self.verify.main([]), 2)


if __name__ == "__main__":
    unittest.main()
