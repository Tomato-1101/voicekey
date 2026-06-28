"""Ed25519 署名ユーティリティ（src/utils/update_signing.py）のテスト。

秘密鍵の生成・署名・検証が往復で成立し、改竄や鍵不一致を確実に弾くことを確認する。
本物の配布鍵（~/.voicekey/voicekey_update_ed25519）には触れず、テスト内で鍵を生成する。
"""

import base64
import unittest

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from src.utils.update_signing import public_key_b64, sign_ed25519, verify_ed25519


def _make_keypair():
    """テスト用の (秘密 seed base64, 公開鍵 base64) を返す。"""
    key = Ed25519PrivateKey.generate()
    seed = key.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
    )
    pub = key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return base64.b64encode(seed).decode("ascii"), base64.b64encode(pub).decode("ascii")


class TestUpdateSigning(unittest.TestCase):
    def setUp(self):
        self.seed, self.pub = _make_keypair()
        self.data = b"voicekey-setup-9.9.9.exe contents"

    def test_sign_verify_roundtrip(self):
        """正規の鍵で署名 → 同じ公開鍵で検証が通る。"""
        sig = sign_ed25519(self.seed, self.data)
        self.assertTrue(verify_ed25519(self.pub, sig, self.data))

    def test_public_key_derivation_matches(self):
        """seed から導出した公開鍵が、鍵生成時の公開鍵と一致する。"""
        self.assertEqual(public_key_b64(self.seed), self.pub)

    def test_tampered_data_fails(self):
        """署名対象と 1 バイトでも違うデータは検証に失敗する。"""
        sig = sign_ed25519(self.seed, self.data)
        self.assertFalse(verify_ed25519(self.pub, sig, self.data + b"x"))

    def test_wrong_public_key_fails(self):
        """別の鍵ペアの公開鍵では検証に失敗する。"""
        _, other_pub = _make_keypair()
        sig = sign_ed25519(self.seed, self.data)
        self.assertFalse(verify_ed25519(other_pub, sig, self.data))

    def test_signature_length(self):
        """Ed25519 署名は 64 バイト（base64 デコード後）。"""
        sig = sign_ed25519(self.seed, self.data)
        self.assertEqual(len(base64.b64decode(sig)), 64)

    def test_public_key_length(self):
        """Ed25519 公開鍵は 32 バイト（base64 デコード後）。"""
        self.assertEqual(len(base64.b64decode(self.pub)), 32)

    def test_invalid_base64_signature_returns_false(self):
        """壊れた base64 の署名でも例外を投げず False を返す。"""
        self.assertFalse(verify_ed25519(self.pub, "not-valid-base64!!!", self.data))

    def test_invalid_base64_pubkey_returns_false(self):
        """壊れた公開鍵でも例外を投げず False を返す。"""
        sig = sign_ed25519(self.seed, self.data)
        self.assertFalse(verify_ed25519("@@@bad@@@", sig, self.data))

    def test_empty_signature_returns_false(self):
        """空の署名は False（更新を実行させない）。"""
        self.assertFalse(verify_ed25519(self.pub, "", self.data))

    def test_wrong_length_signature_returns_false(self):
        """64 バイトでない署名（base64 としては有効）は False。"""
        short = base64.b64encode(b"too-short").decode("ascii")
        self.assertFalse(verify_ed25519(self.pub, short, self.data))


if __name__ == "__main__":
    unittest.main()
