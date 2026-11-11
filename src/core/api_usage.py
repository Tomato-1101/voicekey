"""
API 使用量の日次集計と永続化モジュール（ホームの「API の利用料金」の元データ）

API 呼び出し 1 回ごとに「日付・プロバイダー・モデル・用途・数量」を積み上げる。
保存するのは数量だけ（音声秒数・トークン数・回数）。発話本文や API キーは受け取らない。
料金は表示時に api_pricing から計算する（単価を直せば過去分にも効く・未確認単価を隠さない）。
アプリを再起動しても残るよう settings.yaml / stats.json と同じディレクトリに JSON で保存する。
Mac 版 Core/ApiUsageStore.swift 相当（計測開始前の推定取り込みは Windows には無い）。
"""

import json
import sys
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Optional

from ..utils.logger import get_logger
from . import api_pricing

logger = get_logger(__name__)

API_USAGE_FILE_NAME = "api-usage.json"

# 1 行が持つ数量の項目（足し合わせの対象）
_INT_FIELDS = ("requests", "input_tokens", "output_tokens")
_FLOAT_FIELDS = ("audio_seconds", "billed_audio_seconds")


def _default_path() -> Path:
    """使用量ファイルの既定パス（settings.yaml / stats.json と同じ配置ロジック）。"""
    if getattr(sys, "frozen", False):
        # PyInstaller 実行時は実行ファイルと同じディレクトリ
        base_dir = Path(sys.executable).parent
    else:
        # 開発時はプロジェクトルート
        base_dir = Path(__file__).parent.parent.parent
    return base_dir / API_USAGE_FILE_NAME


def _day_string(dt: datetime) -> str:
    """ローカルタイムの yyyy-MM-dd（日次集計のキー。stats と同じ書式）。"""
    return dt.strftime("%Y-%m-%d")


def _now() -> datetime:
    return datetime.now().astimezone()


def _new_row(provider: str, model: str, purpose: str) -> dict:
    row = {"provider": provider, "model": model, "purpose": purpose}
    row.update({k: 0 for k in _INT_FIELDS})
    row.update({k: 0.0 for k in _FLOAT_FIELDS})
    return row


def _same_key(a: dict, b: dict) -> bool:
    return a["provider"] == b["provider"] and a["model"] == b["model"] and a["purpose"] == b["purpose"]


def _add_into(dst: dict, src: dict) -> None:
    for k in _INT_FIELDS + _FLOAT_FIELDS:
        dst[k] += src[k]


def row_cost(row: dict, mode: str = api_pricing.MODE_LIST) -> Optional[float]:
    """
    集計 1 行の料金（USD）。

    Args:
        row: 集計行（provider / model / 数量）
        mode: api_pricing.MODE_LIST（定価）か MODE_PAID（実際に払った分）

    Returns:
        料金（USD）。単価未確認なら None。「実際に払った分」で無料枠のプロバイダーは 0
    """
    if api_pricing.is_free_tier(mode, row["provider"]):
        return 0.0
    return api_pricing.cost(
        row["provider"], row["model"],
        billed_audio_seconds=row["billed_audio_seconds"],
        input_tokens=row["input_tokens"], output_tokens=row["output_tokens"],
    )


class ApiUsageStore:
    """
    API 使用量のストア（スレッドセーフ）。

    記録（record_*）は文字起こし・整形のワーカースレッドから、集計（snapshot 等）は
    UI スレッドから呼ばれるため、内部状態はロックで保護する。

    Attributes:
        _days: {"yyyy-MM-dd": [集計行, ...]}。集計行は
            {provider, model, purpose, requests, audio_seconds, billed_audio_seconds,
             input_tokens, output_tokens}
    """

    def __init__(self, file_path: Optional[Path] = None) -> None:
        """
        ストアを初期化し、保存済みの使用量を読み込む。

        Args:
            file_path: 保存先（テスト用。省略時は既定パス）
        """
        self._path = Path(file_path) if file_path else _default_path()
        self._lock = threading.Lock()
        # 壊れたファイルを退避できなかった（＝元ファイルが残っている）ときは保存しない。
        # 空のまま保存すると元の記録を上書きして消すため
        self._save_blocked = False
        self._days: Dict[str, List[dict]] = self._load()

    # ----- 記録（どのスレッドからでも呼べる） -----

    def record_stt(
        self,
        engine_id: str,
        model: str,
        audio_seconds: float,
        purpose: str = "transcription",
        now: Optional[datetime] = None,
    ) -> None:
        """
        音声の文字起こし 1 リクエストを記録する。最低課金秒数はここで 1 回ごとに適用する。

        Args:
            engine_id: 文字起こしエンジン id（Mac の Backend rawValue と同じ。例 "groq"）
            model: モデル ID
            audio_seconds: 実際に送った音声の秒数（0 以下は記録しない）
            purpose: 用途（api_pricing.PURPOSE_LABELS のキー）
            now: 記録日時（テスト用。省略時は現在時刻）
        """
        seconds = float(audio_seconds)
        if seconds <= 0:
            return
        # 未知のエンジンも記録は残す（料金は「単価未確認」で出る＝0 ドルに見せて隠さない）
        provider = api_pricing.provider_for_engine(engine_id) or engine_id
        row = _new_row(provider, model, purpose)
        row["requests"] = 1
        row["audio_seconds"] = seconds
        row["billed_audio_seconds"] = api_pricing.billed_seconds(provider, model, seconds)
        self._add(row, now)

    def record_llm(
        self,
        provider: str,
        model: str,
        purpose: str,
        input_tokens: int,
        output_tokens: int,
        now: Optional[datetime] = None,
    ) -> None:
        """
        LLM 1 リクエストを記録する（トークン数は応答の usage から）。

        Args:
            provider: 課金元（例 "groq"）
            model: モデル ID
            purpose: 用途（例 "formatting"）
            input_tokens: 入力トークン数
            output_tokens: 出力トークン数
            now: 記録日時（テスト用。省略時は現在時刻）
        """
        row = _new_row(provider, model, purpose)
        row["requests"] = 1
        row["input_tokens"] = max(0, int(input_tokens))
        row["output_tokens"] = max(0, int(output_tokens))
        self._add(row, now)

    def record_chat_usage(
        self, response: dict, provider: str, model: str, purpose: str,
        now: Optional[datetime] = None,
    ) -> bool:
        """
        OpenAI 互換のチャット応答（Groq）の usage を読んで記録する。

        通常の応答は `usage`、Groq のストリーミング最終チャンクは `x_groq.usage` に入るので両方を見る。
        本文（choices）には触れない。

        Returns:
            usage が見つかって記録したら True
        """
        if not isinstance(response, dict):
            return False
        usage = response.get("usage")
        if not isinstance(usage, dict):
            x_groq = response.get("x_groq")
            usage = x_groq.get("usage") if isinstance(x_groq, dict) else None
        if not isinstance(usage, dict):
            return False
        prompt = usage.get("prompt_tokens")
        completion = usage.get("completion_tokens")
        if prompt is None and completion is None:
            return False
        self.record_llm(provider, model, purpose, prompt or 0, completion or 0, now=now)
        return True

    def _add(self, row: dict, now: Optional[datetime]) -> None:
        day = _day_string(now or _now())
        with self._lock:
            rows = self._days.setdefault(day, [])
            for existing in rows:
                if _same_key(existing, row):
                    _add_into(existing, row)
                    break
            else:
                rows.append(row)
            self._save()

    # ----- 集計（表示用） -----

    def summary(self, include: Callable[[str], bool], mode: str = api_pricing.MODE_LIST) -> dict:
        """
        日付キーが include を満たす行の合計。

        Returns:
            {"usd": float, "has_unpriced": bool（単価未確認の行を含む）, "requests": int}
        """
        with self._lock:
            rows = [r for day, rs in self._days.items() if include(day) for r in rs]
        return self._summarize(rows, mode)

    def today_summary(self, mode: str = api_pricing.MODE_LIST, now: Optional[datetime] = None) -> dict:
        today = _day_string(now or _now())
        return self.summary(lambda d: d == today, mode)

    def month_summary(self, mode: str = api_pricing.MODE_LIST, now: Optional[datetime] = None) -> dict:
        month = _day_string(now or _now())[:7]
        return self.summary(lambda d: d.startswith(month), mode)

    def all_time_summary(self, mode: str = api_pricing.MODE_LIST) -> dict:
        return self.summary(lambda d: True, mode)

    def breakdown(self, include: Callable[[str], bool], mode: str = api_pricing.MODE_LIST) -> List[dict]:
        """
        プロバイダー × モデル × 用途の内訳（料金の高い順。単価未確認は末尾）。

        Returns:
            集計行のリスト。各行に "usd"（None = 単価未確認）・"provider_label"・"purpose_label" を足す
        """
        merged: List[dict] = []
        with self._lock:
            for day, rows in self._days.items():
                if not include(day):
                    continue
                for r in rows:
                    for m in merged:
                        if _same_key(m, r):
                            _add_into(m, r)
                            break
                    else:
                        merged.append(dict(r))
        for m in merged:
            m["usd"] = row_cost(m, mode)
            m["provider_label"] = api_pricing.PROVIDER_LABELS.get(m["provider"], m["provider"])
            m["purpose_label"] = api_pricing.PURPOSE_LABELS.get(m["purpose"], m["purpose"])
        merged.sort(key=lambda m: -1 if m["usd"] is None else m["usd"], reverse=True)
        return merged

    def daily_series(
        self, num_days: int, mode: str = api_pricing.MODE_LIST, end_day: Optional[date] = None,
    ) -> List[dict]:
        """
        直近 num_days 日（end_day=既定は今日を末尾・古い順）。記録の無い日は 0。

        Returns:
            {"day": "yyyy-MM-dd", "usd", "has_unpriced", "requests"} のリスト
        """
        end = end_day or _now().date()
        with self._lock:
            days = {k: list(v) for k, v in self._days.items()}
        out = []
        for i in range(max(0, num_days) - 1, -1, -1):
            key = (end - timedelta(days=i)).strftime("%Y-%m-%d")
            s = self._summarize(days.get(key, []), mode)
            s["day"] = key
            out.append(s)
        return out

    def snapshot(
        self,
        mode: str = api_pricing.MODE_LIST,
        period: str = "month",
        now: Optional[datetime] = None,
        num_days: int = 30,
    ) -> dict:
        """
        ホーム画面の「API の利用料金」に出す値をまとめて返す。

        Args:
            mode: api_pricing.MODE_LIST（定価）か MODE_PAID（実際に払った分）
            period: 内訳の期間。"today" / "month" / "all"（Mac のホームの切り替えと同じ）
            now: 基準日時（テスト用。省略時は現在時刻）
            num_days: 推移の日数（Mac のホームは直近 30 日）

        Returns:
            {"today", "month", "all_time"（各 summary）, "breakdown"（period の内訳）,
             "daily"（推移）, "checked_on"（単価の確認日）}
        """
        now = now or _now()
        today = _day_string(now)
        if period == "today":
            include = lambda d: d == today  # noqa: E731
        elif period == "all":
            include = lambda d: True  # noqa: E731
        else:
            include = lambda d: d.startswith(today[:7])  # noqa: E731
        return {
            "today": self.today_summary(mode, now),
            "month": self.month_summary(mode, now),
            "all_time": self.all_time_summary(mode),
            "breakdown": self.breakdown(include, mode),
            "daily": self.daily_series(num_days, mode, now.date()),
            "checked_on": api_pricing.CHECKED_ON,
        }

    @staticmethod
    def _summarize(rows: List[dict], mode: str) -> dict:
        s = {"usd": 0.0, "has_unpriced": False, "requests": 0}
        for r in rows:
            s["requests"] += r["requests"]
            c = row_cost(r, mode)
            if c is None:
                s["has_unpriced"] = True
            else:
                s["usd"] += c
        return s

    # ----- 永続化 -----

    def _load(self) -> Dict[str, List[dict]]:
        """保存済みの使用量を読み込む。壊れていれば退避して空で開始する（クラッシュさせない）。"""
        if not self._path.exists():
            return {}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or not isinstance(raw.get("days", {}), dict):
                raise ValueError("形式が違います")
            return self._clean_days(raw.get("days", {}))
        except Exception as e:
            logger.warning(f"API 使用量の読み込みに失敗（退避して空で開始します）: {e}")
            self._quarantine()
            return {}

    def _quarantine(self) -> None:
        """壊れたファイルを別名へ退避する。退避できなければ以後の保存を止めて元ファイルを守る。"""
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        dest = self._path.with_name(f"{self._path.name}.corrupt-{stamp}")
        try:
            self._path.replace(dest)
            logger.warning(f"壊れた API 使用量ファイルを退避しました: {dest}")
        except OSError as e:
            logger.error(f"壊れた API 使用量ファイルを退避できないため保存を止めます: {e}")
            self._save_blocked = True

    @staticmethod
    def _clean_days(raw_days: dict) -> Dict[str, List[dict]]:
        """保存済みの日次辞書を読めるものだけに整形する（壊れた行は捨てる）。"""
        days: Dict[str, List[dict]] = {}
        for day, rows in raw_days.items():
            if not isinstance(rows, list):
                continue
            clean = []
            for r in rows:
                if not isinstance(r, dict):
                    continue
                try:
                    row = _new_row(str(r.get("provider", "")), str(r.get("model", "")), str(r.get("purpose", "")))
                    for k in _INT_FIELDS:
                        row[k] = int(r.get(k, 0))
                    for k in _FLOAT_FIELDS:
                        row[k] = float(r.get(k, 0.0))
                except (TypeError, ValueError):
                    continue
                clean.append(row)
            if clean:
                days[str(day)] = clean
        return days

    def _save(self) -> None:
        """使用量をファイルに保存する（_lock 保持中に呼ぶこと）。"""
        if self._save_blocked:
            return
        try:
            # 書き込み途中のクラッシュでファイルが壊れないよう一時ファイル経由で置換する
            tmp = self._path.with_name(self._path.name + ".tmp")
            tmp.write_text(
                json.dumps({"days": self._days}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(self._path)
        except Exception as e:
            logger.error(f"API 使用量の保存に失敗: {e}")
