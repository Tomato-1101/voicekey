"""録音開始時の処理コンテキスト snapshot（VoicekeyApp._snapshot_context / _process_task）のテスト。

バグ: 録音終了後にキューへ積んだタスクが、処理開始時点の「現在の」slot/model/transcriber を
self._slots から引き直していた。設定 hot-reload やスロット変更が録音終了〜処理開始の間に
挟まると、録音開始時とは別プロバイダーへ音声を送ってしまう。

修正: 録音開始時に slot/provider/model/language/処理フラグを不変の TaskContext に snapshot し、
キュー処理はその snapshot だけを使う。本テストは「設定変更を録音終了と処理開始の間に挟む」状況で、
音声が開始時のプロバイダーへ届き、変更後のプロバイダーへ送られないことを確認する。

VoicekeyApp は QObject 派生で素の生成ができないため、対象メソッドが参照する属性だけを持つ
SimpleNamespace を self に見立て、未束縛メソッドとして呼び出す。
"""

import unittest
from types import SimpleNamespace
from unittest import mock

import numpy as np

from src.app import HotkeySlot, TaskContext, VoicekeyApp
from src.config.constants import SAMPLE_RATE
from src.config.types import TranscriptionTask


class _FakeTranscriber:
    """transcribe 呼び出しを記録するだけのモック transcriber。"""

    def __init__(self, name: str, result: str):
        self.name = name
        self.result = result
        self.calls = 0
        # 直近の transcribe に渡された server_format（サーバー統合整形の伝搬確認用）
        self.last_server_format = None

    def transcribe(self, audio, server_format=False):
        self.calls += 1
        self.last_server_format = server_format
        return self.result


def _slot(
    slot_id: int, backend: str, model: str, transcriber: _FakeTranscriber,
    format_enabled: bool = False,
) -> HotkeySlot:
    return HotkeySlot(
        slot_id=slot_id,
        hotkey="<f2>",
        hotkey_mode="hold",
        required_keys={"f2"},
        backend=backend,
        api_model=model,
        api_prompt="",
        format_enabled=format_enabled,
        transcriber=transcriber,
    )


class _FakeConfig:
    """録音開始時に snapshot で読まれる設定値を返す最小 config。"""

    def __init__(self, values: dict):
        self._values = values

    def get(self, key, default=None):
        return self._values.get(key, default)


class TestSnapshotBindsAtRecordStart(unittest.TestCase):
    def _fake_self(self, slots: dict, config_values: dict):
        return SimpleNamespace(
            _slots=slots,
            _config=_FakeConfig(config_values),
        )

    def test_snapshot_captures_start_slot_and_flags(self):
        """snapshot は開始時の slot と処理フラグを取り込む。"""
        tr = _FakeTranscriber("groq", "x")
        slot_a = _slot(1, "groq", "whisper-large-v3-turbo", tr)
        s = self._fake_self(
            {1: slot_a},
            {
                "language": "en",
                "vad_filter": False,
                "split_parallel_enabled": False,
                "audio_preprocess": {"volume_normalize": False},
                "format_model": "m",
                "format_auto_prompt": "p",
            },
        )

        ctx = VoicekeyApp._snapshot_context(s, slot_a)

        self.assertIs(ctx.slot, slot_a)
        self.assertEqual(ctx.language, "en")
        self.assertFalse(ctx.vad_on)
        self.assertFalse(ctx.split_on)
        self.assertFalse(ctx.volume_normalize)
        self.assertEqual(ctx.format_model, "m")
        self.assertEqual(ctx.format_auto_prompt, "p")
        # format_enabled=False なので短絡で is_logged_in を呼ばずに server_format=False
        self.assertFalse(ctx.server_format)

    def test_snapshot_server_format_eligible(self):
        """groq × 整形 ON × ログイン済みのとき server_format=True。ログアウト/deepgram では False。"""
        tr = _FakeTranscriber("groq", "x")
        slot = _slot(1, "groq", "whisper-large-v3-turbo", tr, format_enabled=True)
        s = self._fake_self({1: slot}, {})

        with mock.patch("src.core.backend_client.is_logged_in", return_value=True):
            self.assertTrue(VoicekeyApp._snapshot_context(s, slot).server_format)
        with mock.patch("src.core.backend_client.is_logged_in", return_value=False):
            self.assertFalse(VoicekeyApp._snapshot_context(s, slot).server_format)

        # deepgram（リアルタイム）は整形 ON・ログイン済みでも統合整形の対象外
        dg = _slot(1, "deepgram", "nova-3", _FakeTranscriber("dg", "x"), format_enabled=True)
        with mock.patch("src.core.backend_client.is_logged_in", return_value=True):
            self.assertFalse(VoicekeyApp._snapshot_context(s, dg).server_format)


class TestConfigChangeBetweenRecordEndAndProcessStart(unittest.TestCase):
    """録音終了〜処理開始の間に設定（slot 差し替え）が挟まる状況の中核テスト。"""

    def _process_self(self, slots: dict):
        s = SimpleNamespace(
            _slots=slots,
            _record_stats=mock.Mock(),
            _insert_and_enter=mock.Mock(),
            notice=mock.Mock(),
        )
        # 整形も snapshot を使うことを確認するため、実装をそのまま束縛して呼ばせる
        s._maybe_format = VoicekeyApp._maybe_format.__get__(s)
        return s

    def _ctx(self, slot: HotkeySlot, server_format: bool = False) -> TaskContext:
        # 録音開始時の snapshot を模す（VAD/分割/正規化を切って REST 直送経路にする）
        return TaskContext(
            slot=slot,
            language="ja",
            vad_on=False,
            split_on=False,
            volume_normalize=False,
            format_model="llama-3.1-8b-instant",
            format_auto_prompt="",
            server_format=server_format,
        )

    def test_audio_goes_to_start_provider_not_reloaded_one(self):
        """開始時プロバイダーへ音声が届き、処理前に差し替わった新プロバイダーへは送られない。"""
        tr_a = _FakeTranscriber("groq", "ぐろっく結果")
        slot_a = _slot(1, "groq", "whisper-large-v3-turbo", tr_a)
        ctx = self._ctx(slot_a)  # 録音開始時に snapshot

        # 録音終了 → タスク投函（snapshot を持たせる）
        audio = np.zeros(SAMPLE_RATE, dtype=np.float32)  # 1 秒（_MIN_AUDIO_SEC 超）
        task = TranscriptionTask(
            audio_data=audio, slot_id=1, timestamp=0.0, context=ctx
        )

        # ここで hot-reload: スロット 1 が別プロバイダー（openai）に差し替わる
        tr_b = _FakeTranscriber("openai", "おーぷんあい結果")
        slot_b = _slot(1, "openai", "gpt-4o-mini-transcribe", tr_b)
        s = self._process_self({1: slot_b})

        # 処理開始
        VoicekeyApp._process_task(s, task)

        self.assertEqual(tr_a.calls, 1, "開始時プロバイダーへ送られていない")
        self.assertEqual(tr_b.calls, 0, "差し替え後の別プロバイダーへ音声が送られた")
        s._insert_and_enter.assert_called_once()
        # 貼り付けられたのは開始時プロバイダーの結果
        self.assertEqual(s._insert_and_enter.call_args.args[0], "ぐろっく結果")

    def test_slot_removed_by_reload_still_processes(self):
        """処理前にスロットが消えても、snapshot により開始時設定で処理を完遂する。"""
        tr_a = _FakeTranscriber("groq", "結果")
        slot_a = _slot(1, "groq", "whisper-large-v3-turbo", tr_a)
        ctx = self._ctx(slot_a)

        audio = np.zeros(SAMPLE_RATE, dtype=np.float32)
        task = TranscriptionTask(
            audio_data=audio, slot_id=1, timestamp=0.0, context=ctx
        )

        # hot-reload でスロット辞書が空になっても（旧実装なら None で早期 return）
        s = self._process_self({})

        VoicekeyApp._process_task(s, task)

        self.assertEqual(tr_a.calls, 1)
        s._insert_and_enter.assert_called_once()

    def test_server_format_skips_client_format(self):
        """統合整形（server_format=True・単発送信）のときは transcribe に伝搬しクライアント整形はスキップする。"""
        tr = _FakeTranscriber("groq", "整形済みテキスト")
        slot = _slot(1, "groq", "whisper-large-v3-turbo", tr, format_enabled=True)
        ctx = self._ctx(slot, server_format=True)

        audio = np.zeros(SAMPLE_RATE, dtype=np.float32)
        task = TranscriptionTask(audio_data=audio, slot_id=1, timestamp=0.0, context=ctx)

        s = self._process_self({1: slot})
        s._maybe_format = mock.Mock()  # 呼ばれたら統合時に再整形した＝バグ

        VoicekeyApp._process_task(s, task)

        self.assertTrue(tr.last_server_format)          # server_format=True が transcribe へ伝搬
        s._maybe_format.assert_not_called()             # サーバー統合済みなので再整形しない
        self.assertEqual(s._insert_and_enter.call_args.args[0], "整形済みテキスト")

    def test_no_server_format_uses_client_format(self):
        """統合でない（server_format=False）ときは従来どおりクライアント整形を通す。"""
        tr = _FakeTranscriber("groq", "生テキスト")
        slot = _slot(1, "groq", "whisper-large-v3-turbo", tr, format_enabled=True)
        ctx = self._ctx(slot, server_format=False)

        audio = np.zeros(SAMPLE_RATE, dtype=np.float32)
        task = TranscriptionTask(audio_data=audio, slot_id=1, timestamp=0.0, context=ctx)

        s = self._process_self({1: slot})
        s._maybe_format = mock.Mock(return_value="整形済み")

        VoicekeyApp._process_task(s, task)

        self.assertFalse(tr.last_server_format)          # server_format は渡らない
        s._maybe_format.assert_called_once()             # クライアント整形が走る
        self.assertEqual(s._insert_and_enter.call_args.args[0], "整形済み")


if __name__ == "__main__":
    unittest.main()
