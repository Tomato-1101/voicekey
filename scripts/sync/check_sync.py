"""
sync-worker のデプロイ確認スクリプト（Windows 版）

Cloudflare Worker（sync-worker/）をデプロイした後、5 ステップで疎通を確認する。
トークンは一切出力しない（標準出力・エラー出力どちらにも文字列として現れない）。

使い方:
    python scripts/sync/check_sync.py [<worker-url>]

    <worker-url> を省略すると settings.yaml の history_sync.url を使う。
    トークンは常に src.utils.secrets.get_api_key(SERVICE_SYNC_TOKEN)（Credential Manager）から読む。

確認内容:
  1. GET /health                                     -> 200
  2. GET /history（不正トークン）                      -> 401
  3. GET /history（正規トークン）                      -> 200
  4. POST /history（チェック用 1 件）                  -> 200, accepted==1
  5. GET /history?limit=50（送った id が含まれるか）   -> 200, id を検出

全ステップ成功時のみ終了コード 0。
"""

import sys
import uuid
from datetime import datetime
from pathlib import Path

# Windows の既定 stdout は cp1252（英語ロケール）で、日本語の進捗 print が
# UnicodeEncodeError になりスクリプトごと落ちる（scripts/build/generate_embedded_keys.py と同じ対策）。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

# scripts/sync/ から src/ を import できるようにリポジトリ直下を sys.path へ
ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from src.config.config_manager import ConfigManager  # noqa: E402
from src.utils import secrets  # noqa: E402

_TIMEOUT = httpx.Timeout(10.0, connect=5.0)


def _resolve_url(argv) -> str:
    """引数（argv[0]）優先。無ければ settings.yaml の history_sync.url を使う。"""
    if argv:
        return argv[0].rstrip("/")
    config = ConfigManager()
    url = str((config.get("history_sync", {}) or {}).get("url", "") or "")
    return url.rstrip("/")


def _report(ok: bool, label: str, status: int) -> bool:
    """OK/NG とステータスコードだけを出す（本文やトークンは出力しない）。"""
    mark = "OK" if ok else "NG"
    print(f"[{mark}] {label} (HTTP {status})")
    return ok


def main(argv) -> int:
    url = _resolve_url(argv)
    if not url:
        print(
            "エラー: Worker の URL が未指定です（引数、または settings.yaml の history_sync.url）",
            file=sys.stderr,
        )
        return 2

    token = secrets.get_api_key(secrets.SERVICE_SYNC_TOKEN)
    if not token:
        print("エラー: 同期トークンが未設定です（Credential Manager: voicekey.SyncToken）", file=sys.stderr)
        return 2

    all_ok = True
    try:
        with httpx.Client(timeout=_TIMEOUT) as client:
            all_ok = _run_checks(client, url, token)
    except httpx.HTTPError as e:
        # 接続不能・タイムアウト等（URL 間違い・Worker 未デプロイでよく起きるため個別に握る）
        print(f"エラー: Worker への接続に失敗しました: {e}", file=sys.stderr)
        return 1

    if all_ok:
        print("==> すべて OK（sync-worker は正常に疎通しています）")
        return 0
    print("==> 失敗したステップがあります（上記 NG を確認してください）", file=sys.stderr)
    return 1


def _run_checks(client: httpx.Client, url: str, token: str) -> bool:
    """5 ステップの疎通確認を行う。すべて成功したら True。"""
    all_ok = True
    # 1. GET /health -> 200（認証不要）
    resp = client.get(f"{url}/health")
    all_ok &= _report(resp.status_code == 200, "GET /health", resp.status_code)

    # 2. GET /history（不正トークン） -> 401
    resp = client.get(
        f"{url}/history", headers={"Authorization": "Bearer invalid-token-for-check"}
    )
    all_ok &= _report(resp.status_code == 401, "GET /history（不正トークン）", resp.status_code)

    # 3. GET /history（正規トークン） -> 200
    headers = {"Authorization": f"Bearer {token}"}
    resp = client.get(f"{url}/history", headers=headers)
    all_ok &= _report(resp.status_code == 200, "GET /history（正規トークン）", resp.status_code)

    # 4. POST /history（チェック用 1 件） -> 200, accepted==1
    check_id = f"check-{uuid.uuid4().hex}"
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    payload = {
        "items": [
            {
                "id": check_id,
                "text": "sync check",
                "date": now,
                "device": "windows-check",
                "characters": 10,
            }
        ]
    }
    resp = client.post(f"{url}/history", json=payload, headers=headers)
    accepted_ok = False
    if resp.status_code == 200:
        try:
            accepted_ok = resp.json().get("accepted") == 1
        except Exception:
            accepted_ok = False
    all_ok &= _report(
        resp.status_code == 200 and accepted_ok, "POST /history（1 件）", resp.status_code
    )

    # 5. GET /history?limit=50 -> 送った id が含まれる
    resp = client.get(f"{url}/history", params={"limit": 50}, headers=headers)
    found = False
    if resp.status_code == 200:
        try:
            items = resp.json().get("items", [])
            found = any(it.get("id") == check_id for it in items)
        except Exception:
            found = False
    all_ok &= _report(
        resp.status_code == 200 and found,
        "GET /history?limit=50（送信 id の検出）",
        resp.status_code,
    )

    return all_ok


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
