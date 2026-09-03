"""
履歴同期モジュール（Cloudflare Worker 経由で Mac と履歴を共有）

HistoryStore は「ローカルに履歴を保つこと」だけを知り、通信は一切しない。
本モジュールはその外側で、貼り付け直後に enqueue() された 1 件を送信待ち
（sync_outbox.json）に積み、常駐スレッドがバックグラウンドで Worker へ
POST /history（送信）・GET /history（取得）を行う。

Windows は「personal」ビルド（backend_client / auth_client / is_logged_in() は
一切使わない）と同じ独立性で、この機能も自社バックエンドの認証層に依存しない。
認証は Worker 専用の共有トークン（keyring の voicekey.SyncToken）のみで行う。

貼り付け経路をブロックしないことが最優先のため:
- enqueue() はファイル追記のみで通信しない（HistoryStore.add と同じスレッドから呼ばれる）
- ネットワーク・401・サーバーエラーはすべてこのモジュール内で握り、ログに残すだけにする
"""

import json
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional

import httpx

from ..utils import secrets
from ..utils.logger import get_logger

logger = get_logger(__name__)

OUTBOX_FILE_NAME = "sync_outbox.json"
CACHE_FILE_NAME = "sync_cloud_cache.json"

# サーバーが受理する 1 リクエストあたりの最大件数（sync-worker の POST /history 上限と一致）
_POST_CHUNK = 200
# 受信キャッシュに保持する最大件数
_CACHE_MAX_ITEMS = 200
# バックオフの初期値・上限（秒）。失敗のたびに倍化し、成功でリセットする
_BACKOFF_INITIAL_SEC = 10.0
_BACKOFF_MAX_SEC = 300.0


def _default_base_dir() -> Path:
    """既定の保存先ディレクトリ（settings.yaml / history.json と同じ配置ロジック）。"""
    if getattr(sys, "frozen", False):
        # PyInstaller 実行時は実行ファイルと同じディレクトリ
        return Path(sys.executable).parent
    # 開発時はプロジェクトルート
    return Path(__file__).parent.parent.parent


def _now_iso() -> str:
    """現在時刻を settings.yaml 周辺のファイル群と同じ ISO 8601（秒精度・ローカルタイムゾーン）で返す。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def parse_iso_date(s: str) -> datetime:
    """ISO 8601 文字列を比較可能な datetime に変換する（UI の履歴表示ソートでも共用する）。

    Python 3.12 の fromisoformat は末尾 "Z" も受け付ける。タイムゾーンの無い
    値（旧ローカル保存分）はローカルタイムとみなす。パースできない値は
    ソートで必ず最後尾に来るよう datetime.min（tz 付き）を返す。

    Args:
        s: ISO 8601 形式の日時文字列

    Returns:
        タイムゾーン付きの datetime（比較・ソートに使える）
    """
    try:
        dt = datetime.fromisoformat(str(s))
    except (ValueError, TypeError):
        return datetime.min.replace(tzinfo=timezone.utc)
    if dt.tzinfo is None:
        # ナイーブな datetime はローカルタイムとみなして付与する
        dt = dt.astimezone()
    return dt


class HistorySync:
    """
    履歴を Cloudflare Worker（sync-worker/）経由で Mac と共有するクラス。

    常駐の daemon スレッド 1 本が送信待ち（outbox）のフラッシュと受信キャッシュの
    更新を行う。UI（設定ウィンドウ）はこのクラスの enqueue / request_fetch /
    merged_items / status だけを触ればよく、通信の詳細やスレッドを意識しない。
    """

    DEVICE = "windows"
    # 定期実行の間隔（秒）。enable() 直後の 1 回はこれを待たず即座に走る
    _SYNC_INTERVAL_SEC = 300.0

    def __init__(
        self,
        config_manager,
        base_dir: Optional[Path] = None,
        client: Optional[httpx.Client] = None,
        on_changed: Optional[Callable[[], None]] = None,
    ) -> None:
        """
        履歴同期を初期化し、送信待ち・受信キャッシュを読み込む（通信はまだ行わない）。

        Args:
            config_manager: history_sync.enabled / history_sync.url を読む ConfigManager
            base_dir: sync_outbox.json / sync_cloud_cache.json の保存先（テスト用。省略時は既定パス）
            client: 差し替え用の httpx.Client（テストで MockTransport を注入する）
            on_changed: 受信キャッシュが変化したときに呼ぶコールバック（UI の再描画用）
        """
        self._config_manager = config_manager
        base = Path(base_dir) if base_dir else _default_base_dir()
        self._outbox_path = base / OUTBOX_FILE_NAME
        self._cache_path = base / CACHE_FILE_NAME
        self._client: Optional[httpx.Client] = client
        self._on_changed = on_changed

        self._lock = threading.Lock()
        self._wake_event = threading.Event()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

        self._fetch_requested = False
        self._token_invalid = False
        self._in_error_streak = False  # 連続失敗の何回目か（初回だけ WARNING、以降は DEBUG）
        self._backoff_sec = 0.0
        self._next_allowed_at = 0.0  # time.monotonic() 基準。0 = 制限なし
        self._last_sync: Optional[str] = None
        self._last_error: Optional[str] = None

        self._cfg_enabled = False
        self._cfg_url = ""
        self._load_config()

        self._outbox: List[dict] = self._load_outbox()
        self._cache: dict = self._load_cache()

    # ------------------------------------------------------------------
    # 公開 API
    # ------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        """設定で有効化されており、URL も設定済みか。"""
        with self._lock:
            return bool(self._cfg_enabled and self._cfg_url)

    def enable(self) -> None:
        """常駐ワーカースレッドを起動する（app.py からのみ呼ぶ想定・冪等）。

        設定で無効でもスレッド自体は起動する（_run_cycle が毎サイクル enabled を見て
        何もせず待つだけになる）。これで apply_config() による後からの有効化に
        スレッド再生成なしで追従できる。
        """
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._fetch_requested = True  # 起動直後に一度だけ取得してキャッシュを温める
            thread = threading.Thread(target=self._worker_loop, daemon=True, name="HistorySync")
            self._thread = thread
        thread.start()
        self._wake_event.set()  # 最初のサイクルを 300 秒待たせない

    def stop(self, timeout: float = 2.0) -> None:
        """ワーカースレッドを停止する（アプリ終了時に呼ぶ）。"""
        self._stop_event.set()
        self._wake_event.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)

    def apply_config(self) -> None:
        """設定を再読み込みし、401 による停止を解除して、ワーカーを起こす。"""
        self._load_config()
        with self._lock:
            self._token_invalid = False
            self._in_error_streak = False
        self._wake_event.set()

    def enqueue(self, entry: dict) -> None:
        """HistoryStore.on_add から呼ばれる。送信待ちに追記して即座に返る（通信しない）。

        Args:
            entry: {"id", "text", "date", "device"} を含む履歴エントリのコピー
        """
        entry_id = str(entry.get("id") or "")
        if not entry_id:
            return  # id の無いエントリは送信できない（サーバー側の重複排除キー）
        item = {
            "id": entry_id,
            "text": entry.get("text", ""),
            "date": entry.get("date", ""),
            "device": entry.get("device", self.DEVICE),
        }
        with self._lock:
            self._outbox.append(item)
            self._save_outbox_locked()
        self._wake_event.set()

    def request_fetch(self) -> None:
        """次のサイクルで GET /history を必ず行うよう頼む（履歴パネルを開いたとき等）。

        無効設定のときは何もしない（無駄なスレッド起床をしない）。
        """
        if not self.enabled:
            return
        with self._lock:
            self._fetch_requested = True
        self._wake_event.set()

    def merged_items(self, local_items: List[dict]) -> List[dict]:
        """ローカル履歴と受信キャッシュを id でまとめ、日付降順で返す（表示用）。

        同じ id はローカル側を優先する（ローカルは確定直後の最新表現のため）。

        Args:
            local_items: HistoryStore.items() の結果（送信前のものを含んでよい）

        Returns:
            {"id","text","date","device"} に app_name/characters を足した辞書のリスト（最大 200 件）
        """
        with self._lock:
            cache_items = list(self._cache.get("items", []))
        combined: Dict[str, dict] = {}
        for it in cache_items:
            iid = it.get("id")
            if iid:
                combined[str(iid)] = self._normalize_item(it)
        for it in local_items:  # 後勝ちでローカルが上書きする
            iid = it.get("id")
            if iid:
                combined[str(iid)] = self._normalize_item(it)
        merged = sorted(
            combined.values(), key=lambda e: parse_iso_date(e.get("date", "")), reverse=True
        )
        return merged[:_CACHE_MAX_ITEMS]

    def status(self) -> dict:
        """設定画面の「履歴」タブ向けの状態を返す。"""
        with self._lock:
            pending = len(self._outbox)
            token_invalid = self._token_invalid
            last_sync = self._last_sync
            last_error = self._last_error
            enabled_cfg = self._cfg_enabled
            url = self._cfg_url
        token = secrets.get_api_key(secrets.SERVICE_SYNC_TOKEN)
        return {
            "enabled": bool(enabled_cfg and url),
            "configured": bool(url) and bool(token),
            "pending": pending,
            "last_sync": last_sync,
            "token_invalid": token_invalid,
            "last_error": last_error,
        }

    # ------------------------------------------------------------------
    # 設定
    # ------------------------------------------------------------------

    def _load_config(self) -> None:
        """config_manager から history_sync 設定を読み直す。"""
        raw = self._config_manager.get("history_sync", {}) or {}
        enabled = bool(raw.get("enabled", False))
        url = str(raw.get("url", "") or "").rstrip("/")
        with self._lock:
            self._cfg_enabled = enabled
            self._cfg_url = url

    # ------------------------------------------------------------------
    # ワーカースレッド
    # ------------------------------------------------------------------

    def _worker_loop(self) -> None:
        """定期実行 + 起床で 1 サイクルずつ回す常駐ループ。"""
        while not self._stop_event.is_set():
            timed_out = not self._wake_event.wait(timeout=self._SYNC_INTERVAL_SEC)
            self._wake_event.clear()
            if self._stop_event.is_set():
                break
            try:
                self._run_cycle(periodic=timed_out)
            except Exception as e:  # 想定外の例外で常駐ループを止めない
                logger.debug(f"履歴同期サイクルで予期しない例外（無視）: {e}")

    def _run_cycle(self, periodic: bool = False) -> None:
        """送信・受信を 1 回ぶん行う（テストからも直接呼べるよう分離）。

        Args:
            periodic: 300 秒の定期実行による起動なら True。この場合は送信の有無に
                関わらず GET も行う（Mac 側で増えた履歴を定期的に拾うため）。
        """
        with self._lock:
            cfg_enabled, url, token_invalid = self._cfg_enabled, self._cfg_url, self._token_invalid
        if not (cfg_enabled and url):
            return
        if token_invalid:
            return  # apply_config() が呼ばれるまで一切通信しない
        if time.monotonic() < self._next_allowed_at:
            return  # バックオフ中

        token = secrets.get_api_key(secrets.SERVICE_SYNC_TOKEN)
        if not token:
            return  # 未設定（configured=False）。UI 側の案内に任せて静かに待つ

        fetch_requested = self._consume_fetch_requested()
        sent = self._flush_outbox(token, url)

        do_fetch = periodic or fetch_requested or sent > 0
        received = 0
        if do_fetch:
            with self._lock:
                blocked = self._token_invalid or time.monotonic() < self._next_allowed_at
            if not blocked:
                received = self._fetch_and_merge(token, url)

        if sent or received:
            logger.info(f"履歴同期完了 (送信 {sent} 件, 受信 {received} 件)")
        else:
            logger.debug("履歴同期完了 (送信 0 件, 受信 0 件)")

    def _consume_fetch_requested(self) -> bool:
        with self._lock:
            v = self._fetch_requested
            self._fetch_requested = False
            return v

    # ------------------------------------------------------------------
    # 送信（POST /history）
    # ------------------------------------------------------------------

    def _flush_outbox(self, token: str, url: str) -> int:
        """送信待ちを ≤200 件ずつ POST する。失敗したチャンクで打ち切る（順序を保つため）。"""
        with self._lock:
            pending = list(self._outbox)
        if not pending:
            return 0

        client = self._get_client()
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        total_sent = 0
        for i in range(0, len(pending), _POST_CHUNK):
            chunk = pending[i : i + _POST_CHUNK]
            payload = {"items": [self._to_post_item(e) for e in chunk]}
            logger.info(f"履歴同期要求 (送信 {len(chunk)} 件)")
            try:
                resp = client.post(f"{url}/history", json=payload, headers=headers)
            except httpx.HTTPError as e:
                self._last_error = str(e)
                self._log_failure(f"送信に失敗しました: {e}")
                self._record_failure()
                break
            if resp.status_code == 401:
                self._handle_token_invalid()
                break
            if resp.status_code == 503:
                self._handle_maybe_misconfigured(resp)
                break
            if resp.status_code // 100 != 2:
                self._last_error = f"HTTP {resp.status_code}"
                self._log_failure(f"送信でエラー (HTTP {resp.status_code})")
                self._record_failure()
                break

            sent_ids = {e["id"] for e in chunk}
            with self._lock:
                self._outbox = [e for e in self._outbox if e.get("id") not in sent_ids]
                self._save_outbox_locked()
            total_sent += len(chunk)
            self._record_success()
            self._last_sync = _now_iso()
        return total_sent

    @staticmethod
    def _to_post_item(entry: dict) -> dict:
        """outbox の 1 件をサーバー契約の形へ整える（characters/app_name の既定値を補う）。"""
        item = dict(entry)
        item.setdefault("app_name", None)
        item.setdefault("characters", len(str(entry.get("text", ""))))
        return item

    # ------------------------------------------------------------------
    # 受信（GET /history）
    # ------------------------------------------------------------------

    def _fetch_and_merge(self, token: str, url: str) -> int:
        """カーソル以降の履歴を取得し、受信キャッシュへマージする。取得件数を返す。"""
        with self._lock:
            cursor = self._cache.get("cursor") or ""
        params: Dict[str, object] = {"limit": _CACHE_MAX_ITEMS}
        if cursor:
            params["since"] = cursor

        client = self._get_client()
        try:
            resp = client.get(f"{url}/history", params=params, headers={"Authorization": f"Bearer {token}"})
        except httpx.HTTPError as e:
            self._last_error = str(e)
            self._log_failure(f"取得に失敗しました: {e}")
            self._record_failure()
            return 0
        if resp.status_code == 401:
            self._handle_token_invalid()
            return 0
        if resp.status_code != 200:
            self._last_error = f"HTTP {resp.status_code}"
            self._log_failure(f"取得でエラー (HTTP {resp.status_code})")
            self._record_failure()
            return 0
        try:
            items = resp.json().get("items", [])
        except Exception as e:
            self._last_error = str(e)
            self._log_failure(f"取得結果の解析に失敗しました: {e}")
            self._record_failure()
            return 0

        self._record_success()
        self._last_sync = _now_iso()
        if self._merge_cache(items) and self._on_changed is not None:
            try:
                self._on_changed()
            except Exception as e:  # UI 側コールバックの失敗で同期ループを止めない
                logger.warning(f"履歴同期: 変更通知コールバックに失敗（無視）: {e}")
        return len(items)

    def _merge_cache(self, items: list) -> bool:
        """受信した items を id でキャッシュへマージし、200 件に切り詰めてカーソルを更新する。

        Returns:
            キャッシュの中身が変化したか（呼び出し側が on_changed を呼ぶか判定するため）
        """
        if not items:
            return False
        with self._lock:
            before = list(self._cache.get("items", []))
            existing = {e.get("id"): e for e in before if e.get("id")}
            cursor = self._cache.get("cursor") or ""
            for it in items:
                iid = it.get("id")
                if not iid:
                    continue
                existing[iid] = it
                recv = str(it.get("received_at") or "")
                if recv > cursor:
                    cursor = recv
            merged = sorted(
                existing.values(), key=lambda e: parse_iso_date(e.get("date", "")), reverse=True
            )[:_CACHE_MAX_ITEMS]
            self._cache["items"] = merged
            self._cache["cursor"] = cursor
            self._save_cache_locked()
        return merged != before

    @staticmethod
    def _normalize_item(it: dict) -> dict:
        """merged_items 用に必須キーへ揃える（app_name/characters は値がある場合だけ含める）。"""
        out = {
            "id": str(it.get("id", "")),
            "text": str(it.get("text", "")),
            "date": str(it.get("date", "")),
            "device": str(it.get("device", "")),
        }
        if it.get("app_name") is not None:
            out["app_name"] = it["app_name"]
        if it.get("characters") is not None:
            out["characters"] = it["characters"]
        return out

    # ------------------------------------------------------------------
    # エラー処理・バックオフ
    # ------------------------------------------------------------------

    def _handle_token_invalid(self) -> None:
        """401: トークン無効。apply_config() されるまで通信を止める（警告は最初の 1 回だけ）。"""
        with self._lock:
            already = self._token_invalid
            self._token_invalid = True
        self._last_error = "invalid_token"
        if not already:
            logger.warning("履歴同期: トークンが無効です（設定の「履歴」で確認してください）")

    def _handle_maybe_misconfigured(self, resp: httpx.Response) -> None:
        """503: サーバー側にトークン未設定（token_not_configured）ならその旨を、それ以外は通常のエラーとして扱う。"""
        try:
            body = resp.json()
        except Exception:
            body = {}
        if body.get("error") == "token_not_configured":
            self._last_error = "token_not_configured"
            self._log_failure("サーバー側でトークンが未設定です（sync-worker の設定を確認してください）")
        else:
            self._last_error = f"HTTP {resp.status_code}"
            self._log_failure(f"送信でエラー (HTTP {resp.status_code})")
        self._record_failure()

    def _log_failure(self, message: str) -> None:
        """連続失敗の最初の 1 回だけ WARNING、以降は DEBUG（ログの洪水を防ぐ）。"""
        if not self._in_error_streak:
            logger.warning(f"履歴同期: {message}")
            self._in_error_streak = True
        else:
            logger.debug(f"履歴同期: {message}")

    def _record_failure(self) -> None:
        """バックオフを 10s→20s→…→300s と倍化し、次に通信を試せる時刻を記録する。"""
        with self._lock:
            self._backoff_sec = min(
                _BACKOFF_MAX_SEC, self._backoff_sec * 2 if self._backoff_sec else _BACKOFF_INITIAL_SEC
            )
            self._next_allowed_at = time.monotonic() + self._backoff_sec

    def _record_success(self) -> None:
        """成功したらバックオフと連続失敗カウントをリセットする。"""
        with self._lock:
            self._backoff_sec = 0.0
            self._next_allowed_at = 0.0
        self._in_error_streak = False

    # ------------------------------------------------------------------
    # 通信クライアント
    # ------------------------------------------------------------------

    def _get_client(self) -> httpx.Client:
        """共有 httpx.Client を取得または生成する（テストでは注入したものを使う）。"""
        if self._client is None:
            self._client = httpx.Client(
                timeout=httpx.Timeout(10.0, connect=5.0),
                transport=httpx.HTTPTransport(retries=1),
            )
        return self._client

    # ------------------------------------------------------------------
    # ファイル永続化（history.py と同じ tmp+replace パターン）
    # ------------------------------------------------------------------

    def _load_outbox(self) -> List[dict]:
        """送信待ちファイルを読み込む。壊れていれば空で開始する（クラッシュさせない）。"""
        try:
            if self._outbox_path.exists():
                data = json.loads(self._outbox_path.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    return [e for e in data if isinstance(e, dict) and e.get("id")]
        except Exception as e:
            logger.warning(f"履歴同期: 送信待ちファイルの読み込みに失敗（空で開始します）: {e}")
        return []

    def _save_outbox_locked(self) -> None:
        """送信待ちファイルを保存する（_lock 保持中に呼ぶこと）。"""
        try:
            tmp = self._outbox_path.with_name(self._outbox_path.name + ".tmp")
            tmp.write_text(
                json.dumps(self._outbox, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            tmp.replace(self._outbox_path)
        except Exception as e:
            logger.error(f"履歴同期: 送信待ちファイルの保存に失敗: {e}")

    def _load_cache(self) -> dict:
        """受信キャッシュファイルを読み込む。壊れていれば空で開始する（クラッシュさせない）。"""
        try:
            if self._cache_path.exists():
                data = json.loads(self._cache_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    items = data.get("items")
                    return {
                        "cursor": str(data.get("cursor", "") or ""),
                        "items": items if isinstance(items, list) else [],
                    }
        except Exception as e:
            logger.warning(f"履歴同期: 受信キャッシュの読み込みに失敗（空で開始します）: {e}")
        return {"cursor": "", "items": []}

    def _save_cache_locked(self) -> None:
        """受信キャッシュファイルを保存する（_lock 保持中に呼ぶこと）。"""
        try:
            tmp = self._cache_path.with_name(self._cache_path.name + ".tmp")
            tmp.write_text(
                json.dumps(self._cache, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            tmp.replace(self._cache_path)
        except Exception as e:
            logger.error(f"履歴同期: 受信キャッシュの保存に失敗: {e}")
