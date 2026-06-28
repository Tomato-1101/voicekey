"""
自動アップデートの Ed25519 署名ユーティリティ（Windows 更新の改竄検証）。

Windows 版の更新は、配布物（setup.exe）を開発者だけが持つ Ed25519 秘密鍵で署名し、
アプリに埋め込んだ固定公開鍵で検証する。これにより version.json / exe のホスティングが
改竄・MITM されても、秘密鍵を持たない第三者は正規の署名を作れず不正な更新を実行できない
（SHA256 はフィードを信頼できる場合の破損検出にすぎず、改竄には無力だった）。

Mac 版は Sparkle の EdDSA（Info.plist の SUPublicEDKey）で同等の検証を既に行っている。
本モジュールはその Windows 版相当で、鍵運用も Sparkle に倣う（秘密鍵は ~/.voicekey/ に
置き、git にコミットしない・表示しない。公開鍵だけをアプリへ埋め込む）。

依存: cryptography（Ed25519PrivateKey/PublicKey）。
"""

import base64

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)


def public_key_b64(private_seed_b64: str) -> str:
    """秘密鍵（32 バイト seed の base64）から対応する公開鍵の base64 を返す。"""
    seed = base64.b64decode(private_seed_b64)
    key = Ed25519PrivateKey.from_private_bytes(seed)
    pub = key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return base64.b64encode(pub).decode("ascii")


def sign_ed25519(private_seed_b64: str, data: bytes) -> str:
    """秘密鍵（32 バイト seed の base64）で data に署名し、署名の base64 を返す。"""
    seed = base64.b64decode(private_seed_b64)
    key = Ed25519PrivateKey.from_private_bytes(seed)
    return base64.b64encode(key.sign(data)).decode("ascii")


def verify_ed25519(public_key_b64: str, signature_b64: str, data: bytes) -> bool:
    """
    固定公開鍵で署名を検証する。検証成功時のみ True。

    どんな失敗（署名不一致・base64 不正・鍵長不正・空文字など）でも例外を投げず False を返す
    （更新の検証関数は絶対に呼び出し側を巻き込まない＝失敗は「信頼しない」に倒す）。
    """
    try:
        pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64))
        pub.verify(base64.b64decode(signature_b64), data)
        return True
    except Exception:
        return False
