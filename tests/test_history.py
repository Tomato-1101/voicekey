"""history（音声入力履歴ストア）のロジックテスト。

追加・上限・永続化・消去・壊れたファイルからの復帰を、
一時ディレクトリ上のファイルで検証する（既定パスには触れない）。
"""

import json
import tempfile
import unittest
from pathlib import Path

from src.core.history import MAX_ITEMS, HistoryStore


class TestHistoryStore(unittest.TestCase):
    """HistoryStore の基本動作テスト。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "history.json"

    def tearDown(self):
        self._tmp.cleanup()

    def test_add_newest_first(self):
        """追加した履歴が新しい順に並ぶ。"""
        store = HistoryStore(self.path)
        store.add("ひとつめ")
        store.add("ふたつめ")
        texts = [e["text"] for e in store.items()]
        self.assertEqual(texts, ["ふたつめ", "ひとつめ"])

    def test_entry_has_date(self):
        """各エントリに日時（ISO 8601）が付く。"""
        store = HistoryStore(self.path)
        store.add("テスト")
        entry = store.items()[0]
        self.assertIn("T", entry["date"])

    def test_empty_text_ignored(self):
        """空文字・空白のみのテキストは履歴に追加されない。"""
        store = HistoryStore(self.path)
        store.add("")
        store.add("   \n  ")
        self.assertEqual(store.items(), [])

    def test_max_items_trim(self):
        """上限を超えると古いものから捨てられる。"""
        store = HistoryStore(self.path)
        for i in range(MAX_ITEMS + 5):
            store.add(f"エントリ {i}")
        items = store.items()
        self.assertEqual(len(items), MAX_ITEMS)
        # 最新（最後に追加したもの）が先頭、最古の 5 件が落ちている
        self.assertEqual(items[0]["text"], f"エントリ {MAX_ITEMS + 4}")
        self.assertEqual(items[-1]["text"], "エントリ 5")

    def test_persistence_across_instances(self):
        """別インスタンスで開き直しても履歴が残る（再起動相当）。"""
        store = HistoryStore(self.path)
        store.add("再起動前のテキスト")
        reopened = HistoryStore(self.path)
        self.assertEqual(reopened.items()[0]["text"], "再起動前のテキスト")

    def test_clear(self):
        """clear() でメモリとファイルの両方が空になる。"""
        store = HistoryStore(self.path)
        store.add("消えるテキスト")
        store.clear()
        self.assertEqual(store.items(), [])
        self.assertEqual(HistoryStore(self.path).items(), [])

    def test_corrupted_file_starts_empty(self):
        """壊れた JSON ファイルがあっても空履歴で起動する（クラッシュしない）。"""
        self.path.write_text("{ これは JSON ではない", encoding="utf-8")
        store = HistoryStore(self.path)
        self.assertEqual(store.items(), [])
        # その後の追加・保存も正常に動く
        store.add("復帰後のテキスト")
        self.assertEqual(HistoryStore(self.path).items()[0]["text"], "復帰後のテキスト")

    def test_load_skips_invalid_entries(self):
        """型が不正な要素・空テキストの要素は読み込み時に除外される。"""
        data = [
            {"text": "正常な履歴", "date": "2026-06-12T00:00:00+09:00"},
            {"text": "  ", "date": "2026-06-12T00:00:00+09:00"},
            "ただの文字列",
            123,
        ]
        self.path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        store = HistoryStore(self.path)
        self.assertEqual([e["text"] for e in store.items()], ["正常な履歴"])

    def test_items_returns_copy(self):
        """items() の返り値を書き換えても内部状態に影響しない。"""
        store = HistoryStore(self.path)
        store.add("オリジナル")
        items = store.items()
        items[0]["text"] = "改ざん"
        self.assertEqual(store.items()[0]["text"], "オリジナル")

    def test_entry_has_id_and_device(self):
        """新規エントリには 32 桁 hex の id と device="windows" が付く（history_sync 用）。"""
        store = HistoryStore(self.path)
        store.add("同期対象")
        entry = store.items()[0]
        self.assertEqual(len(entry["id"]), 32)
        int(entry["id"], 16)  # hex として解釈できること
        self.assertEqual(entry["device"], "windows")

    def test_legacy_entries_get_id_filled_in(self):
        """id/device の無い旧形式ファイルを読み込むと、id が新規採番され device が補われる。"""
        data = [{"text": "旧形式のエントリ", "date": "2026-06-12T00:00:00+09:00"}]
        self.path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        store = HistoryStore(self.path)
        entry = store.items()[0]
        self.assertEqual(len(entry["id"]), 32)
        self.assertEqual(entry["device"], "windows")

    def test_on_add_receives_copy_of_saved_entry(self):
        """on_add は保存済みエントリのコピー（id/text/date/device 込み）を受け取る。"""
        received = []
        store = HistoryStore(self.path, on_add=received.append)
        store.add("コールバック対象")
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]["text"], "コールバック対象")
        self.assertEqual(received[0]["device"], "windows")
        self.assertIn(received[0]["id"], [e["id"] for e in store.items()])
        # コールバックへ渡ったものはコピーであり、内部状態と同一オブジェクトではない
        received[0]["text"] = "改ざん"
        self.assertEqual(store.items()[0]["text"], "コールバック対象")

    def test_on_add_exception_does_not_break_add(self):
        """on_add が例外を投げても add() 自体は正常に完了する（貼り付け経路をブロックしない）。"""
        def _boom(entry):
            raise RuntimeError("同期に失敗")

        store = HistoryStore(self.path, on_add=_boom)
        store.add("例外があっても保存される")
        self.assertEqual(store.items()[0]["text"], "例外があっても保存される")
        # 保存自体は完了しているので、開き直しても残っている
        self.assertEqual(HistoryStore(self.path).items()[0]["text"], "例外があっても保存される")


if __name__ == "__main__":
    unittest.main()
