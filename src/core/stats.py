"""
使用実績モジュール

音声入力 1 回ごとに「文字数・録音秒数・日付」を積み上げ、累計の
推定節約時間・レベル・連続利用日数を計算して設定ウィンドウの「実績」タブに見せる。
すべて貼り付け確定「後」のローカル集計なので、音声→テキストの遅延には一切影響しない。
アプリを再起動しても残るよう settings.yaml と同じディレクトリに JSON で保存する。
Mac 版 StatsStore.swift と計算式・しきい値を一致させること。
"""

import json
import math
import sys
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from ..utils.logger import get_logger

logger = get_logger(__name__)

STATS_FILE_NAME = "stats.json"

# タイピング速度の仮定（文字/秒）。節約時間 = 文字数 / これ − 録音秒数。
# 過大表示を避けるため速め（240 字/分）に置く＝これより速く打てる人は節約が控えめに出る。
# Mac 版 StatsStore.assumedTypingCharsPerSecond と同値。
ASSUMED_TYPING_CHARS_PER_SECOND = 4.0


def _default_path() -> Path:
    """実績ファイルの既定パス（settings.yaml / history.json と同じ配置ロジック）。"""
    if getattr(sys, "frozen", False):
        # PyInstaller 実行時は実行ファイルと同じディレクトリ
        base_dir = Path(sys.executable).parent
    else:
        # 開発時はプロジェクトルート
        base_dir = Path(__file__).parent.parent.parent
    return base_dir / STATS_FILE_NAME


def _day_string(dt: datetime) -> str:
    """ローカルタイムの yyyy-MM-dd（連続日数の判定キー）。"""
    return dt.strftime("%Y-%m-%d")


def threshold(level: int) -> int:
    """レベル L に到達するのに必要な累計 XP（= 250*(L-1)*L）。Mac 版と一致。"""
    L = max(0, level)
    return 250 * max(0, L - 1) * L


def level_for_xp(xp: int) -> int:
    """累計 XP から現在レベルを逆算する（threshold(L) <= xp を満たす最大の L）。"""
    x = max(0, xp)
    inner = 1.0 + 4.0 * x / 250.0
    lvl = int((1.0 + math.sqrt(inner)) / 2.0)
    return max(1, lvl)


class StatsStore:
    """
    使用実績のストア（スレッドセーフ）。

    record_session() は文字起こしワーカースレッドから、snapshot()/reset() は
    UI スレッドから呼ばれるため、内部状態はロックで保護する。
    """

    def __init__(self, file_path: Optional[Path] = None) -> None:
        self._path = Path(file_path) if file_path else _default_path()
        self._lock = threading.Lock()
        self._data = self._load()

    def record_session(self, characters: int, recording_seconds: float) -> None:
        """音声入力 1 回分を記録して保存する（貼り付け確定後に呼ぶ）。空入力は無視。"""
        if characters <= 0:
            return
        now = datetime.now().astimezone()
        rec_sec = max(0.0, float(recording_seconds))
        # 推定節約 = 手入力にかかる時間 − 実際の発話時間（マイナスは 0 に丸める）
        typing_seconds = characters / ASSUMED_TYPING_CHARS_PER_SECOND
        with self._lock:
            if not self._data.get("first_use_date"):
                self._data["first_use_date"] = now.isoformat(timespec="seconds")
            self._data["total_sessions"] += 1
            self._data["total_characters"] += characters
            self._data["total_recording_seconds"] += rec_sec
            self._data["saved_seconds"] += max(0.0, typing_seconds - rec_sec)
            self._update_streak(now)
            self._record_daily(now, characters, rec_sec)
            self._save()

    def reset(self) -> None:
        """実績をすべてリセットして保存する（テスト用・ユーザーが消したいとき用）。"""
        with self._lock:
            self._data = self._empty()
            self._save()

    def snapshot(self) -> dict:
        """UI 表示用に派生値（レベル・進捗）込みのコピーを返す。"""
        with self._lock:
            d = dict(self._data)
        xp = int(d["total_characters"])
        lvl = level_for_xp(xp)
        base = threshold(lvl)
        nxt = threshold(lvl + 1)
        progress = 0.0 if nxt <= base else min(1.0, max(0.0, (xp - base) / (nxt - base)))
        d["xp"] = xp
        d["level"] = lvl
        d["level_progress"] = progress
        d["xp_to_next_level"] = max(0, nxt - xp)
        return d

    def _update_streak(self, now: datetime) -> None:
        """連続利用日数を更新する。同日2回目は据え置き、前日からの継続で +1、空きが出たら 1。"""
        today = _day_string(now)
        if self._data.get("last_used_day") == today:
            return
        yesterday = _day_string(now - timedelta(days=1))
        if self._data.get("last_used_day") == yesterday:
            self._data["current_streak"] += 1
        else:
            self._data["current_streak"] = 1
        self._data["last_used_day"] = today
        self._data["longest_streak"] = max(
            int(self._data.get("longest_streak", 0)), self._data["current_streak"]
        )

    def _record_daily(self, now: datetime, characters: int, rec_sec: float) -> None:
        """日付ごとのバケット（文字数・録音秒・回数）を積み増す。チャート表示用。"""
        day = _day_string(now)
        daily = self._data.setdefault("daily", {})
        bucket = daily.setdefault(
            day, {"characters": 0, "recording_seconds": 0.0, "sessions": 0}
        )
        bucket["characters"] += characters
        bucket["recording_seconds"] += rec_sec
        bucket["sessions"] += 1
        self._prune_daily()

    def _prune_daily(self) -> None:
        """日次バケットが増えすぎないよう、古い日から落として直近 800 日に保つ。"""
        daily = self._data.get("daily", {})
        if len(daily) > 800:
            for key in sorted(daily.keys())[:-800]:
                del daily[key]

    def daily_series(self, num_days: int, end_day: Optional[str] = None) -> list:
        """末尾を end_day（既定=今日）として直近 num_days 日分を古い順で返す（空の日は 0 埋め）。

        各要素: {"day": "yyyy-MM-dd", "characters": int, "recording_seconds": float, "sessions": int}
        """
        if end_day:
            end = datetime.strptime(end_day, "%Y-%m-%d").date()
        else:
            end = datetime.now().astimezone().date()
        with self._lock:
            daily = dict(self._data.get("daily", {}))
        out = []
        for i in range(max(0, num_days) - 1, -1, -1):
            d = end - timedelta(days=i)
            key = d.strftime("%Y-%m-%d")
            b = daily.get(key) or {}
            out.append({
                "day": key,
                "characters": int(b.get("characters", 0)),
                "recording_seconds": float(b.get("recording_seconds", 0.0)),
                "sessions": int(b.get("sessions", 0)),
            })
        return out

    def monthly_series(self, num_months: int, end_month: Optional[str] = None) -> list:
        """末尾を end_month（既定=今月）として直近 num_months ヶ月分を古い順で返す（空月は 0 埋め）。

        各要素: {"month": "yyyy-MM", "characters": int, "recording_seconds": float, "sessions": int}
        """
        if end_month:
            ey, em = int(end_month[:4]), int(end_month[5:7])
        else:
            today = datetime.now().astimezone().date()
            ey, em = today.year, today.month
        with self._lock:
            daily = dict(self._data.get("daily", {}))
        # 日次バケットを月キー（yyyy-MM）へ集約する
        agg: dict = {}
        for key, b in daily.items():
            mk = str(key)[:7]
            a = agg.setdefault(mk, {"characters": 0, "recording_seconds": 0.0, "sessions": 0})
            a["characters"] += int(b.get("characters", 0))
            a["recording_seconds"] += float(b.get("recording_seconds", 0.0))
            a["sessions"] += int(b.get("sessions", 0))
        out = []
        end_index = ey * 12 + (em - 1)  # 年月を 0 起点の通し番号にして月の引き算を安全に行う
        for i in range(max(0, num_months) - 1, -1, -1):
            j = end_index - i
            y, m0 = divmod(j, 12)
            mk = f"{y:04d}-{m0 + 1:02d}"
            a = agg.get(mk) or {}
            out.append({
                "month": mk,
                "characters": int(a.get("characters", 0)),
                "recording_seconds": float(a.get("recording_seconds", 0.0)),
                "sessions": int(a.get("sessions", 0)),
            })
        return out

    @staticmethod
    def _empty() -> dict:
        return {
            "total_sessions": 0,
            "total_characters": 0,
            "total_recording_seconds": 0.0,
            "saved_seconds": 0.0,
            "first_use_date": "",
            "last_used_day": "",
            "current_streak": 0,
            "longest_streak": 0,
            # 日付ごとの入力量（チャート用）。{"yyyy-MM-dd": {characters, recording_seconds, sessions}}
            "daily": {},
        }

    def _load(self) -> dict:
        """保存済みの実績を読み込む。壊れていれば 0 から開始する（クラッシュさせない）。"""
        data = self._empty()
        try:
            if self._path.exists():
                raw = json.loads(self._path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    data["total_sessions"] = int(raw.get("total_sessions", 0))
                    data["total_characters"] = int(raw.get("total_characters", 0))
                    data["total_recording_seconds"] = float(raw.get("total_recording_seconds", 0.0))
                    data["saved_seconds"] = float(raw.get("saved_seconds", 0.0))
                    data["first_use_date"] = str(raw.get("first_use_date", "") or "")
                    data["last_used_day"] = str(raw.get("last_used_day", "") or "")
                    data["current_streak"] = int(raw.get("current_streak", 0))
                    data["longest_streak"] = int(raw.get("longest_streak", 0))
                    # 日次バケット（壊れた要素は捨てて読めるものだけ残す）
                    raw_daily = raw.get("daily", {})
                    if isinstance(raw_daily, dict):
                        clean = {}
                        for key, value in raw_daily.items():
                            if isinstance(value, dict):
                                clean[str(key)] = {
                                    "characters": int(value.get("characters", 0)),
                                    "recording_seconds": float(value.get("recording_seconds", 0.0)),
                                    "sessions": int(value.get("sessions", 0)),
                                }
                        data["daily"] = clean
        except Exception as e:
            logger.warning(f"実績の読み込みに失敗（0 から開始します）: {e}")
        return data

    def _save(self) -> None:
        """実績をファイルに保存する（_lock 保持中に呼ぶこと）。"""
        try:
            # 書き込み途中のクラッシュでファイルが壊れないよう一時ファイル経由で置換する
            tmp = self._path.with_name(self._path.name + ".tmp")
            tmp.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(self._path)
        except Exception as e:
            logger.error(f"実績の保存に失敗: {e}")
