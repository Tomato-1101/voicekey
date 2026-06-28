"""
自動アップデート署名鍵（Ed25519）の生成（一回限り・Windows 更新用）。

秘密鍵を ~/.voicekey/voicekey_update_ed25519 に base64（32 バイト seed・chmod 600）で保存し、
公開鍵の base64 を標準出力に表示する。表示された公開鍵を
src/config/constants.py の UPDATE_PUBLIC_KEY_ED25519 に貼る。

セキュリティ:
- 秘密鍵は絶対に git にコミットしない・画面に表示しない（このスクリプトは公開鍵のみ表示する）。
- Mac 版 Sparkle の ~/.voicekey/sparkle_eddsa_key と同じ運用（リリース署名はこの Mac でのみ可能）。
- 既存の鍵があれば上書きしない（誤再生成で過去の配布物の検証鍵を失う事故を防ぐ）。
  どうしても作り直す場合のみ --force を付ける。

使い方:
    python scripts/build/generate_update_key.py
"""

import base64
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

KEY_PATH = Path.home() / ".voicekey" / "voicekey_update_ed25519"


def main(argv) -> int:
    force = "--force" in argv
    if KEY_PATH.exists() and not force:
        print(
            f"エラー: 既に鍵が存在します: {KEY_PATH}\n"
            "作り直すと過去の配布物を検証できなくなります。意図的なら --force を付けてください。",
            file=sys.stderr,
        )
        return 1

    key = Ed25519PrivateKey.generate()
    seed = key.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
    )
    pub = key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )

    KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    # 秘密鍵は本人のみ読める権限で保存（chmod 600 相当）。base64 で 44 文字（Sparkle 鍵と同形式）
    KEY_PATH.write_text(base64.b64encode(seed).decode("ascii") + "\n", encoding="ascii")
    KEY_PATH.chmod(0o600)

    print(f"==> 秘密鍵を保存しました（コミット禁止・表示しません）: {KEY_PATH}")
    print("==> 次の公開鍵を src/config/constants.py の UPDATE_PUBLIC_KEY_ED25519 に貼ってください:")
    print(base64.b64encode(pub).decode("ascii"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
