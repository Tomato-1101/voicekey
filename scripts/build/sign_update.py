"""
Windows 配布物（setup.exe）に Ed25519 署名を付け、version.json に書き込む。

Windows 版のビルドは CI（GitHub Actions）で行うが、署名は秘密鍵が手元にある Mac で
リリース relay 時に行う（秘密鍵を CI に置かない＝Mac 版 Sparkle と同じ運用）。

使い方（CI 成果物を Mac へ download した後）:
    python scripts/build/sign_update.py \
        --exe dist/ci/voicekey-1.0.0-setup.exe \
        --version-json dist/ci/version.json

処理:
  ~/.voicekey/voicekey_update_ed25519 の秘密鍵で exe のバイト列を署名し、署名 base64 を
  version.json の "ed25519" フィールドへ書き込む（既存フィールドは保持）。署名後の
  version.json と exe を voicekey-site/windows/ へ配置して deploy する。
"""

import argparse
import json
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
    parser.add_argument("--version-json", required=True, help="署名を書き込む version.json のパス")
    args = parser.parse_args(argv)

    if not KEY_PATH.exists():
        print(
            f"エラー: 署名秘密鍵がありません: {KEY_PATH}\n"
            "先に python scripts/build/generate_update_key.py で鍵を生成してください。",
            file=sys.stderr,
        )
        return 1
    exe = Path(args.exe)
    vjson = Path(args.version_json)
    if not exe.exists():
        print(f"エラー: exe が見つかりません: {exe}", file=sys.stderr)
        return 1
    if not vjson.exists():
        print(f"エラー: version.json が見つかりません: {vjson}", file=sys.stderr)
        return 1

    seed = KEY_PATH.read_text(encoding="ascii").strip()
    signature = sign_ed25519(seed, exe.read_bytes())

    # BOM 付きで書かれていることがあるため utf-8-sig で読む（アプリ側 updater と揃える）
    info = json.loads(vjson.read_text(encoding="utf-8-sig"))
    info["ed25519"] = signature
    # 末尾改行付き・BOM 無し UTF-8 で書き出す
    vjson.write_text(json.dumps(info, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"==> 署名を書き込みました: {vjson}")
    print(f"    対象 exe   : {exe}")
    print(f"    公開鍵     : {public_key_b64(seed)}")
    print("    （この公開鍵が src/config/constants.py の UPDATE_PUBLIC_KEY_ED25519 と一致すること）")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
