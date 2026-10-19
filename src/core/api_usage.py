"""
API の利用料金（単価表・使用量の日次集計・料金の計算）— Mac 版 ApiPricing.swift / ApiUsageStore.swift の移植

API を呼ぶたびに「日付・プロバイダー・モデル・用途・数量」を積み上げ、料金は表示のたびに単価表から
計算する（単価を直せば過去の使用量にもそのまま反映される）。保存するのは数量だけ（音声の秒数・
トークン数・回数）で、発話本文や API キーは受け取らない。公式ページで確かめられなかった単価は
推測で埋めず「単価未確認」にする（0 円に見せて隠さない）。

記録はどのスレッドからでも同期で呼べる（ロックで守る）。音声入力の経路から呼ばれるので待ちは足さない:
加算はメモリ上で一瞬、ファイル保存は裏のスレッドで後回しにする。
保存先は settings.yaml と同じ場所の `api_usage.json`（Mac は `api-usage.json`。形は同じ）。
"""

import json
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..utils import corrupt_file
from ..utils.logger import get_logger
from ..utils.paths import data_dir

logger = get_logger(__name__)

FILE_NAME = "api_usage.json"

# ----------------------------------------------------------------------
# プロバイダー・用途
# ----------------------------------------------------------------------

PROVIDER_LABELS: Dict[str, str] = {
    "soniox": "Soniox",
    "openai": "OpenAI",
    "microsoft": "Microsoft",
    "elevenlabs": "ElevenLabs",
    "groq": "Groq",
    "gemini": "Gemini",
    "deepgram": "Deepgram",
}

# 文字起こしのエンジン識別子 → 課金元（OpenAI ライブも請求は OpenAI）
_BACKEND_PROVIDERS: Dict[str, str] = {
    "openai": "openai",
    "openai_live": "openai",
    "groq": "groq",
    "elevenlabs": "elevenlabs",
    "deepgram": "deepgram",
    "soniox": "soniox",
    "azure_mai": "microsoft",
    "gemini": "gemini",
}

PURPOSE_TRANSCRIPTION = "transcription"
PURPOSE_FORMATTING = "formatting"
PURPOSE_TRANSLATION = "translation"

PURPOSE_LABELS: Dict[str, str] = {
    PURPOSE_TRANSCRIPTION: "文字起こし",
    PURPOSE_FORMATTING: "テキスト整形",
    PURPOSE_TRANSLATION: "翻訳して入力",
    "captionTranslation": "字幕の翻訳",
}


def provider_for(backend_or_provider: str) -> str:
    """エンジン識別子（openai_live 等）か課金元名を、課金元名にそろえる。"""
    key = (backend_or_provider or "").lower()
    return _BACKEND_PROVIDERS.get(key, key)


# ----------------------------------------------------------------------
# 単価表（Mac の ApiPricing.swift と同じ値・同じ確認日）
# ----------------------------------------------------------------------

# 単価を公式ページで確認した日（画面の注記に出す）
CHECKED_ON = "2026-10-02"

# 本人が実際にお金を払っている契約のあるプロバイダー（「実際に払った分」表示の対象・Mac と同じ）
PAID_PROVIDERS = frozenset({"openai", "soniox", "microsoft", "gemini"})

# 単価: ("audio", USD/時間, 1 リクエストの最低課金秒数) か ("tokens", 入力 USD/100 万, 出力 USD/100 万)
_TABLE: Dict[str, tuple] = {
    # Soniox リアルタイム $0.12/時間 出典 https://soniox.com/pricing（2026-10-02 確認）
    "soniox/stt-rt-v5": ("audio", 0.12, 0.0),
    # OpenAI gpt-transcribe $0.0045/分 出典 https://developers.openai.com/api/docs/pricing（2026-10-02 確認）
    "openai/gpt-transcribe": ("audio", 0.0045 * 60, 0.0),
    # OpenAI gpt-live-transcribe（Realtime）$0.017/分（同上）
    "openai/gpt-live-transcribe": ("audio", 0.017 * 60, 0.0),
    # Microsoft MAI-Transcribe-2 $0.10/時間（2026 年末までの期間限定価格・2026-10-02 確認）
    "microsoft/mai-transcribe-2": ("audio", 0.10, 0.0),
    # ElevenLabs Scribe v2 $0.22/時間（scribe_v1 は料金ページに無いので未確認のまま）
    "elevenlabs/scribe_v2": ("audio", 0.22, 0.0),
    # Groq whisper-large-v3-turbo $0.04/時間・1 リクエスト最低 10 秒課金
    "groq/whisper-large-v3-turbo": ("audio", 0.04, 10.0),
    # Groq LLM（llama 系は 2026-10 に API から廃止＝表に載せない）
    "groq/openai/gpt-oss-20b": ("tokens", 0.075, 0.30),
    "groq/openai/gpt-oss-120b": ("tokens", 0.15, 0.60),
    "groq/qwen/qwen3.8-27b": ("tokens", 0.80, 4.00),
    # Gemini 3.5 Flash-Lite 有料枠 入力 $0.30 / 出力 $2.50（出力は思考トークン込み）
    "gemini/gemini-3.5-flash-lite": ("tokens", 0.30, 2.50),
}


def price(provider: str, model: str) -> Optional[tuple]:
    """単価を引く（None = 単価未確認）。"""
    return _TABLE.get(f"{provider}/{(model or '').lower()}")


def billed_seconds(provider: str, model: str, audio_seconds: float) -> float:
    """1 リクエストぶんの課金対象秒数（最低課金秒数を適用）。単価未確認なら実秒数のまま。"""
    seconds = max(0.0, float(audio_seconds))
    p = price(provider, model)
    if seconds <= 0 or p is None or p[0] != "audio":
        return seconds
    return max(seconds, p[2])


def cost(provider: str, model: str, billed_audio_seconds: float, input_tokens: int, output_tokens: int) -> Optional[float]:
    """数量から料金（USD）を計算する（None = 単価未確認）。"""
    p = price(provider, model)
    if p is None:
        return None
    if p[0] == "audio":
        return billed_audio_seconds / 3600.0 * p[1]
    return input_tokens / 1_000_000 * p[1] + output_tokens / 1_000_000 * p[2]


def formatted_usd(usd: float) -> str:
    """「$0.12」形式の表示（1 セント未満は桁を増やして 0 に潰さない）。"""
    if 0 < usd < 0.01:
        return f"${usd:.4f}"
    return f"${usd:.2f}"


MODE_LIST = "list"   # 定価（無料枠で使った分も単価どおりに計上）
MODE_PAID = "paid"   # 実際に払った分（有料契約のプロバイダーだけ計上。それ以外は無料枠として 0 円）


def is_free_tier(mode: str, provider: str) -> bool:
    """このモードで無料枠として扱う（0 円に落とす）プロバイダーか。"""
    return mode == MODE_PAID and provider not in PAID_PROVIDERS


# ----------------------------------------------------------------------
# 集計 1 行・合計
# ----------------------------------------------------------------------

@dataclass
class UsageEntry:
    """1 日 × プロバイダー × モデル × 用途 の集計 1 行（Mac の ApiUsageEntry と同じ項目名で保存する）。"""

    provider: str
    model: str
    purpose: str
    requests: int = 0
    audioSeconds: float = 0.0
    billedAudioSeconds: float = 0.0
    inputTokens: int = 0
    outputTokens: int = 0
    estimated: bool = False

    def same_key(self, other: "UsageEntry") -> bool:
        return (self.provider, self.model, self.purpose, self.estimated) == (
            other.provider, other.model, other.purpose, other.estimated)

    def add(self, other: "UsageEntry") -> None:
        self.requests += other.requests
        self.audioSeconds += other.audioSeconds
        self.billedAudioSeconds += other.billedAudioSeconds
        self.inputTokens += other.inputTokens
        self.outputTokens += other.outputTokens

    def cost_usd(self, mode: str) -> Optional[float]:
        """料金（USD）。None = 単価未確認。「実際に払った分」で無料枠のプロバイダーは 0。"""
        if is_free_tier(mode, self.provider):
            return 0.0
        return cost(self.provider, self.model, self.billedAudioSeconds, self.inputTokens, self.outputTokens)

    @staticmethod
    def from_dict(data: dict) -> "UsageEntry":
        """後から項目を増やしても古い JSON を読めるよう、無い項目は既定値で読む。"""
        return UsageEntry(
            provider=str(data.get("provider", "")),
            model=str(data.get("model", "")),
            purpose=str(data.get("purpose", "")),
            requests=int(data.get("requests", 0) or 0),
            audioSeconds=float(data.get("audioSeconds", 0) or 0),
            billedAudioSeconds=float(data.get("billedAudioSeconds", 0) or 0),
            inputTokens=int(data.get("inputTokens", 0) or 0),
            outputTokens=int(data.get("outputTokens", 0) or 0),
            estimated=bool(data.get("estimated", False)),
        )


@dataclass
class CostSummary:
    """料金の合計（単価未確認の行を含むかも持つ＝0 円に見せて隠さないため）。"""

    usd: float = 0.0
    has_unpriced: bool = False
    requests: int = 0
    rows: List[UsageEntry] = field(default_factory=list)


def day_string(when: Optional[date] = None) -> str:
    """ローカル日付の yyyy-MM-dd（実績の集計と同じ書式）。"""
    return (when or datetime.now()).strftime("%Y-%m-%d")


# ----------------------------------------------------------------------
# ストア
# ----------------------------------------------------------------------

class ApiUsageStore:
    """API 使用量の日次集計と永続化。

    Attributes:
        on_changed: 記録したときの通知（UI の更新用。記録したスレッドから呼ばれる）
    """

    def __init__(self, file_path: Optional[Path] = None) -> None:
        self._path = Path(file_path) if file_path else data_dir() / FILE_NAME
        self._lock = threading.Lock()
        self._days: Dict[str, List[UsageEntry]] = {}
        self._save_blocked = False
        self._save_pending = False
        # ファイル書き込みを 1 本に並べる（裏の保存と flush が同じ .tmp を奪い合わないように）
        self._write_lock = threading.Lock()
        self._save_thread: Optional[threading.Thread] = None
        self.on_changed: Optional[Callable[[], None]] = None
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            days = raw.get("days") if isinstance(raw, dict) else None
            if not isinstance(days, dict):
                raise ValueError("API 使用量ファイルの形が違います")
            for day, rows in days.items():
                self._days[str(day)] = [UsageEntry.from_dict(r) for r in rows if isinstance(r, dict)]
        except Exception as e:
            logger.warning(f"API 使用量ファイルを読めませんでした（退避して空で始めます）: {e}")
            # 壊れたまま空で開始すると次の保存で元の記録を上書きして消すので退避する
            if corrupt_file.quarantine(self._path, label="API 使用量ファイル") is None:
                self._save_blocked = True

    # --- 記録（どのスレッドからでも呼べる） ---

    def record_audio(self, backend_or_provider: str, model: str, seconds: float,
                     purpose: str = PURPOSE_TRANSCRIPTION, when: Optional[date] = None) -> None:
        """音声の文字起こし 1 リクエストを記録する（最低課金秒数はここで 1 回ごとに適用）。"""
        if seconds <= 0:
            return
        provider = provider_for(backend_or_provider)
        entry = UsageEntry(provider, model, purpose, requests=1, audioSeconds=float(seconds),
                           billedAudioSeconds=billed_seconds(provider, model, seconds))
        self._add(entry, when)

    def record_tokens(self, provider: str, model: str, purpose: str,
                      input_tokens: int, output_tokens: int, when: Optional[date] = None) -> None:
        """LLM 1 リクエストを記録する（トークン数は応答の usage から）。"""
        entry = UsageEntry(provider_for(provider), model, purpose, requests=1,
                           inputTokens=max(0, int(input_tokens)), outputTokens=max(0, int(output_tokens)))
        self._add(entry, when)

    def record_chat_usage(self, payload: Any, provider: str, model: str, purpose: str) -> bool:
        """OpenAI 互換のチャット応答（Groq）の usage を読んで記録する（usage が無ければ False）。"""
        if not isinstance(payload, dict):
            return False
        usage = payload.get("usage") or (payload.get("x_groq") or {}).get("usage")
        if not isinstance(usage, dict):
            return False
        prompt = usage.get("prompt_tokens")
        completion = usage.get("completion_tokens")
        if prompt is None and completion is None:
            return False
        self.record_tokens(provider, model, purpose, int(prompt or 0), int(completion or 0))
        return True

    def record_gemini_usage(self, payload: Any, model: str, purpose: str) -> bool:
        """Gemini の応答の usageMetadata を記録する（思考トークンは出力として課金されるので出力に足す）。"""
        if not isinstance(payload, dict) or not isinstance(payload.get("usageMetadata"), dict):
            return False
        meta = payload["usageMetadata"]
        self.record_tokens(
            "gemini", model, purpose,
            int(meta.get("promptTokenCount") or 0),
            int(meta.get("candidatesTokenCount") or 0) + int(meta.get("thoughtsTokenCount") or 0),
        )
        return True

    def _add(self, entry: UsageEntry, when: Optional[date]) -> None:
        day = day_string(when)
        with self._lock:
            rows = self._days.setdefault(day, [])
            for row in rows:
                if row.same_key(entry):
                    row.add(entry)
                    break
            else:
                rows.append(entry)
        self._schedule_save()
        handler = self.on_changed
        if handler is not None:
            try:
                handler()
            except Exception:
                pass

    def _schedule_save(self) -> None:
        """保存は裏のスレッドで後回しにする（記録した側を待たせない。連続した記録は 1 回にまとめる）。"""
        if self._save_blocked:
            return
        with self._lock:
            if self._save_pending:
                return
            self._save_pending = True
        thread = threading.Thread(target=self._save, daemon=True, name="ApiUsageSave")
        self._save_thread = thread
        thread.start()

    def _save(self) -> None:
        if self._save_blocked:
            return
        # 書き込み全体を直列化し、その内側で最新の中身を写す（後から書いた側が必ず新しい中身になる）
        with self._write_lock:
            with self._lock:
                self._save_pending = False
                data = {"days": {d: [r.__dict__.copy() for r in rows] for d, rows in self._days.items()},
                        "backfillVersion": 0}
            try:
                tmp = self._path.with_name(self._path.name + ".tmp")
                tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
                tmp.replace(self._path)
            except Exception as e:
                logger.error(f"API 使用量の保存に失敗: {e}")

    def flush(self) -> None:
        """保留中の保存をすぐ行い、裏で走っている保存の終わりも待つ（テスト・終了時用）。"""
        thread = self._save_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5)
        self._save()

    # --- 集計（表示用） ---

    def summary(self, mode: str, include: Callable[[str], bool]) -> CostSummary:
        """指定した日付キーの行をまとめた合計（内訳は料金の高い順・単価未確認は末尾）。"""
        merged: List[UsageEntry] = []
        with self._lock:
            for day, rows in self._days.items():
                if not include(day):
                    continue
                for row in rows:
                    for m in merged:
                        if m.same_key(row):
                            m.add(row)
                            break
                    else:
                        merged.append(UsageEntry(**row.__dict__))
        result = CostSummary()
        for row in merged:
            result.requests += row.requests
            c = row.cost_usd(mode)
            if c is None:
                result.has_unpriced = True
            else:
                result.usd += c
        result.rows = sorted(merged, key=lambda r: -(r.cost_usd(mode) if r.cost_usd(mode) is not None else -1))
        return result

    def today(self, mode: str, now: Optional[date] = None) -> CostSummary:
        key = day_string(now)
        return self.summary(mode, lambda d: d == key)

    def month(self, mode: str, now: Optional[date] = None) -> CostSummary:
        key = day_string(now)[:7]
        return self.summary(mode, lambda d: d.startswith(key))

    def all_time(self, mode: str) -> CostSummary:
        return self.summary(mode, lambda d: True)

    def daily_series(self, days: int, mode: str, end: Optional[date] = None) -> List[tuple]:
        """直近 days 日の (yyyy-MM-dd, USD) の並び（古い順・記録の無い日は 0）。"""
        end_day = end or datetime.now().date()
        series = []
        for offset in range(days - 1, -1, -1):
            key = day_string(end_day - timedelta(days=offset))
            series.append((key, self.summary(mode, lambda d, k=key: d == k).usd))
        return series


# アプリ全体で共有するストア（遅延生成。テストでは差し替える）
_shared: Optional[ApiUsageStore] = None
_shared_lock = threading.Lock()


def shared() -> ApiUsageStore:
    """共有ストアを返す（初回だけ作る）。"""
    global _shared
    with _shared_lock:
        if _shared is None:
            import os
            import tempfile

            if os.environ.get("VOICEKEY_TESTING"):
                # テスト実行中は本人の実ファイル（リポジトリ直下の api_usage.json）を汚さない
                _shared = ApiUsageStore(Path(tempfile.mkdtemp(prefix="voicekey-api-usage-")) / FILE_NAME)
            else:
                _shared = ApiUsageStore()
        return _shared


def flush_shared() -> None:
    """終了時用: 共有ストアが作られていれば保留中の保存を書き切る（作られていなければ何もしない）。"""
    store = _shared
    if store is not None:
        store.flush()


def record_audio(backend: str, model: str, seconds: float, purpose: str = PURPOSE_TRANSCRIPTION) -> None:
    """共有ストアへ音声の使用量を記録する（文字起こしの各経路から呼ぶ）。"""
    shared().record_audio(backend, model, seconds, purpose)


def record_chat_usage(payload: Any, provider: str, model: str, purpose: str) -> bool:
    """共有ストアへ Groq の usage を記録する（整形・翻訳から呼ぶ）。"""
    return shared().record_chat_usage(payload, provider, model, purpose)


def record_gemini_usage(payload: Any, model: str, purpose: str) -> bool:
    """共有ストアへ Gemini の usage を記録する（翻訳から呼ぶ）。"""
    return shared().record_gemini_usage(payload, model, purpose)


def model_label(provider: str) -> str:
    """プロバイダーの表示名。"""
    return PROVIDER_LABELS.get(provider, provider)
