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
        # アカウント連携（#10）。既定 OFF。実アプリ起動時に app.py が
        # enable_account_sync() で有効化する。ユニットテストでは呼ばれないため、
        # record_session/起動で keyring・ネットワークに触れない（テスト時のパスワード
        # ダイアログを防ぐ＝lessons 2026-06-12）。Mac は署名アプリの Keychain 読取が
        # 無プロンプトなので StatsStore.init で登録するが、機能としては等価。
        self._account_sync_enabled = False
        self._data = self._load()

    def enable_account_sync(self) -> None:
        """アカウント連携を有効化する（実アプリ起動時に一度だけ app.py から呼ぶ）。

        ログイン/ログアウトに追従して端末横断の実績を取り込む/破棄するための
        観測者を登録し、すでにログイン済みなら起動時同期をバックグラウンドで行う。
        """
        if self._account_sync_enabled:
            return
        self._account_sync_enabled = True
        from . import login_coordinator

        login_coordinator.add_login_observer(self.sync_on_login)
        login_coordinator.add_logout_observer(self.clear_account)
        # すでにログイン済みで起動した場合は、起動時に一度だけ同期する（UI を止めない）
        threading.Thread(target=self._sync_on_login_if_logged_in, daemon=True).start()

    def _sync_on_login_if_logged_in(self) -> None:
        """ログイン済みなら起動時同期を行う（バックグラウンドスレッドから呼ぶ）。"""
        from . import backend_client

        try:
            if backend_client.is_logged_in():
                self.sync_on_login()
        except Exception as e:  # 起動時同期の失敗は致命的でない
            logger.debug(f"起動時の実績同期に失敗（無視）: {e}")

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
        # ログイン中なら当日分（この端末の絶対値）をサーバーへ送る（音声経路には乗らない）
        self.sync_today_in_background()

    def reset(self) -> None:
        """実績をすべてリセットして保存する（テスト用・ユーザーが消したいとき用）。"""
        with self._lock:
            self._data = self._empty()
            self._save()

    def snapshot(self) -> dict:
        """UI 表示用に派生値（レベル・進捗）込みのコピーを返す。

        ログイン中（account_daily 取り込み済み）は累計・節約時間・連続日数を端末横断で
        算出する。古い実績で下がらないよう、いずれもローカル値を下限にする。未ログインは
        従来どおりローカル値。UI は本メソッドのキーだけ読むので UI 改修は不要。
        """
        with self._lock:
            d = dict(self._data)
            eff = self._effective_daily_locked()
        logged_in = d.get("account_daily") is not None
        # アカウント対応の累計（ローカルを下限にする）
        total_chars = max(int(d["total_characters"]), int(d.get("account_total_characters") or 0))
        total_sessions = max(int(d["total_sessions"]), int(d.get("account_total_sessions") or 0))
        total_rec = max(
            float(d["total_recording_seconds"]),
            float(d.get("account_total_recording_seconds") or 0.0),
        )
        if logged_in:
            saved = max(float(d["saved_seconds"]), self._saved_from_daily(eff))
            cur_streak = max(self._current_streak_from(eff), int(d["current_streak"]))
            long_streak = max(self._longest_streak_from(eff), int(d["longest_streak"]))
        else:
            saved = float(d["saved_seconds"])
            cur_streak = int(d["current_streak"])
            long_streak = int(d["longest_streak"])
        d["total_characters"] = total_chars
        d["total_sessions"] = total_sessions
        d["total_recording_seconds"] = total_rec
        d["saved_seconds"] = saved
        d["current_streak"] = cur_streak
        d["longest_streak"] = long_streak
        xp = total_chars
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
            daily = self._effective_daily_locked()
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
            daily = self._effective_daily_locked()
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

    # MARK: - アカウント連携（#10）

    def _effective_daily_locked(self) -> dict:
        """ローカル daily に account_daily を field-wise max で重ねた辞書を返す。

        ログイン中はサーバー合算（端末横断）にこの端末の未送信分を max で重ねる
        ＝二重計上せずに最新も拾う。未ログイン（account_daily=None）はローカルのみ。
        _lock 保持中に呼ぶこと。
        """
        local = self._data.get("daily", {}) or {}
        acc = self._data.get("account_daily")
        if not acc:
            return {k: dict(v) for k, v in local.items()}
        out = {k: dict(v) for k, v in acc.items()}
        for key, lv in local.items():
            m = out.get(key) or {"characters": 0, "recording_seconds": 0.0, "sessions": 0}
            m["characters"] = max(int(m.get("characters", 0)), int(lv.get("characters", 0)))
            m["recording_seconds"] = max(
                float(m.get("recording_seconds", 0.0)), float(lv.get("recording_seconds", 0.0))
            )
            m["sessions"] = max(int(m.get("sessions", 0)), int(lv.get("sessions", 0)))
            out[key] = m
        return out

    @staticmethod
    def _saved_from_daily(daily: dict) -> float:
        """日次バケットから推定節約秒を復元する（サーバーは節約秒を持たないため）。"""
        total = 0.0
        for b in daily.values():
            typing = int(b.get("characters", 0)) / ASSUMED_TYPING_CHARS_PER_SECOND
            total += max(0.0, typing - float(b.get("recording_seconds", 0.0)))
        return total

    @staticmethod
    def _active_days(daily: dict) -> set:
        """利用のあった日付キー集合（文字 or 回数が正の日）。"""
        return {
            k
            for k, b in daily.items()
            if int(b.get("characters", 0)) > 0 or int(b.get("sessions", 0)) > 0
        }

    @classmethod
    def _current_streak_from(cls, daily: dict) -> int:
        """日付集合から現在の連続利用日数を算出する。今日未記録でも昨日まで継続なら途切れない。"""
        days = cls._active_days(daily)
        if not days:
            return 0
        cursor = datetime.now().astimezone().date()
        if cursor.strftime("%Y-%m-%d") not in days:
            cursor = cursor - timedelta(days=1)
        streak = 0
        while cursor.strftime("%Y-%m-%d") in days:
            streak += 1
            cursor = cursor - timedelta(days=1)
        return streak

    @classmethod
    def _longest_streak_from(cls, daily: dict) -> int:
        """日付集合から最長連続利用日数を算出する。"""
        keys = sorted(cls._active_days(daily))
        if not keys:
            return 0
        longest = run = 1
        prev = datetime.strptime(keys[0], "%Y-%m-%d").date()
        for k in keys[1:]:
            cur = datetime.strptime(k, "%Y-%m-%d").date()
            run = run + 1 if (cur - prev).days == 1 else 1
            longest = max(longest, run)
            prev = cur
        return longest

    def _recent_own_days(self, num_days: int) -> list:
        """この端末で記録した直近 num_days 日分を送信形（絶対値）で返す（記録の無い日は除外）。"""
        now = datetime.now().astimezone()
        with self._lock:
            daily = dict(self._data.get("daily", {}))
        out = []
        for i in range(max(0, num_days)):
            key = _day_string(now - timedelta(days=i))
            b = daily.get(key)
            if not b:
                continue
            out.append({
                "day": key,
                "chars": int(b.get("characters", 0)),
                "sessions": int(b.get("sessions", 0)),
                "duration_ms": int(round(float(b.get("recording_seconds", 0.0)) * 1000)),
            })
        return out

    def sync_today_in_background(self) -> None:
        """当日分（この端末の絶対値）をサーバーへ送る。連携 ON かつログイン中のみ・撃ちっぱなし。

        録音のたびに呼ばれるが、(user_id,device_id,day) の絶対値 upsert なので冪等。
        連携 OFF（テスト等）のときは keyring/ネットワークに一切触れず即 return する。
        """
        if not self._account_sync_enabled:
            return
        from . import backend_client

        try:
            if not backend_client.is_logged_in():
                return
        except Exception:
            return
        key = _day_string(datetime.now().astimezone())
        with self._lock:
            b = (self._data.get("daily") or {}).get(key)
            if not b:
                return
            payload = {
                "day": key,
                "chars": int(b.get("characters", 0)),
                "sessions": int(b.get("sessions", 0)),
                "duration_ms": int(round(float(b.get("recording_seconds", 0.0)) * 1000)),
            }

        def _run() -> None:
            try:
                backend_client.sync_stats([payload])
            except Exception as e:  # 同期失敗は実績表示に影響しない
                logger.debug(f"実績の同期に失敗（無視）: {e}")

        threading.Thread(target=_run, daemon=True).start()

    def sync_on_login(self) -> None:
        """ログイン直後/起動時に呼ぶ。まずこの端末の直近分を押し上げ、続いて端末横断の集計を取り込む。

        ネットワーク I/O を伴うため、ワーカースレッド（ログイン処理スレッド等）から呼ぶこと。
        """
        from . import backend_client

        try:
            recent = self._recent_own_days(60)
            if recent:
                backend_client.sync_stats(recent)
        except Exception as e:  # 送信失敗でも取り込みは試みる
            logger.debug(f"ログイン時の実績送信に失敗（無視）: {e}")
        self.refresh_account()

    def refresh_account(self) -> None:
        """端末横断の集計をサーバーから取り込む（account_* を更新して永続化）。"""
        from . import backend_client

        try:
            snap = backend_client.fetch_stats()
        except Exception as e:
            logger.debug(f"端末横断実績の取得に失敗（無視）: {e}")
            return
        with self._lock:
            self._data["account_daily"] = snap.get("daily") or {}
            self._data["account_total_characters"] = int(snap.get("total_characters") or 0)
            self._data["account_total_sessions"] = int(snap.get("total_sessions") or 0)
            self._data["account_total_recording_seconds"] = float(
                snap.get("total_recording_seconds") or 0.0
            )
            self._save()

    def clear_account(self) -> None:
        """ログアウト時に端末横断の取り込み結果を破棄し、ローカル表示へ戻す。"""
        with self._lock:
            self._data["account_daily"] = None
            self._data["account_total_characters"] = None
            self._data["account_total_sessions"] = None
            self._data["account_total_recording_seconds"] = None
            self._save()

    @staticmethod
    def _clean_daily(raw_daily: dict) -> dict:
        """保存済みの日次辞書を読めるものだけに整形する（壊れた要素は捨てる）。"""
        clean = {}
        for key, value in raw_daily.items():
            if isinstance(value, dict):
                clean[str(key)] = {
                    "characters": int(value.get("characters", 0)),
                    "recording_seconds": float(value.get("recording_seconds", 0.0)),
                    "sessions": int(value.get("sessions", 0)),
                }
        return clean

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
            # ★これは「この端末で記録した分」だけ＝サーバーへ送る送信元。
            "daily": {},
            # アカウント連携（#10。ログイン中のみ・端末横断の取り込み結果。None=未取り込み）
            "account_daily": None,
            "account_total_characters": None,
            "account_total_sessions": None,
            "account_total_recording_seconds": None,
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
                        data["daily"] = self._clean_daily(raw_daily)
                    # アカウント連携の取り込み結果（あれば復元。再起動後も表示が消えない）
                    raw_acc = raw.get("account_daily")
                    if isinstance(raw_acc, dict):
                        data["account_daily"] = self._clean_daily(raw_acc)
                    atc = raw.get("account_total_characters")
                    data["account_total_characters"] = int(atc) if atc is not None else None
                    ats = raw.get("account_total_sessions")
                    data["account_total_sessions"] = int(ats) if ats is not None else None
                    atr = raw.get("account_total_recording_seconds")
                    data["account_total_recording_seconds"] = float(atr) if atr is not None else None
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
