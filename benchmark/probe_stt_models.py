#!/usr/bin/env python3
"""
各社の「最新 STT モデル」を実測で洗い出すプローブ。

モデル一覧 API は STT を網羅しないことがある（ElevenLabs の /v1/models は TTS 用）。
そこで候補 model_id を実際に短文音声へ投げ、200 が返る＝実在 とみなして
レイテンシ・CER も同時に測る。推測でなく事実で「scribe_v2 はあるか」を判定する。

実行: .venv/bin/python probe_stt_models.py
"""

import time
import httpx

from run_benchmark import load_dotenv, get_key, cer, AUDIO

# 各社で試す候補（実在しないものは 400/404 で弾かれる）
ELEVENLABS_CANDIDATES = [
    "scribe_v2", "scribe_v2_experimental", "scribe_v2_realtime",
    "scribe_v1", "scribe_v1_experimental",
]
OPENAI_CANDIDATES = [
    "gpt-4o-transcribe", "gpt-4o-mini-transcribe", "gpt-realtime-whisper",
    "whisper-1",
]
DEEPGRAM_CANDIDATES = ["nova-3", "nova-2"]


def probe_elevenlabs(key, model, wav, truth):
    t0 = time.perf_counter()
    try:
        r = httpx.post(
            "https://api.elevenlabs.io/v1/speech-to-text",
            headers={"xi-api-key": key},
            files={"file": ("audio.wav", wav, "audio/wav")},
            data={"model_id": model, "language_code": "ja", "tag_audio_events": "false"},
            timeout=120,
        )
        dt = (time.perf_counter() - t0) * 1000
        if r.status_code == 200:
            text = (r.json().get("text") or "").strip()
            return ("ok", dt, cer(text, truth), text)
        return (f"HTTP {r.status_code}", dt, None, r.text[:160])
    except Exception as e:
        return (f"{type(e).__name__}", None, None, str(e)[:160])


def probe_openai_like(base, key, model, wav, truth):
    t0 = time.perf_counter()
    try:
        r = httpx.post(
            f"{base}/audio/transcriptions",
            headers={"Authorization": f"Bearer {key}"},
            files={"file": ("audio.wav", wav, "audio/wav")},
            data={"model": model, "response_format": "text", "temperature": "0", "language": "ja"},
            timeout=120,
        )
        dt = (time.perf_counter() - t0) * 1000
        if r.status_code == 200:
            text = r.text.strip()
            return ("ok", dt, cer(text, truth), text)
        return (f"HTTP {r.status_code}", dt, None, r.text[:160])
    except Exception as e:
        return (f"{type(e).__name__}", None, None, str(e)[:160])


def show_models_endpoint(key):
    """ElevenLabs /v1/models から scribe/stt 関連を抜き出して確認"""
    try:
        r = httpx.get("https://api.elevenlabs.io/v1/models",
                      headers={"xi-api-key": key}, timeout=30)
        r.raise_for_status()
        names = []
        for m in r.json():
            mid = m.get("model_id", "")
            can_stt = m.get("can_do_voice_conversion") or m.get("can_use_speaker_boost")
            # STT 能力フラグは models API にないことが多いので名前で拾う
            if "scribe" in mid.lower() or "stt" in mid.lower() or "transcri" in mid.lower():
                names.append(mid)
        print(f"  /v1/models 内の STT らしき model_id: {names or '(なし＝STTはmodels APIに出ない)'}")
    except Exception as e:
        print(f"  /v1/models 取得エラー: {type(e).__name__}: {e}")


def main():
    load_dotenv()
    wav = (AUDIO / "short_ja.wav").read_bytes()
    truth = (AUDIO / "short_ja.txt").read_text(encoding="utf-8").strip()

    keys = {p: get_key(p) for p in ("openai", "elevenlabs", "deepgram")}

    print("== ElevenLabs ==")
    if keys["elevenlabs"]:
        show_models_endpoint(keys["elevenlabs"])
        for m in ELEVENLABS_CANDIDATES:
            status, dt, c, detail = probe_elevenlabs(keys["elevenlabs"], m, wav, truth)
            d = f"{dt:.0f}ms" if dt else "-"
            cs = f"CER={c*100:.1f}%" if c is not None else ""
            print(f"  {m:26s} {status:10s} {d:8s} {cs}")
            if status != "ok":
                print(f"      ↳ {detail}")
    else:
        print("  キーなし")

    print("\n== OpenAI ==")
    if keys["openai"]:
        for m in OPENAI_CANDIDATES:
            status, dt, c, detail = probe_openai_like(
                "https://api.openai.com/v1", keys["openai"], m, wav, truth)
            d = f"{dt:.0f}ms" if dt else "-"
            cs = f"CER={c*100:.1f}%" if c is not None else ""
            print(f"  {m:26s} {status:10s} {d:8s} {cs}")
            if status != "ok":
                print(f"      ↳ {detail}")
    else:
        print("  キーなし")


if __name__ == "__main__":
    main()
