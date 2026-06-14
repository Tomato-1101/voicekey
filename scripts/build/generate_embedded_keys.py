"""
配布ビルド用の埋め込みキー生成スクリプト（Windows 版）

.env.dist（リポジトリ直下・git 管理外）または環境変数から API キーを読み、
XOR 難読化した src/config/embedded_keys.py（git 管理外）を生成する。

使い方:
    python scripts/build/generate_embedded_keys.py

注意:
- keyring（Credential Manager / Keychain）は意図的に使わない。
  macOS 上での検証時に実 Keychain の許可ダイアログを誘発する事故を防ぐため。
- .env.dist は Mac 側で `macos/scripts/generate_embedded_keys.sh --export-env` を
  実行すると Keychain から書き出せる（Windows ビルド機へ手動コピーする）。
- 生成された embedded_keys.py は絶対に git にコミットしない（.gitignore 済み）。
"""

import os
import secrets as py_secrets
import sys
from pathlib import Path

# Windows の既定 stdout は cp1252（英語ロケール）で、日本語の進捗 print が
# UnicodeEncodeError になりスクリプトごと落ちる（CI の Windows ランナーで実害）。
# 出力を UTF-8 に固定して、どのロケールでも日本語メッセージを安全に出せるようにする。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

# リポジトリ直下（このファイルは scripts/build/ にある）
ROOT = Path(__file__).resolve().parent.parent.parent
ENV_DIST = ROOT / ".env.dist"
OUT = ROOT / "src" / "config" / "embedded_keys.py"

# サービス識別子 → .env.dist / 環境変数のキー名
SERVICES = {
    "voicekey.OpenAI": "OPENAI_API_KEY",
    "voicekey.Groq": "GROQ_API_KEY",
    "voicekey.ElevenLabs": "ELEVENLABS_API_KEY",
    "voicekey.Deepgram": "DEEPGRAM_API_KEY",
}


def load_keys() -> dict:
    """.env.dist → 環境変数の順でキーを集める（取れたものだけ返す）。"""
    values: dict = {}
    if ENV_DIST.exists():
        for line in ENV_DIST.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, _, value = line.partition("=")
            values[name.strip()] = value.strip().strip('"').strip("'")
    keys = {}
    for service, env_name in SERVICES.items():
        value = values.get(env_name) or os.environ.get(env_name)
        if value:
            keys[service] = value
    return keys


def byte_literal(data: bytes) -> str:
    """バイト列を Python の bytes リテラル文字列にする。"""
    return "bytes([" + ", ".join(str(b) for b in data) + "])"


def main() -> int:
    keys = load_keys()
    if not keys:
        print(
            f"エラー: API キーを 1 件も取得できませんでした。"
            f"{ENV_DIST} を作成するか環境変数を設定してください",
            file=sys.stderr,
        )
        return 1

    # 生成ごとにランダムなマスクで XOR する（strings での平文抽出を防ぐ程度の軽い難読化）
    mask = py_secrets.token_bytes(32)
    entries = []
    for service, value in keys.items():
        raw = value.encode("utf-8")
        enc = bytes(b ^ mask[i % len(mask)] for i, b in enumerate(raw))
        entries.append(f'    "{service}": {byte_literal(enc)},')
    payload = "\n".join(entries)

    OUT.write_text(
        f'''"""
（自動生成）配布ビルド用の埋め込み API キー

git にコミット絶対禁止（.gitignore 済み）。手で編集しない。
生成元: scripts/build/generate_embedded_keys.py
"""

# 配布（DIST）ビルドかどうか。設定画面の API キータブ表示や自動アップデートの判定に使う
IS_DIST = True

# XOR マスク（生成ごとにランダム）
_MASK = {byte_literal(mask)}

# サービス識別子 → XOR 済みキーバイト列
_PAYLOAD = {{
{payload}
}}


def get_key(service):
    """埋め込みキーを復元して返す（未埋め込みのサービスは None）。"""
    data = _PAYLOAD.get(service)
    if data is None:
        return None
    return bytes(b ^ _MASK[i % len(_MASK)] for i, b in enumerate(data)).decode("utf-8")
''',
        encoding="utf-8",
    )
    print(f"==> 生成: {OUT}（埋め込み {len(keys)} 件）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
