#!/usr/bin/env python3
"""
リアルタイムストリーミング文字起こしの速度測定（Deepgram / OpenAI Realtime）。

WAV をリアルタイム速度（1倍速）で WebSocket に流し込み、次の 2 つを測る:
  - TTFB        : 送信開始から「最初のテキスト」が返るまで（喋り出して何秒で字が出るか）
  - 確定レイテンシ: 送信完了（＝発話終了相当）から「最終確定」までの時間
実使用の「喋り終わって離す → 貼り付け」体感に最も近いのは確定レイテンシ。

実行: .venv/bin/python stream_benchmark.py
"""

import asyncio
import audioop
import base64
import json
import time
import wave
from pathlib import Path

import websockets

from run_benchmark import get_key, load_dotenv, cer

ROOT = Path(__file__).parent
AUDIO = ROOT / "audio"
SR = 16000
OPENAI_SR = 24000  # OpenAI Realtime は入力 PCM レート 24000 以上が必須（16k は拒否される）
CHUNK_MS = 100
BYTES_PER_CHUNK = SR * 2 * CHUNK_MS // 1000  # 16k * 2byte * 0.1s = 3200

# (プロバイダ, モデル)
STREAM_MODELS = [
    ("deepgram", "nova-2"),
    ("deepgram", "nova-3"),
    ("openai", "gpt-4o-transcribe"),
    ("openai", "gpt-4o-mini-transcribe"),
    ("openai", "gpt-realtime-whisper"),
]


def read_pcm(name):
    p = AUDIO / f"{name}_ja.wav"
    with wave.open(str(p), "rb") as w:
        assert w.getframerate() == SR and w.getnchannels() == 1 and w.getsampwidth() == 2, \
            "16kHz mono pcm16 が必要"
        pcm = w.readframes(w.getnframes())
    truth = (AUDIO / f"{name}_ja.txt").read_text(encoding="utf-8").strip()
    return pcm, truth, len(pcm) / (SR * 2)


async def _connect(url, headers):
    """websockets のバージョン差（additional_headers / extra_headers）を吸収"""
    try:
        return await websockets.connect(url, additional_headers=headers, max_size=None)
    except TypeError:
        return await websockets.connect(url, extra_headers=headers, max_size=None)


async def run_deepgram(key, model, pcm):
    lang = "multi" if model.startswith("nova-3") else "ja"
    url = (f"wss://api.deepgram.com/v1/listen?model={model}&language={lang}"
           f"&encoding=linear16&sample_rate=16000&channels=1"
           f"&interim_results=true&punctuate=true")
    t = {"start": None, "first": None, "audio_end": None, "final": None}
    finals = []
    ws = await _connect(url, {"Authorization": f"Token {key}"})
    async with ws:
        async def send():
            t["start"] = time.perf_counter()
            for i in range(0, len(pcm), BYTES_PER_CHUNK):
                await ws.send(pcm[i:i + BYTES_PER_CHUNK])
                await asyncio.sleep(CHUNK_MS / 1000)
            t["audio_end"] = time.perf_counter()
            await ws.send(json.dumps({"type": "CloseStream"}))

        async def recv():
            async for msg in ws:
                d = json.loads(msg)
                if "channel" not in d:
                    continue
                txt = (d["channel"]["alternatives"][0].get("transcript") or "").strip()
                if txt and t["first"] is None:
                    t["first"] = time.perf_counter()
                if d.get("is_final") and txt:
                    finals.append(txt)
                    t["final"] = time.perf_counter()

        await asyncio.gather(send(), recv())
    return t, "".join(finals)


async def run_openai(key, model, pcm):
    # GA 仕様: OpenAI-Beta ヘッダは廃止。session.type=transcription、
    # audio.input.format に PCM レートを指定、turn_detection=null で手動 commit。
    # OpenAI は 24kHz 以上必須なので 16kHz テスト音声をアップサンプルして送る。
    pcm, _ = audioop.ratecv(pcm, 2, 1, SR, OPENAI_SR, None)
    chunk_bytes = OPENAI_SR * 2 * CHUNK_MS // 1000  # 24k 基準で1倍速を維持
    url = "wss://api.openai.com/v1/realtime?intent=transcription"
    headers = {"Authorization": f"Bearer {key}"}
    t = {"start": None, "first": None, "audio_end": None, "final": None}
    parts = []
    ws = await _connect(url, headers)
    async with ws:
        await ws.send(json.dumps({
            "type": "session.update",
            "session": {
                "type": "transcription",
                "audio": {
                    "input": {
                        "format": {"type": "audio/pcm", "rate": OPENAI_SR},
                        "transcription": {"model": model, "language": "ja"},
                        "turn_detection": None,
                    }
                },
            },
        }))

        async def send():
            t["start"] = time.perf_counter()
            for i in range(0, len(pcm), chunk_bytes):
                chunk = pcm[i:i + chunk_bytes]
                await ws.send(json.dumps({
                    "type": "input_audio_buffer.append",
                    "audio": base64.b64encode(chunk).decode(),
                }))
                await asyncio.sleep(CHUNK_MS / 1000)
            t["audio_end"] = time.perf_counter()
            await ws.send(json.dumps({"type": "input_audio_buffer.commit"}))

        async def recv():
            # 手動 commit のため、確定は commit 後に届く。確定が来たら抜ける
            async for msg in ws:
                d = json.loads(msg)
                typ = d.get("type", "")
                if typ.endswith("transcription.delta"):
                    if t["first"] is None:
                        t["first"] = time.perf_counter()
                    parts.append(d.get("delta", ""))
                elif typ.endswith("transcription.completed"):
                    t["final"] = time.perf_counter()
                    tr = d.get("transcript")
                    if tr:
                        parts[:] = [tr]
                    if t["audio_end"]:
                        break
                elif typ == "error":
                    parts.append(f"[error:{d.get('error', {}).get('message', '')}]")
                    break

        try:
            await asyncio.wait_for(asyncio.gather(send(), recv()), timeout=120)
        except asyncio.TimeoutError:
            pass
    return t, "".join(parts)


async def measure(provider, model, key, pcm):
    if provider == "deepgram":
        return await run_deepgram(key, model, pcm)
    return await run_openai(key, model, pcm)


async def main():
    load_dotenv()
    clips = {name: read_pcm(name) for name in ("short", "long")}
    keys = {p: get_key(p) for p in ("openai", "deepgram")}
    for p in ("openai", "deepgram"):
        print(f"  {p:9s}: {'キーあり' if keys[p] else 'キーなし（スキップ）'}")
    print("  groq/elevenlabs: ストリーミング非対応のため対象外\n")

    rows = []
    for provider, model in STREAM_MODELS:
        key = keys.get(provider)
        for clip_name, (pcm, truth, dur) in clips.items():
            label = f"{provider}/{model} [{clip_name}]"
            if not key:
                rows.append((provider, model, clip_name, "skip(キーなし)", None, None, None))
                continue
            try:
                t, text = await measure(provider, model, key, pcm)
                ttfb = (t["first"] - t["start"]) if t["first"] else None
                settle = (t["final"] - t["audio_end"]) if (t["final"] and t["audio_end"]) else None
                c = cer(text, truth) if text else None
                ts = f"{ttfb*1000:.0f}ms" if ttfb else "-"
                ss = f"{settle*1000:.0f}ms" if settle else "-"
                cs = f"{c*100:.1f}%" if c is not None else "-"
                print(f"  o {label}: TTFB={ts} 確定={ss} CER={cs}")
                rows.append((provider, model, clip_name, "ok", ttfb, settle, c))
            except Exception as e:
                print(f"  x {label}: {type(e).__name__}: {e}")
                rows.append((provider, model, clip_name, f"error:{type(e).__name__}", None, None, None))

    print("\n## ストリーミング速度測定\n")
    print("| モデル | 音声 | TTFB（最初の字） | 確定レイテンシ（離して→確定） | CER |")
    print("|---|---|---|---|---|")
    for provider, model, clip_name, status, ttfb, settle, c in rows:
        clip = "短文" if clip_name == "short" else "長文"
        if status != "ok":
            print(f"| {provider}/{model} | {clip} | {status} | - | - |")
        else:
            ts = f"{ttfb*1000:.0f}ms" if ttfb else "-"
            ss = f"{settle*1000:.0f}ms" if settle else "-"
            cs = f"{c*100:.1f}%" if c is not None else "-"
            print(f"| {provider}/{model} | {clip} | {ts} | {ss} | {cs} |")


if __name__ == "__main__":
    asyncio.run(main())
