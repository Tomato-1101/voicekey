"""history_sync（Mac と履歴を共有する Cloudflare Worker 連携）のロジックテスト。

httpx.MockTransport でサーバーを模擬し、実通信・実 keyring に触れない
（secrets.get_api_key は差し替えて偽トークンを返す）。スレッドのタイミングに
依存しないよう、常駐ループ（_worker_loop）ではなく _run_cycle() を直接呼んで
1 サイクルぶんの送信・受信を検証する（_worker_loop はこれを呼ぶだけの薄い
ラッパーなので、ロジックのテストとしては _run_cycle() の直接呼び出しで十分）。
"""

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import httpx

from src.core.history_sync import HistorySync
from src.utils import secrets


class _FakeConfig:
    """ConfigManager の代わり。.get(key, default) だけを持つ（tests/test_stats.py 等と同じ流儀）。"""

    def __init__(self, data: dict):
        self._data = data

    def get(self, key, default=None):
        return self._data.get(key, default)


def _enabled_config(url: str = "http://sync.test") -> _FakeConfig:
    return _FakeConfig({"history_sync": {"enabled": True, "url": url}})


def _forbidden_handler(request: httpx.Request) -> httpx.Response:
    """無効時など「通信してはいけない」ケースで使うハンドラ。"""
    raise AssertionError(f"通信してはいけない: {request.method} {request.url}")


class _SyncTestBase(unittest.TestCase):
    """一時ディレクトリ上のファイルで検証する（既定パスには触れない）。トークンは常に偽物。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base_dir = Path(self._tmp.name)
        self._token_patch = mock.patch.object(
            secrets, "get_api_key", return_value="fake-token-for-tests"
        )
        self._token_patch.start()

    def tearDown(self):
        self._token_patch.stop()
        self._tmp.cleanup()

    def _make(self, handler, config=None) -> HistorySync:
        client = httpx.Client(transport=httpx.MockTransport(handler))
        return HistorySync(config or _enabled_config(), base_dir=self.base_dir, client=client)


class TestEnqueueAndFlush(_SyncTestBase):
    """enqueue() の永続化と、成功 POST による outbox のクリアを検証する。"""

    def test_enqueue_persists_to_outbox_file(self):
        """enqueue() は通信せず sync_outbox.json へ即座に追記する。"""
        hs = self._make(_forbidden_handler)
        hs.enqueue({"id": "abc123", "text": "hello", "date": "2026-09-02T09:00:00+09:00", "device": "windows"})

        outbox_path = self.base_dir / "sync_outbox.json"
        self.assertTrue(outbox_path.exists())
        data = json.loads(outbox_path.read_text(encoding="utf-8"))
        self.assertEqual(data[0]["id"], "abc123")
        self.assertEqual(hs.status()["pending"], 1)

    def test_successful_post_sends_bearer_and_items_then_clears_outbox(self):
        """成功 POST は Authorization ヘッダーと items ペイロードを送り、outbox を空にする。"""
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                # 成功 POST の後は仕様どおり GET も飛ぶ（本テストの対象外）。空応答を返す
                return httpx.Response(200, json={"items": []})
            seen["auth"] = request.headers.get("authorization")
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"accepted": 1, "received_at": "2026-09-02T00:00:00Z"})

        hs = self._make(handler)
        hs.enqueue({"id": "abc123", "text": "hello", "date": "2026-09-02T09:00:00+09:00", "device": "windows"})
        hs._run_cycle()

        self.assertEqual(seen["auth"], "Bearer fake-token-for-tests")
        self.assertEqual(seen["body"]["items"][0]["id"], "abc123")
        self.assertEqual(seen["body"]["items"][0]["characters"], len("hello"))
        self.assertIsNone(seen["body"]["items"][0]["app_name"])

        outbox_path = self.base_dir / "sync_outbox.json"
        self.assertEqual(json.loads(outbox_path.read_text(encoding="utf-8")), [])
        self.assertEqual(hs.status()["pending"], 0)


class TestNetworkErrorBackoff(_SyncTestBase):
    """通信失敗時に outbox を保持しつつバックオフし、期限切れ後に再送することを検証する。"""

    def test_connect_error_keeps_outbox_and_backs_off_then_resends(self):
        posts = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                # 成功 POST の後は仕様どおり GET も飛ぶ（本テストの対象外）。空応答を返す
                return httpx.Response(200, json={"items": []})
            posts["n"] += 1
            if posts["n"] == 1:
                raise httpx.ConnectError("boom", request=request)
            return httpx.Response(200, json={"accepted": 1, "received_at": "2026-09-02T00:00:00Z"})

        hs = self._make(handler)
        hs.enqueue({"id": "x1", "text": "t", "date": "2026-09-02T09:00:00+09:00", "device": "windows"})

        hs._run_cycle()
        self.assertEqual(posts["n"], 1)
        self.assertEqual(hs.status()["pending"], 1)  # 失敗したので outbox は残る

        # バックオフ中はすぐの再試行では通信しない
        hs._run_cycle()
        self.assertEqual(posts["n"], 1)

        # バックオフ期限を過去に見せかけて、期限切れ後の再試行が送信されることを確認する
        hs._next_allowed_at = time.monotonic() - 1
        hs._run_cycle()
        self.assertEqual(posts["n"], 2)
        self.assertEqual(hs.status()["pending"], 0)


class TestTokenInvalid(_SyncTestBase):
    """401 は 1 回だけ警告し、apply_config() まで通信を止めることを検証する。"""

    def test_401_halts_until_apply_config(self):
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(401, json={"error": "invalid token"})

        hs = self._make(handler)
        hs.enqueue({"id": "x1", "text": "t", "date": "2026-09-02T09:00:00+09:00", "device": "windows"})

        with self.assertLogs("src.core.history_sync", level="WARNING") as log:
            hs._run_cycle()
        self.assertTrue(any("トークンが無効" in m for m in log.output))
        self.assertTrue(hs.status()["token_invalid"])
        self.assertEqual(calls["n"], 1)

        # halt 中はログも通信も発生しない
        hs._run_cycle()
        self.assertEqual(calls["n"], 1)

        hs.apply_config()
        self.assertFalse(hs.status()["token_invalid"])
        hs._run_cycle()
        self.assertEqual(calls["n"], 2)


class TestFetchMerge(_SyncTestBase):
    """GET の結果を id で重複排除しつつ 200 件へ切り詰め、cursor を更新することを検証する。"""

    def test_get_merges_dedupes_caps_and_updates_cursor(self):
        seen_params = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen_params.append(dict(request.url.params))
            items = [
                {
                    "id": f"srv-{i}",
                    "text": f"text {i}",
                    "date": f"2026-01-01T00:00:{i % 60:02d}+09:00",
                    "device": "mac",
                    "received_at": f"2026-09-02T00:{i // 60:02d}:{i % 60:02d}Z",
                }
                for i in range(250)  # サーバー上限より多い件数を返し、切り詰めを検証する
            ]
            return httpx.Response(200, json={"items": items})

        hs = self._make(handler)
        hs.request_fetch()
        hs._run_cycle()

        cache_path = self.base_dir / "sync_cloud_cache.json"
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
        self.assertEqual(len(cache["items"]), 200)  # 200 件に切り詰め
        self.assertEqual(cache["cursor"], "2026-09-02T00:04:09Z")  # 最大の received_at
        self.assertIsNone(seen_params[0].get("since"))  # 初回は since を送らない

        hs.request_fetch()
        hs._run_cycle()
        self.assertEqual(seen_params[1].get("since"), "2026-09-02T00:04:09Z")  # 2 回目は cursor を送る

    def test_dedupe_by_id_keeps_latest_payload(self):
        call = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            call["n"] += 1
            if call["n"] == 1:
                items = [{"id": "dup1", "text": "old", "date": "2026-01-01T00:00:00+09:00",
                          "device": "mac", "received_at": "2026-09-02T00:00:01Z"}]
            else:
                items = [{"id": "dup1", "text": "new", "date": "2026-01-01T00:00:00+09:00",
                          "device": "mac", "received_at": "2026-09-02T00:00:02Z"}]
            return httpx.Response(200, json={"items": items})

        hs = self._make(handler)
        hs.request_fetch()
        hs._run_cycle()
        hs.request_fetch()
        hs._run_cycle()

        cache = json.loads((self.base_dir / "sync_cloud_cache.json").read_text(encoding="utf-8"))
        self.assertEqual(len(cache["items"]), 1)
        self.assertEqual(cache["items"][0]["text"], "new")


class TestMergedItems(_SyncTestBase):
    """merged_items() の日付降順ソート（オフセット混在）とローカル優先の重複排除を検証する。"""

    def test_order_across_mixed_offsets_and_local_wins_dedupe(self):
        hs = self._make(_forbidden_handler)
        # 受信キャッシュへ直接仕込む（Mac 由来・UTC の "Z" 表記）
        hs._cache = {
            "cursor": "",
            "items": [
                {"id": "shared1", "text": "cloud version", "date": "2026-09-02T00:30:00Z", "device": "mac"},
                {"id": "cloud-only", "text": "cloud only", "date": "2026-09-01T23:00:00Z", "device": "mac"},
            ],
        }
        local_items = [
            # shared1 をローカルでも持つ（内容が違う）→ ローカルが勝つはず
            {"id": "shared1", "text": "local version", "date": "2026-09-02T09:00:00+09:00", "device": "windows"},
            # +09:00 は UTC 換算で cloud 側の shared1 より新しい局所限定エントリ
            {"id": "local-only", "text": "local only", "date": "2026-09-02T10:00:00+09:00", "device": "windows"},
        ]

        merged = hs.merged_items(local_items)
        ids = [e["id"] for e in merged]

        self.assertEqual(ids, ["local-only", "shared1", "cloud-only"])
        shared = next(e for e in merged if e["id"] == "shared1")
        self.assertEqual(shared["text"], "local version")  # ローカルが勝つ


class TestDisabled(_SyncTestBase):
    """無効設定では通信が一切発生しないことを検証する。"""

    def test_disabled_makes_no_requests(self):
        hs = self._make(
            _forbidden_handler,
            config=_FakeConfig({"history_sync": {"enabled": False, "url": "http://sync.test"}}),
        )
        hs.enqueue({"id": "x1", "text": "t", "date": "2026-09-02T09:00:00+09:00", "device": "windows"})
        hs._run_cycle()  # _forbidden_handler が呼ばれなければ OK
        self.assertFalse(hs.enabled)

    def test_request_fetch_is_noop_when_disabled(self):
        hs = self._make(
            _forbidden_handler,
            config=_FakeConfig({"history_sync": {"enabled": False, "url": ""}}),
        )
        hs.request_fetch()
        self.assertFalse(hs._fetch_requested)


class TestStatus(_SyncTestBase):
    """status() が仕様どおりのキー・値を返すことを検証する。"""

    def test_status_fields(self):
        hs = self._make(lambda req: httpx.Response(200, json={"items": []}))
        status = hs.status()
        self.assertEqual(
            set(status.keys()),
            {"enabled", "configured", "pending", "last_sync", "token_invalid", "last_error"},
        )
        self.assertTrue(status["enabled"])
        self.assertTrue(status["configured"])  # url あり・token あり（テストではモック）
        self.assertEqual(status["pending"], 0)
        self.assertIsNone(status["last_sync"])
        self.assertFalse(status["token_invalid"])
        self.assertIsNone(status["last_error"])


class TestThreadLifecycle(_SyncTestBase):
    """enable()/stop() がハングせず、二重 enable() が冪等であることを検証する（スモークテスト）。"""

    def test_enable_and_stop_do_not_hang(self):
        hs = self._make(
            lambda req: httpx.Response(200, json={"items": []}),
            config=_FakeConfig({"history_sync": {"enabled": False, "url": ""}}),
        )
        hs.enable()
        hs.enable()  # 二重に起動しない（冪等）
        hs.stop(timeout=2.0)
        self.assertFalse(hs._thread.is_alive())


if __name__ == "__main__":
    unittest.main()
