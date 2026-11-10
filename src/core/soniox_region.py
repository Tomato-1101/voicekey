"""
Soniox の接続リージョン（米国 / 日本）を API キーから自動判定するモジュール（Mac 版 SonioxRegion.swift の移植）

Soniox はリージョン別のプロジェクトで、API キーもリージョン専用
（米国のキーは日本の窓口で 401、日本のキーは米国の窓口で通らない）。
接続先を固定せず「どちらの窓口でキーが通るか」を無料の REST（モデル一覧）で確かめて決める。
判定結果はキーの指紋ごとに JSON へ覚えるので、問い合わせはキー 1 本につき初回だけ。
キー本体は保存もログ出力もしない（指紋＝SHA256 の先頭 8 バイトだけを使う）。
"""

import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Dict, Optional

import httpx

from ..utils.logger import get_logger
from ..utils.paths import data_dir

logger = get_logger(__name__)

US = "us"
JP = "jp"

# リアルタイム文字起こしの WebSocket 窓口
WEBSOCKET_URLS: Dict[str, str] = {
    US: "wss://stt-rt.soniox.com/transcribe-websocket",
    JP: "wss://stt-rt.jp.soniox.com/transcribe-websocket",
}

# 判定に使う REST（モデル一覧）。課金が発生しない読み取り専用の API
MODELS_URLS: Dict[str, str] = {
    US: "https://api.soniox.com/v1/models",
    JP: "https://api.jp.soniox.com/v1/models",
}

# 判定の問い合わせ 1 本あたりの待ち上限（秒）。初回の録音開始を長く止めないため短くする
PROBE_TIMEOUT_SEC = 2.0

CACHE_FILE_NAME = "soniox_region.json"

_lock = threading.Lock()


def decide(jp_status: Optional[int], us_status: Optional[int]) -> Optional[str]:
    """2 窓口の HTTP ステータスからリージョンを決める（純関数・None は通信失敗）。

    日本を先に見るのは、日本のキーをわざわざ取った人は日本の窓口を使いたいはずだから。
    どちらも 200 でなければ None ＝判定不能（呼び出し側は覚えずに米国へつなぐ）。
    """
    if jp_status == 200:
        return JP
    if us_status == 200:
        return US
    return None


def fingerprint(api_key: str) -> str:
    """キーの指紋（SHA256 の先頭 8 バイトの hex）。キー本体を保存せずに「同じキーか」だけを見分ける。"""
    return hashlib.sha256(api_key.encode("utf-8")).digest()[:8].hex()


def _cache_path(path: Optional[Path]) -> Path:
    return Path(path) if path is not None else data_dir() / CACHE_FILE_NAME


def _load(path: Path) -> Dict[str, str]:
    """覚えている判定結果を読む（壊れていれば空）。"""
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items() if v in (US, JP)}
    except Exception as e:
        logger.warning(f"Soniox の接続先の記憶を読めませんでした（判定し直します）: {e}")
    return {}


def cached(api_key: str, path: Optional[Path] = None) -> Optional[str]:
    """覚えている判定結果（無ければ None）。"""
    with _lock:
        return _load(_cache_path(path)).get(fingerprint(api_key))


def remember(api_key: str, region: str, path: Optional[Path] = None) -> None:
    """判定結果を指紋ごとに保存する（一時ファイル経由で置き換える）。"""
    target = _cache_path(path)
    with _lock:
        data = _load(target)
        data[fingerprint(api_key)] = region
        try:
            tmp = target.with_name(target.name + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(target)
        except Exception as e:
            # 覚えられなくても接続はできる（次の録音で判定し直すだけ）
            logger.warning(f"Soniox の接続先を保存できませんでした: {e}")


def _probe(region: str, api_key: str) -> Optional[int]:
    """1 窓口へモデル一覧を問い合わせ、HTTP ステータスを返す（通信失敗は None）。"""
    try:
        resp = httpx.get(
            MODELS_URLS[region],
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=PROBE_TIMEOUT_SEC,
        )
        return resp.status_code
    except Exception:
        return None


def detect(
    api_key: str,
    path: Optional[Path] = None,
    probe: Callable[[str, str], Optional[int]] = _probe,
) -> Optional[str]:
    """2 窓口へ並列に問い合わせて判定し、判定できたときだけ覚える。

    判定できなかったとき（両方 401・ネットワーク不通・タイムアウト）は覚えずに None を返す
    （一時的な不通で誤ったリージョンを固定しないため。次の録音で再判定される）。

    Args:
        api_key: Soniox のキー
        path: 記憶ファイル（テストで差し替える）
        probe: 問い合わせ関数（テストでモックする）
    """
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="SonioxRegion") as ex:
        jp_future = ex.submit(probe, JP, api_key)
        us_future = ex.submit(probe, US, api_key)
        region = decide(jp_future.result(), us_future.result())
    if region is not None:
        remember(api_key, region, path)
    return region


def websocket_url_for(api_key: str, path: Optional[Path] = None, detector=None) -> str:
    """キーに合った WebSocket の窓口を返す（記憶 → 判定 → 判定できなければ米国）。

    初回だけ判定の往復が接続の前に挟まる（「押下直後が遅い」の原因になりうるのでログに残す）。
    """
    import time

    region = cached(api_key, path)
    if region is not None:
        logger.info(f"Soniox リージョン region={region} cache=hit")
        return WEBSOCKET_URLS[region]
    started = time.monotonic()
    detected = (detector or detect)(api_key, path)
    elapsed_ms = (time.monotonic() - started) * 1000
    region = detected or US
    logger.info(
        f"Soniox リージョン region={region} cache=miss detected={detected is not None} {elapsed_ms:.0f}ms"
    )
    return WEBSOCKET_URLS[region]
