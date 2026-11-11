"""
Windows 配布物（setup.exe）に Ed25519 署名を付け、署名ファイル（<exe>.sig）を書き出す。

Windows 版のビルドは CI（GitHub Actions）で行うが、署名は秘密鍵が手元にある Mac で
行う（秘密鍵を CI に置かない＝Mac 版 Sparkle と同じ運用）。

使い方（CI が GitHub Release v<版> へ上げた setup.exe を Mac へ download した後）:
    gh release download v1.0.0 -p voicekey-1.0.0-windows-setup.exe -D dist/ci/
    python scripts/build/sign_update.py --exe dist/ci/voicekey-1.0.0-windows-setup.exe
    gh release upload v1.0.0 dist/ci/voicekey-1.0.0-windows-setup.exe.sig

処理:
  ~/.voicekey/voicekey_update_ed25519 の秘密鍵で exe のバイト列を署名し、署名 base64 を
  1 行だけ <exe>.sig へ書く。アプリ側（src/utils/updater.py）は Latest に exe と .sig が
  両方そろうまで更新を知らせない。
"""

import argparse
import sys
from pathlib import Path

# scripts/build/ から src/ を import できるようにリポジトリ直下を sys.path へ
ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from src.utils.update_signing import public_key_b64, sign_ed25519  # noqa: E402

KEY_PATH = Path.home() / ".voicekey" / "voicekey_update_ed25519"


def main(argv) -> int:
    parser = argparse.ArgumentParser(description="Windows 配布物に Ed25519 署名を付ける")
    parser.add_argument("--exe", required=True, help="署名する setup.exe のパス")
    args = parser.parse_args(argv)

    if not KEY_PATH.exists():
        print(
            f"エラー: 署名秘密鍵がありません: {KEY_PATH}\n"
            "先に python scripts/build/generate_update_key.py で鍵を生成してください。",
            file=sys.stderr,
        )
        return 1
    exe = Path(args.exe)
    if not exe.exists():
        print(f"エラー: exe が見つかりません: {exe}", file=sys.stderr)
        return 1

    seed = KEY_PATH.read_text(encoding="ascii").strip()
    signature = sign_ed25519(seed, exe.read_bytes())

    sig_path = exe.with_name(exe.name + ".sig")
    # 末尾改行付き・BOM 無しで書く（アプリ側は前後の空白を落として読む）
    sig_path.write_text(signature + "\n", encoding="ascii")

    print(f"==> 署名を書き出しました: {sig_path}")
    print(f"    対象 exe   : {exe}")
    print(f"    公開鍵     : {public_key_b64(seed)}")
    print("    （この公開鍵が src/config/constants.py の UPDATE_PUBLIC_KEY_ED25519 と一致すること）")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
