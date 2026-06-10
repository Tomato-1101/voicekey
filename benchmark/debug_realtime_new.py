#!/usr/bin/env python3
"""
新しいリアルタイム STT（Deepgram Flux / ElevenLabs Scribe v2 Realtime）の
生メッセージ確認用デバッグ。正確な model_id・イベント名・transcript の場所を
事実で確定してから stream_benchmark に組み込むために使う。

実行: .venv/bin/python debug_realtime_new.py
"""

import asyncio
import base64
import json
import wave
from pathlib import Path

import websockets

from run_benchmark import get_key, load_dotenv

ROOT = Path(__file__).parent
SR = 16000


def read_pcm(name="short"):
    with wave.open(str(ROOT / "audio" / f"{name}_ja.wav"), "rb") as w:
        return w.readframes(w.getnframes())


async def connect(url, headers):
    try:
        return await websockets.connect(url, additional_headers=headers, max_size=None)
    except TypeError:
        return await websockets.connect(url, extra_headers=headers, max_size=None)


async def debug_flux(key, pcm):
    print("\n===== Deepgram Flux (flux-general-multi, language_hint=ja) =====")
    url = ("wss://api.deepgram.com/v2/listen?model=flux-general-multi"
           "&encoding=linear16&sample_rate=16000&language_hint=ja")
    chunk = 16000 * 2 * 80 // 1000  # 80ms 推奨 = 2560 bytes
    try:
        ws = await connect(url, {"Authorization": f"Token {key}"})
    except Exception as e:
        print(f"  接続失敗: {type(e).__name__}: {e}")
        return
    async with ws:
        async def send():
            for i in range(0, min(len(pcm), SR * 2 * 3), chunk):  # 先頭3秒
                await ws.send(pcm[i:i + chunk])
                await asyncio.sleep(0.08)

        async def recv():
            n = 0
            async for msg in ws:
                try:
                    d = json.loads(msg)
                except Exception:
                    print(f"  (binary {len(msg)}B)")
                    continue
                t = d.get("type")
                tr = d.get("transcript")
                ev = d.get("event")
                print(f"  [{t}] event={ev} transcript={tr!r}")
                n += 1
                if n > 30:
                    break

        asyncio.create_task(send())
        try:
            await asyncio.wait_for(recv(), timeout=15)
        except asyncio.TimeoutError:
            print("  ...timeout")


async def debug_elevenlabs(key, pcm):
    print("\n===== ElevenLabs Scribe v2 Realtime =====")
    for model_id in ("scribe_v2_realtime", "scribe_v2", "scribe_realtime_v2"):
        url = (f"wss://api.elevenlabs.io/v1/speech-to-text/realtime"
               f"?model_id={model_id}&language_code=ja&audio_format=pcm_16000&commit_strategy=manual")
        try:
            ws = await connect(url, {"xi-api-key": key})
        except Exception as e:
            print(f"  [{model_id}] 接続失敗: {type(e).__name__}: {e}")
            continue
        print(f"  --- model_id={model_id} 接続OK ---")
        async with ws:
            async def send():
                step = 16000 * 2 * 100 // 1000  # 100ms
                data = pcm[:SR * 2 * 3]  # 先頭3秒
                for i in range(0, len(data), step):
                    last = i + step >= len(data)
                    await ws.send(json.dumps({
                        "message_type": "input_audio_chunk",
                        "audio_base_64": base64.b64encode(data[i:i + step]).decode(),
                        "commit": last,
                        "sample_rate": SR,
                    }))
                    await asyncio.sleep(0.1)

            async def recv():
                n = 0
                async for msg in ws:
                    try:
                        d = json.loads(msg)
                    except Exception:
                        continue
                    mt = d.get("message_type") or d.get("type")
                    txt = d.get("text")
                    print(f"  [{mt}] text={txt!r} {json.dumps(d, ensure_ascii=False)[:160]}")
                    n += 1
                    if n > 30:
                        break

            asyncio.create_task(send())
            try:
                await asyncio.wait_for(recv(), timeout=15)
            except asyncio.TimeoutError:
                print("  ...timeout")
        return  # 1つ接続できたら十分


async def main():
    load_dotenv()
    pcm = read_pcm("short")
    dg = get_key("deepgram")
    el = get_key("elevenlabs")
    if dg:
        await debug_flux(dg, pcm)
    else:
        print("Deepgram キーなし")
    if el:
        await debug_elevenlabs(el, pcm)
    else:
        print("ElevenLabs キーなし")


if __name__ == "__main__":
    asyncio.run(main())
