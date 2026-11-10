"""クリップボード履歴（src/core/clipboard_history.py）のテスト。

検証する性質（Mac の ClipboardHistoryStoreTests と対）:
1. 印の判定: ExcludeClipboardContentFromMonitorProcessing / Clipboard Viewer Ignore /
   CanIncludeInClipboardHistory=0 は記録しない。値 1 は記録する。値が読めなければ記録しない。
2. 記録: 新しい順・空白だけは無視・先頭と同じは無視・途中の重複は先頭へ移す・件数の上限。
3. 永続化: 開き直しても残る・件数を下げたら即保存・壊れたファイルは退避して空で始める。
4. 監視: dataChanged で記録し、stop で止まる。サイドノッチの印付きコピーは記録しない。
5. 設定: 既定値と、範囲外の件数の補正。音声入力の履歴の件数も設定で変わる。

実際の OS のクリップボードには触れない（ダミーの QClipboard に QMimeData を持たせて通知を送る）。
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import yaml
from PySide6.QtCore import QMimeData, QObject, Signal
from PySide6.QtWidgets import QApplication

from src.config.config_manager import ConfigManager
from src.config.constants import DEFAULT_CONFIG
from src.core.clipboard_history import (
    CAN_INCLUDE_MIME,
    EXCLUDE_MIME,
    ClipboardHistoryStore,
    is_excluded,
)
from src.core.history import HistoryStore
from src.ui.side_notch import marked_mime_data

_app = QApplication.instance() or QApplication([])


def _qt(name: str) -> str:
    return f'application/x-qt-windows-mime;value="{name}"'


class IsExcludedTests(unittest.TestCase):
    def test_plain_text_is_recorded(self):
        self.assertFalse(is_excluded(["text/plain", "text/html"]))

    def test_exclude_marker(self):
        self.assertTrue(is_excluded(["text/plain", _qt("ExcludeClipboardContentFromMonitorProcessing")]))

    def test_clipboard_viewer_ignore(self):
        self.assertTrue(is_excluded(["text/plain", _qt("Clipboard Viewer Ignore")]))

    def test_case_insensitive_and_bare_name(self):
        self.assertTrue(is_excluded(["EXCLUDECLIPBOARDCONTENTFROMMONITORPROCESSING"]))
        self.assertTrue(is_excluded([_qt("clipboard viewer ignore")]))

    def test_can_include_zero_is_excluded(self):
        fmt = _qt("CanIncludeInClipboardHistory")
        self.assertTrue(is_excluded([fmt], lambda f: b"\x00\x00\x00\x00"))

    def test_can_include_one_is_recorded(self):
        fmt = _qt("CanIncludeInClipboardHistory")
        self.assertFalse(is_excluded([fmt], lambda f: b"\x01\x00\x00\x00"))

    def test_can_include_unreadable_is_excluded(self):
        fmt = _qt("CanIncludeInClipboardHistory")
        self.assertTrue(is_excluded([fmt]))
        self.assertTrue(is_excluded([fmt], lambda f: b""))

        def boom(_):
            raise RuntimeError("読めない")

        self.assertTrue(is_excluded([fmt], boom))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "clipboard_history.json"

    def tearDown(self):
        self._tmp.cleanup()

    def texts(self, store):
        return [e["text"] for e in store.items()]

    def test_newest_first_with_app(self):
        store = ClipboardHistoryStore(self.path)
        self.assertTrue(store.record("ひとつめ", app_id="notepad.exe", app_name="メモ帳"))
        store.record("ふたつめ")
        self.assertEqual(self.texts(store), ["ふたつめ", "ひとつめ"])
        self.assertEqual(store.items()[1]["app_name"], "メモ帳")
        self.assertIn("T", store.items()[0]["date"])

    def test_whitespace_ignored(self):
        store = ClipboardHistoryStore(self.path)
        self.assertFalse(store.record("   \n\t "))
        self.assertFalse(store.record(""))
        self.assertEqual(store.items(), [])

    def test_marked_copy_ignored(self):
        store = ClipboardHistoryStore(self.path)
        self.assertFalse(store.record("秘密", formats=[_qt("ExcludeClipboardContentFromMonitorProcessing")]))
        self.assertFalse(
            store.record("秘密", formats=[_qt("CanIncludeInClipboardHistory")], read_data=lambda f: b"\0\0\0\0")
        )
        self.assertEqual(store.items(), [])

    def test_same_as_head_ignored(self):
        store = ClipboardHistoryStore(self.path)
        store.record("A")
        first_id = store.items()[0]["id"]
        self.assertFalse(store.record("A"))
        self.assertEqual(len(store.items()), 1)
        self.assertEqual(store.items()[0]["id"], first_id)

    def test_duplicate_moves_to_front(self):
        store = ClipboardHistoryStore(self.path)
        for text in ("A", "B", "C"):
            store.record(text)
        store.record("A")
        self.assertEqual(self.texts(store), ["A", "C", "B"])

    def test_limit_trims_oldest(self):
        store = ClipboardHistoryStore(self.path, limit=3)
        for i in range(5):
            store.record(f"コピー {i}")
        self.assertEqual(self.texts(store), ["コピー 4", "コピー 3", "コピー 2"])

    def test_lowering_limit_trims_and_saves(self):
        store = ClipboardHistoryStore(self.path, limit=10)
        for i in range(5):
            store.record(f"コピー {i}")
        store.limit = 2
        self.assertEqual(self.texts(store), ["コピー 4", "コピー 3"])
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual([e["text"] for e in saved], ["コピー 4", "コピー 3"])

    def test_persistence_across_instances(self):
        store = ClipboardHistoryStore(self.path)
        store.record("残る")
        reopened = ClipboardHistoryStore(self.path)
        self.assertEqual(self.texts(reopened), ["残る"])

    def test_load_respects_limit(self):
        store = ClipboardHistoryStore(self.path, limit=10)
        for i in range(5):
            store.record(f"コピー {i}")
        self.assertEqual(len(ClipboardHistoryStore(self.path, limit=2).items()), 2)

    def test_clear(self):
        store = ClipboardHistoryStore(self.path)
        store.record("消える")
        store.clear()
        self.assertEqual(store.items(), [])
        self.assertEqual(ClipboardHistoryStore(self.path).items(), [])

    def test_clear_failure_keeps_items(self):
        # 保存できなかったら False を返し、メモリ上も消さない（画面だけ消えて再起動で復活しないように）
        store = ClipboardHistoryStore(self.path)
        store.record("残る")
        self.path.unlink()
        self.path.mkdir()
        (self.path / "block").write_text("x", encoding="utf-8")  # 置換先が中身のあるディレクトリ＝書けない
        self.assertFalse(store.clear())
        self.assertEqual(self.texts(store), ["残る"])

    def test_clear_returns_true(self):
        store = ClipboardHistoryStore(self.path)
        store.record("消える")
        self.assertTrue(store.clear())

    def test_corrupt_file_is_quarantined(self):
        self.path.write_text("{壊れた", encoding="utf-8")
        store = ClipboardHistoryStore(self.path)
        self.assertEqual(store.items(), [])
        # 元のファイルは退避されて残る（上書きして消さない）
        leftovers = [p.name for p in Path(self._tmp.name).iterdir() if p.name != "clipboard_history.json"]
        self.assertTrue(leftovers)
        store.record("新しい")
        self.assertEqual(self.texts(ClipboardHistoryStore(self.path)), ["新しい"])

    def test_default_path_next_to_history(self):
        # 既定の保存先は history.json と同じディレクトリ（実際の既定パスには触れない）
        fake_history = Path(self._tmp.name) / "history.json"
        with mock.patch("src.core.clipboard_history._history_default_path", return_value=fake_history):
            store = ClipboardHistoryStore()
        self.assertEqual(store._path, Path(self._tmp.name) / "clipboard_history.json")


class _FakeClipboard(QObject):
    """QClipboard の代わり（dataChanged と mimeData() だけ）。実 OS のクリップボードには触れない。"""

    dataChanged = Signal()

    def __init__(self):
        super().__init__()
        self._mime = QMimeData()

    def set(self, mime: QMimeData) -> None:
        self._mime = mime
        self.dataChanged.emit()

    def mimeData(self):
        return self._mime


def _text_mime(text: str) -> QMimeData:
    mime = QMimeData()
    mime.setText(text)
    return mime


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "clipboard_history.json"
        self.clipboard = _FakeClipboard()
        self.store = ClipboardHistoryStore(self.path, app_provider=lambda: ("code.exe", "Visual Studio Code"))

    def tearDown(self):
        self.store.stop()
        self._tmp.cleanup()

    def test_records_on_data_changed(self):
        self.assertFalse(self.store.is_running)
        self.store.start(self.clipboard)
        self.assertTrue(self.store.is_running)
        self.clipboard.set(_text_mime("コピーした"))
        items = self.store.items()
        self.assertEqual([e["text"] for e in items], ["コピーした"])
        self.assertEqual(items[0]["app_id"], "code.exe")

    def test_stop_stops_recording_and_keeps_items(self):
        self.store.start(self.clipboard)
        self.clipboard.set(_text_mime("1 件目"))
        self.store.stop()
        self.assertFalse(self.store.is_running)
        self.clipboard.set(_text_mime("2 件目"))
        self.assertEqual([e["text"] for e in self.store.items()], ["1 件目"])

    def test_non_text_ignored(self):
        self.store.start(self.clipboard)
        mime = QMimeData()
        mime.setData("image/png", b"\x89PNG")
        self.clipboard.set(mime)
        self.assertEqual(self.store.items(), [])

    def test_excluded_mime_ignored(self):
        self.store.start(self.clipboard)
        mime = _text_mime("パスワード")
        mime.setData(EXCLUDE_MIME, b"\0\0\0\0")
        self.clipboard.set(mime)
        mime2 = _text_mime("パスワード 2")
        mime2.setData(CAN_INCLUDE_MIME, b"\0\0\0\0")
        self.clipboard.set(mime2)
        self.assertEqual(self.store.items(), [])

    def test_self_write_is_ignored_once(self):
        # 印の付かない自分の書き込み（pyperclip のフォールバック・文字列だけの復元）も記録しない
        self.store.start(self.clipboard)
        self.store.ignore_next_change("音声入力の結果")
        self.clipboard.set(_text_mime("音声入力の結果"))
        self.store.ignore_next_change("原本")
        self.clipboard.set(_text_mime("原本"))
        self.assertEqual(self.store.items(), [])
        # 1 回限り: 利用者が同じ本文を改めてコピーしたら記録する
        self.clipboard.set(_text_mime("原本"))
        self.assertEqual([e["text"] for e in self.store.items()], ["原本"])

    def test_self_write_with_marks_consumes_notice(self):
        # 印付きの書き込みでも知らせた分は使い切る（残して後の同じ本文のコピーを取りこぼさない）
        self.store.start(self.clipboard)
        self.store.ignore_next_change("パスワード")
        mime = _text_mime("パスワード")
        mime.setData(EXCLUDE_MIME, b"\0\0\0\0")
        self.clipboard.set(mime)
        self.clipboard.set(_text_mime("パスワード"))
        self.assertEqual([e["text"] for e in self.store.items()], ["パスワード"])

    def test_unmatched_notice_is_dropped(self):
        # 書けずに終わった分の印は、次の変更が違う本文なら捨てる
        self.store.start(self.clipboard)
        self.store.ignore_next_change("書けなかった")
        self.clipboard.set(_text_mime("別のコピー"))
        self.clipboard.set(_text_mime("書けなかった"))
        self.assertEqual([e["text"] for e in self.store.items()], ["書けなかった", "別のコピー"])

    def test_delayed_notifications_are_matched_in_order(self):
        # 変更通知が遅れて 2 回分まとめて届いても、どちらも自分の書き込みとして扱う
        self.store.start(self.clipboard)
        self.store.ignore_next_change("一時")
        self.store.ignore_next_change("原本")
        self.clipboard.set(_text_mime("一時"))
        self.clipboard.set(_text_mime("原本"))
        self.assertEqual(self.store.items(), [])

    def test_crlf_and_lf_match(self):
        # pyperclip は CRLF、Qt の text() は LF で返すので改行を揃えて比べる
        self.store.start(self.clipboard)
        self.store.ignore_next_change("1 行目\r\n2 行目")
        self.clipboard.set(_text_mime("1 行目\n2 行目"))
        self.assertEqual(self.store.items(), [])

    def test_notice_while_stopped_is_dropped(self):
        # 止めている間の知らせは届く変更が無いので残さない（再開後の同じ本文のコピーを捨てない）
        self.store.ignore_next_change("止めている間")
        self.store.start(self.clipboard)
        self.clipboard.set(_text_mime("止めている間"))
        self.assertEqual([e["text"] for e in self.store.items()], ["止めている間"])

    def test_side_notch_copy_is_not_recorded(self):
        # サイドノッチからのコピーは印付きなので、自分の履歴に記録し直さない
        self.store.start(self.clipboard)
        self.clipboard.set(marked_mime_data("履歴から再コピー"))
        self.assertEqual(self.store.items(), [])
        self.assertEqual(marked_mime_data("x").text(), "x")


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self._paths = []

    def tearDown(self):
        for path in self._paths:
            for p in (path, path + ".tmp"):
                if os.path.exists(p):
                    os.unlink(p)

    def _manager(self, data):
        handle = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8")
        yaml.dump(data, handle, allow_unicode=True)
        handle.close()
        self._paths.append(handle.name)
        return ConfigManager(config_path=handle.name)

    def test_defaults(self):
        self.assertEqual(DEFAULT_CONFIG["history_limit"], 200)
        self.assertTrue(DEFAULT_CONFIG["clipboard_history_enabled"])
        self.assertEqual(DEFAULT_CONFIG["clipboard_history_limit"], 200)
        cm = self._manager({"language": "ja"})
        self.assertEqual(cm.get("history_limit"), 200)
        self.assertTrue(cm.get("clipboard_history_enabled"))
        self.assertEqual(cm.get("clipboard_history_limit"), 200)

    def test_valid_values_kept(self):
        cm = self._manager({"history_limit": 1000, "clipboard_history_limit": 50, "clipboard_history_enabled": False})
        self.assertEqual(cm.get("history_limit"), 1000)
        self.assertEqual(cm.get("clipboard_history_limit"), 50)
        self.assertFalse(cm.get("clipboard_history_enabled"))

    def test_out_of_range_reset_to_default(self):
        for bad in (0, 7, 5000, -1, "500", True, None):
            cm = self._manager({"history_limit": bad, "clipboard_history_limit": bad})
            self.assertEqual(cm.get("history_limit"), 200, bad)
            self.assertEqual(cm.get("clipboard_history_limit"), 200, bad)

    def test_save_corrects_out_of_range(self):
        cm = self._manager({"language": "ja"})
        self.assertTrue(cm.save({"history_limit": 3, "clipboard_history_limit": 500}))
        with open(cm.config_path, encoding="utf-8") as f:
            saved = yaml.safe_load(f)
        self.assertEqual(saved["history_limit"], 200)
        self.assertEqual(saved["clipboard_history_limit"], 500)


class VoiceHistoryLimitTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "history.json"

    def tearDown(self):
        self._tmp.cleanup()

    def test_limit_param_and_setter(self):
        store = HistoryStore(self.path, limit=3)
        for i in range(5):
            store.add(f"音声 {i}")
        self.assertEqual(len(store.items()), 3)
        store.limit = 1000
        for i in range(5, 10):
            store.add(f"音声 {i}")
        self.assertEqual(len(store.items()), 8)
        store.limit = 2
        self.assertEqual([e["text"] for e in store.items()], ["音声 9", "音声 8"])
        self.assertEqual(len(HistoryStore(self.path, limit=1000).items()), 2)

    def test_load_with_larger_limit_keeps_items(self):
        store = HistoryStore(self.path, limit=500)
        for i in range(300):
            store.add(f"音声 {i}")
        self.assertEqual(len(HistoryStore(self.path, limit=500).items()), 300)


if __name__ == "__main__":
    unittest.main()
