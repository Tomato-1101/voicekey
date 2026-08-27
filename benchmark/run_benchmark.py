#!/usr/bin/env python3
"""
voicekey 文字起こしベンチマーク

同一の音声ファイル（短文・長文）を OpenAI / Groq / ElevenLabs / Deepgram の
各モデルに送り、レイテンシ（API 往復時間）と CER（文字誤り率）を測定する。

API キーの取得順:
  1. 環境変数 / .env（OPENAI_API_KEY など）
  2. Keychain（voicekey アプリと同じエントリ: voicekey.OpenAI など）

CER は「正解原稿（say に読ませた原稿）」と認識結果を、句読点・空白を除いて
比較した文字単位の編集距離 / 正解文字数。小さいほど高精度。
"""

import os
import re
import sys
import json
import time
import subprocess
import unicodedata
from pathlib import Path

import httpx

ROOT = Path(__file__).parent
AUDIO = ROOT / "audio"
RESULTS = ROOT / "results"

# レイテンシは振れるため複数回測って最速値を採用する
RUNS = 2
LANGUAGE = "ja"

# (プロバイダ, ベースURL or None, モデル)
# モデルは list_models.py で API から取得した実在モデル。
# 使えないモデル（バッチ非対応など）はエラー行として表に出る。
OPENAI = "https://api.openai.com/v1"
GROQ = "https://api.groq.com/openai/v1"
MODELS = [
    ("openai", OPENAI, "gpt-4o-transcribe"),
    ("openai", OPENAI, "gpt-4o-mini-transcribe"),
    ("groq", GROQ, "whisper-large-v3-turbo"),
    ("groq", GROQ, "whisper-large-v3"),
    ("elevenlabs", None, "scribe_v2"),
    ("elevenlabs", None, "scribe_v1"),
    ("deepgram", None, "nova-3"),
    ("deepgram", None, "nova-2"),
    # 文字起こし専用モデル（2026-08-26 公開プレビュー）。整形込みで返る
    ("gemini", None, "gemini-3.5-transcribe"),
]

ENV_VAR = {
    "openai": "OPENAI_API_KEY",
    "groq": "GROQ_API_KEY",
    "elevenlabs": "ELEVENLABS_API_KEY",
    "deepgram": "DEEPGRAM_API_KEY",
    "gemini": "GEMINI_API_KEY",
}
KEYCHAIN_SERVICE = {
    "openai": "voicekey.OpenAI",
    "groq": "voicekey.Groq",
    "elevenlabs": "voicekey.ElevenLabs",
    "deepgram": "voicekey.Deepgram",
    "gemini": "voicekey.Gemini",
}


# ---- API キー取得 ----

def load_dotenv() -> None:
    """benchmark/.env を読み、未設定の環境変数だけ補う"""
    f = ROOT / ".env"
    if not f.exists():
        return
    for line in f.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip().strip('"').strip("'")
        if v:
            os.environ.setdefault(k.strip(), v)


def keychain_key(service: str, account: str = "default"):
    """Keychain から API キーを読む（中身は表示しない）"""
    try:
        r = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-a", account, "-w"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0:
            return r.stdout.strip()
    except Exception:
        pass
    return None


def get_key(provider: str):
    """環境変数 → アプリの Keychain 項目 → 中央 Keychain（service=変数名 / account=shared）"""
    return (
        os.environ.get(ENV_VAR[provider])
        or keychain_key(KEYCHAIN_SERVICE[provider])
        or keychain_key(ENV_VAR[provider], account="shared")
    )


# ---- CER 計算 ----

def levenshtein(a: str, b: str) -> int:
    m, n = len(a), len(b)
    if m == 0:
        return n
    if n == 0:
        return m
    prev = list(range(n + 1))
    for i in range(1, m + 1):
        cur = [i] + [0] * n
        ai = a[i - 1]
        for j in range(1, n + 1):
            cost = 0 if ai == b[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
        prev = cur
    return prev[n]


def normalize(s: str) -> str:
    """CER 比較用の正規化。

    モデルごとの**表記の癖**は誤りではないので潰す。潰さないと「12.7% と 12.7 パーセント」の
    ような等価な出力が誤り扱いになり、精度の比較にならない。
    - 全角英数字・記号 → 半角（NFKC）
    - パーセント表記の統一（％ / パーセント → %）
    - 空白・句読点・記号・ハイフンを除去（長音「ー」は残す）
    """
    s = unicodedata.normalize("NFKC", s)
    s = s.replace("パーセント", "%").replace("％", "%")
    return re.sub(r"[\s、。，．,.!！?？「」『』（）()・…〜~\-−–—:：;；]", "", s)


def cer(hyp: str, truth: str):
    h, t = normalize(hyp), normalize(truth)
    if not t:
        return None
    return levenshtein(h, t) / len(t)


# ---- 各バックエンドの文字起こし ----

def transcribe_openai_compat(base, key, model, wav) -> str:
    r = httpx.post(
        f"{base}/audio/transcriptions",
        headers={"Authorization": f"Bearer {key}"},
        files={"file": ("audio.wav", wav, "audio/wav")},
        data={"model": model, "response_format": "text",
              "temperature": "0", "language": LANGUAGE},
        timeout=120,
    )
    r.raise_for_status()
    return r.text.strip()


def transcribe_elevenlabs(key, model, wav) -> str:
    r = httpx.post(
        "https://api.elevenlabs.io/v1/speech-to-text",
        headers={"xi-api-key": key},
        files={"file": ("audio.wav", wav, "audio/wav")},
        data={"model_id": model, "language_code": LANGUAGE, "tag_audio_events": "false"},
        timeout=120,
    )
    r.raise_for_status()
    return (r.json().get("text") or "").strip()


def transcribe_deepgram(key, model, wav) -> str:
    # nova-3 は日本語の単言語指定に未対応のため多言語モードを使う
    lang = "multi" if model.startswith("nova-3") else LANGUAGE
    r = httpx.post(
        "https://api.deepgram.com/v1/listen",
        params={"model": model, "smart_format": "true", "language": lang},
        headers={"Authorization": f"Token {key}", "Content-Type": "audio/wav"},
        content=wav,
        timeout=120,
    )
    r.raise_for_status()
    alt = r.json()["results"]["channels"][0]["alternatives"][0]
    return (alt.get("transcript") or "").strip()


def transcribe_gemini(key, model, wav) -> str:
    """Gemini 3.5 Transcribe（Interactions API）。音声は base64 で JSON に載せる。"""
    import base64
    r = httpx.post(
        "https://generativelanguage.googleapis.com/v1beta/interactions",
        headers={
            "x-goog-api-key": key,
            "Content-Type": "application/json",
            "Api-Revision": "2026-05-20",
        },
        json={
            "model": model,
            "input": [{
                "type": "audio",
                "data": base64.b64encode(wav).decode("ascii"),
                "mime_type": "audio/wav",
            }],
            "generation_config": {"transcription_config": {
                "language_codes": ["ja-JP"],
                "mode": {"type": "smart"},
            }},
        },
        timeout=180,
    )
    r.raise_for_status()
    parts = []
    for step in r.json().get("steps") or []:
        for content in step.get("content") or []:
            if content.get("type") == "text" and content.get("text"):
                parts.append(content["text"])
    return "".join(parts).strip()


def transcribe(provider, base, model, key, wav) -> str:
    if provider in ("openai", "groq"):
        return transcribe_openai_compat(base, key, model, wav)
    if provider == "elevenlabs":
        return transcribe_elevenlabs(key, model, wav)
    if provider == "deepgram":
        return transcribe_deepgram(key, model, wav)
    if provider == "gemini":
        return transcribe_gemini(key, model, wav)
    raise ValueError(provider)


# ---- 実行 ----

def audio_duration(wav_path: Path):
    try:
        r = subprocess.run(["afinfo", str(wav_path)], capture_output=True, text=True, timeout=10)
        m = re.search(r"estimated duration: ([\d.]+) sec", r.stdout)
        if m:
            return float(m.group(1))
    except Exception:
        pass
    return None


def main():
    load_dotenv()
    RESULTS.mkdir(exist_ok=True)

    names = sys.argv[1:] or ["short", "long"]
    clips = []
    for name in names:
        wav = AUDIO / f"{name}_ja.wav"
        # 早口・雑音版は原稿を素の版と共有する（正解テキストは同じ）
        txt = AUDIO / f"{name.replace('_fast_noisy', '')}_ja.txt"
        if not wav.exists() or not txt.exists():
            print(f"!! {name}: 音声または原稿がありません（make_audio.sh を先に実行）", file=sys.stderr)
            continue
        clips.append({
            "name": name,
            "wav_bytes": wav.read_bytes(),
            "truth": txt.read_text(encoding="utf-8").strip(),
            "duration": audio_duration(wav),
        })
    if not clips:
        sys.exit("テスト音声がありません。`bash make_audio.sh` を先に実行してください。")

    # 利用可能なキーを確認
    keys = {p: get_key(p) for p in ENV_VAR}
    for p, k in keys.items():
        print(f"  {p:11s}: {'キーあり' if k else 'キーなし（スキップ）'}")
    print()

    rows = []
    for provider, base, model in MODELS:
        key = keys.get(provider)
        for clip in clips:
            label = f"{provider}/{model} [{clip['name']}]"
            if not key:
                rows.append({"provider": provider, "model": model, "clip": clip["name"],
                             "status": "skip(キーなし)", "latency": None, "cer": None, "text": ""})
                continue
            latencies = []
            text = ""
            error = None
            for _ in range(RUNS):
                try:
                    t0 = time.perf_counter()
                    text = transcribe(provider, base, model, key, clip["wav_bytes"])
                    latencies.append(time.perf_counter() - t0)
                except httpx.HTTPStatusError as e:
                    error = f"HTTP {e.response.status_code}: {e.response.text[:120]}"
                    break
                except Exception as e:
                    error = f"{type(e).__name__}: {e}"
                    break
            if error:
                print(f"  x {label}: {error}")
                rows.append({"provider": provider, "model": model, "clip": clip["name"],
                             "status": "error", "detail": error,
                             "latency": None, "cer": None, "text": ""})
                continue
            lat = min(latencies)
            c = cer(text, clip["truth"])
            print(f"  o {label}: {lat*1000:.0f}ms  CER={c*100:.1f}%")
            rows.append({"provider": provider, "model": model, "clip": clip["name"],
                         "status": "ok", "latency": lat, "cer": c, "text": text})

    # ---- 表出力 ----
    print("\n## ベンチマーク結果\n")
    durs = {c["name"]: c["duration"] for c in clips}
    print(f"音声長: 短文 {durs.get('short')}s / 長文 {durs.get('long')}s\n")
    header = "| モデル | 音声 | レイテンシ | CER（誤り率） | 認識文字数 |"
    print(header)
    print("|---|---|---|---|---|")
    for r in rows:
        name = f"{r['provider']}/{r['model']}"
        clip = "短文" if r["clip"] == "short" else "長文"
        if r["status"] != "ok":
            print(f"| {name} | {clip} | {r['status']} | - | - |")
        else:
            print(f"| {name} | {clip} | {r['latency']*1000:.0f}ms "
                  f"| {r['cer']*100:.1f}% | {len(r['text'])} |")

    # 認識結果の全文も保存
    out = RESULTS / f"result_{time.strftime('%Y%m%d_%H%M%S')}.json"
    out.write_text(json.dumps(
        {"durations": durs, "rows": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n詳細（認識全文つき）: {out}")


if __name__ == "__main__":
    main()
