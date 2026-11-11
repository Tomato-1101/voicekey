"""api_pricing（単価表）と api_usage（API 使用量ストア）のロジックテスト。

単価・最低課金秒数・日またぎ・月またぎ・内訳・推移・表示モード・壊れたファイルからの復帰を、
一時ディレクトリ上のファイルで検証する（既定パスには触れない・ネットワークは使わない）。
"""

import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from src.core import api_pricing
from src.core.api_usage import ApiUsageStore


def _dt(text: str) -> datetime:
    """"2026-11-11 10:00" 形式からローカル日時を作る。"""
    return datetime.strptime(text, "%Y-%m-%d %H:%M")


class TestApiPricing(unittest.TestCase):
    """単価表と料金計算のテスト（Mac 版 ApiPricing.swift と同じ値）。"""

    def test_audio_price_per_hour(self):
        """音声は 1 時間あたりの単価で計算する（Soniox $0.12/時）。"""
        self.assertAlmostEqual(api_pricing.cost("soniox", "stt-rt-v5", billed_audio_seconds=3600), 0.12)
        self.assertAlmostEqual(
            api_pricing.cost("openai", "gpt-transcribe", billed_audio_seconds=60), 0.0045
        )

    def test_model_is_case_insensitive(self):
        """モデル ID は大文字小文字を区別しない。"""
        self.assertIsNotNone(api_pricing.price("microsoft", "MAI-Transcribe-2"))

    def test_token_price(self):
        """LLM は 100 万トークンあたりの入出力単価で計算する。"""
        usd = api_pricing.cost(
            "groq", "openai/gpt-oss-20b", input_tokens=1_000_000, output_tokens=2_000_000
        )
        self.assertAlmostEqual(usd, 0.075 + 0.60)

    def test_groq_minimum_billed_seconds(self):
        """Groq whisper は 1 リクエスト最低 10 秒課金。単価未確認のモデルは実秒数のまま。"""
        self.assertEqual(api_pricing.billed_seconds("groq", "whisper-large-v3-turbo", 3), 10)
        self.assertEqual(api_pricing.billed_seconds("groq", "whisper-large-v3-turbo", 12), 12)
        self.assertEqual(api_pricing.billed_seconds("deepgram", "nova-3", 3), 3)

    def test_unknown_model_is_unpriced(self):
        """表に無いモデルは None（単価未確認）。推測で 0 にしない。"""
        self.assertIsNone(api_pricing.cost("deepgram", "nova-3", billed_audio_seconds=60))
        self.assertIsNone(api_pricing.cost("elevenlabs", "scribe_v1", billed_audio_seconds=60))

    def test_apple_is_free(self):
        """Apple（Windows には無いエンジン）は常に 0 ドル。"""
        self.assertEqual(api_pricing.cost("apple", "anything", billed_audio_seconds=3600), 0)

    def test_engine_ids_match_mac_backend(self):
        """エンジン id は Mac の Backend rawValue と同じ文字列で課金元へ引ける。"""
        self.assertEqual(api_pricing.provider_for_engine("openai_live"), "openai")
        self.assertEqual(api_pricing.provider_for_engine("azure_mai"), "microsoft")
        self.assertEqual(api_pricing.provider_for_engine("apple_local"), "apple")
        self.assertEqual(api_pricing.provider_for_engine("deepgram"), "deepgram")
        self.assertIsNone(api_pricing.provider_for_engine("unknown"))

    def test_free_tier_only_in_paid_mode(self):
        """「実際に払った分」では有料契約の無いプロバイダーが無料枠扱い。定価では常に計上。"""
        self.assertTrue(api_pricing.is_free_tier(api_pricing.MODE_PAID, "groq"))
        self.assertFalse(api_pricing.is_free_tier(api_pricing.MODE_PAID, "openai"))
        self.assertFalse(api_pricing.is_free_tier(api_pricing.MODE_LIST, "groq"))

    def test_format_usd(self):
        """1 セント未満は桁を増やして表示する。"""
        self.assertEqual(api_pricing.format_usd(0.12), "$0.12")
        self.assertEqual(api_pricing.format_usd(0.0012), "$0.0012")
        self.assertEqual(api_pricing.format_usd(0), "$0.00")


class TestApiUsageStore(unittest.TestCase):
    """ApiUsageStore の記録・集計・保存のテスト。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "api-usage.json"

    def tearDown(self):
        self._tmp.cleanup()

    def test_record_stt_cost(self):
        """文字起こし 1 回分の料金が今日の合計に出る。"""
        store = ApiUsageStore(self.path)
        now = _dt("2026-11-11 10:00")
        store.record_stt("soniox", "stt-rt-v5", 1800, now=now)
        s = store.today_summary(now=now)
        self.assertAlmostEqual(s["usd"], 0.06)
        self.assertEqual(s["requests"], 1)
        self.assertFalse(s["has_unpriced"])

    def test_minimum_billed_applied_per_request(self):
        """最低課金秒数は 1 リクエストごとに適用される（3 秒 × 2 回 = 20 秒分）。"""
        store = ApiUsageStore(self.path)
        now = _dt("2026-11-11 10:00")
        store.record_stt("groq", "whisper-large-v3-turbo", 3, now=now)
        store.record_stt("groq", "whisper-large-v3-turbo", 3, now=now)
        row = store.breakdown(lambda d: True)[0]
        self.assertEqual(row["audio_seconds"], 6)
        self.assertEqual(row["billed_audio_seconds"], 20)
        self.assertEqual(row["requests"], 2)

    def test_zero_seconds_not_recorded(self):
        """0 秒以下の音声は記録しない。"""
        store = ApiUsageStore(self.path)
        store.record_stt("groq", "whisper-large-v3-turbo", 0)
        self.assertEqual(store.all_time_summary()["requests"], 0)

    def test_unpriced_is_flagged_not_hidden(self):
        """単価未確認のモデルは 0 ドルに見せず has_unpriced で知らせる。"""
        store = ApiUsageStore(self.path)
        now = _dt("2026-11-11 10:00")
        store.record_stt("deepgram", "nova-3", 60, now=now)
        s = store.today_summary(now=now)
        self.assertTrue(s["has_unpriced"])
        self.assertEqual(s["usd"], 0)

    def test_day_boundary(self):
        """日をまたぐと今日の合計は別になり、今月には両日が入る。"""
        store = ApiUsageStore(self.path)
        store.record_stt("soniox", "stt-rt-v5", 3600, now=_dt("2026-11-10 23:59"))
        store.record_stt("soniox", "stt-rt-v5", 3600, now=_dt("2026-11-11 00:01"))
        now = _dt("2026-11-11 12:00")
        self.assertAlmostEqual(store.today_summary(now=now)["usd"], 0.12)
        self.assertAlmostEqual(store.month_summary(now=now)["usd"], 0.24)

    def test_month_boundary(self):
        """月をまたぐと今月の合計は別になり、累計には両月が入る。"""
        store = ApiUsageStore(self.path)
        store.record_stt("soniox", "stt-rt-v5", 3600, now=_dt("2026-10-31 23:59"))
        store.record_stt("soniox", "stt-rt-v5", 3600, now=_dt("2026-11-01 00:01"))
        now = _dt("2026-11-01 12:00")
        self.assertAlmostEqual(store.month_summary(now=now)["usd"], 0.12)
        self.assertAlmostEqual(store.all_time_summary()["usd"], 0.24)

    def test_breakdown_by_provider_model_purpose(self):
        """内訳はプロバイダー × モデル × 用途ごとに 1 行・料金の高い順・未確認は末尾。"""
        store = ApiUsageStore(self.path)
        now = _dt("2026-11-11 10:00")
        store.record_stt("deepgram", "nova-3", 60, now=now)
        store.record_stt("soniox", "stt-rt-v5", 3600, now=now)
        store.record_stt("soniox", "stt-rt-v5", 3600, now=now - timedelta(days=1))
        store.record_llm("groq", "openai/gpt-oss-20b", "formatting", 1000, 500, now=now)
        rows = store.breakdown(lambda d: True)
        self.assertEqual([(r["provider"], r["model"]) for r in rows], [
            ("soniox", "stt-rt-v5"), ("groq", "openai/gpt-oss-20b"), ("deepgram", "nova-3"),
        ])
        self.assertAlmostEqual(rows[0]["usd"], 0.24)
        self.assertEqual(rows[0]["requests"], 2)
        self.assertEqual(rows[0]["provider_label"], "Soniox")
        self.assertEqual(rows[1]["purpose_label"], "テキスト整形")
        self.assertIsNone(rows[2]["usd"])

    def test_paid_mode_zeroes_free_tier(self):
        """「実際に払った分」では Groq（無料枠）が 0 ドル、OpenAI は計上される。"""
        store = ApiUsageStore(self.path)
        now = _dt("2026-11-11 10:00")
        store.record_llm("groq", "openai/gpt-oss-120b", "formatting", 1_000_000, 0, now=now)
        store.record_stt("openai", "gpt-transcribe", 60, now=now)
        self.assertAlmostEqual(store.today_summary(api_pricing.MODE_LIST, now)["usd"], 0.15 + 0.0045)
        self.assertAlmostEqual(store.today_summary(api_pricing.MODE_PAID, now)["usd"], 0.0045)

    def test_daily_series(self):
        """推移は末尾が今日・古い順・記録の無い日は 0。"""
        store = ApiUsageStore(self.path)
        store.record_stt("soniox", "stt-rt-v5", 3600, now=_dt("2026-11-09 10:00"))
        series = store.daily_series(3, end_day=_dt("2026-11-11 10:00").date())
        self.assertEqual([d["day"] for d in series], ["2026-11-09", "2026-11-10", "2026-11-11"])
        self.assertAlmostEqual(series[0]["usd"], 0.12)
        self.assertEqual(series[1]["requests"], 0)

    def test_snapshot_shape(self):
        """snapshot はホームに出す値（今日・今月・累計・内訳・30 日の推移）をまとめて返す。"""
        store = ApiUsageStore(self.path)
        now = _dt("2026-11-11 10:00")
        store.record_stt("soniox", "stt-rt-v5", 3600, now=now)
        store.record_stt("soniox", "stt-rt-v5", 3600, now=_dt("2026-10-01 10:00"))
        snap = store.snapshot(period="month", now=now)
        self.assertEqual(len(snap["daily"]), 30)
        self.assertEqual(snap["daily"][-1]["day"], "2026-11-11")
        self.assertAlmostEqual(snap["all_time"]["usd"], 0.24)
        self.assertAlmostEqual(snap["breakdown"][0]["usd"], 0.12)  # 今月分だけ
        self.assertEqual(store.snapshot(period="all", now=now)["breakdown"][0]["requests"], 2)
        self.assertEqual(snap["checked_on"], api_pricing.CHECKED_ON)

    def test_record_chat_usage(self):
        """チャット応答の usage（通常）と x_groq.usage（ストリーミング最終）を読む。"""
        store = ApiUsageStore(self.path)
        self.assertTrue(store.record_chat_usage(
            {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5}},
            "groq", "openai/gpt-oss-20b", "formatting"))
        self.assertTrue(store.record_chat_usage(
            {"x_groq": {"usage": {"prompt_tokens": 1, "completion_tokens": 2}}},
            "groq", "openai/gpt-oss-20b", "formatting"))
        self.assertFalse(store.record_chat_usage({"choices": []}, "groq", "m", "formatting"))
        row = store.breakdown(lambda d: True)[0]
        self.assertEqual((row["input_tokens"], row["output_tokens"], row["requests"]), (11, 7, 2))

    def test_persistence_and_no_text_saved(self):
        """開き直しても残り、ファイルには数量だけ（本文の欄が無い）。"""
        store = ApiUsageStore(self.path)
        store.record_stt("soniox", "stt-rt-v5", 10, now=_dt("2026-11-11 10:00"))
        reopened = ApiUsageStore(self.path)
        self.assertEqual(reopened.all_time_summary()["requests"], 1)
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        row = raw["days"]["2026-11-11"][0]
        self.assertEqual(set(row), {
            "provider", "model", "purpose", "requests", "audio_seconds",
            "billed_audio_seconds", "input_tokens", "output_tokens",
        })

    def test_corrupted_file_is_quarantined(self):
        """壊れたファイルは退避して空で開始し、元の中身は消さない。"""
        self.path.write_text("{ これは JSON ではない", encoding="utf-8")
        store = ApiUsageStore(self.path)
        self.assertEqual(store.all_time_summary()["requests"], 0)
        backups = list(Path(self._tmp.name).glob("api-usage.json.corrupt-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(encoding="utf-8"), "{ これは JSON ではない")
        # その後の記録・保存は正常に動く
        store.record_stt("soniox", "stt-rt-v5", 10)
        self.assertEqual(ApiUsageStore(self.path).all_time_summary()["requests"], 1)

    def test_invalid_rows_are_skipped(self):
        """型が不正な行は読み込み時に捨て、読める行だけ残す。"""
        data = {"days": {"2026-11-11": [
            {"provider": "soniox", "model": "stt-rt-v5", "purpose": "transcription",
             "requests": 1, "audio_seconds": 10, "billed_audio_seconds": 10},
            {"provider": "groq", "requests": "たくさん"},
            "ただの文字列",
        ], "2026-11-10": "壊れた日"}}
        self.path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        store = ApiUsageStore(self.path)
        self.assertEqual(store.all_time_summary()["requests"], 1)


if __name__ == "__main__":
    unittest.main()
