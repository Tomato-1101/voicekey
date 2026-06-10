#!/usr/bin/env python3
"""
OpenAI Realtime 文字起こしの生レスポンス確認用デバッグスクリプト。

session.update を送って何が返るか（特に error / 各イベント type）を素のまま表示する。
ストリーミングが期待どおり delta/completed を返すか、仕様ズレを潰すために使う。
"""

import asyncio
import audioop
import base64
import json
import wave
from pathlib import Path

import websockets

from run_benchmark import get_key, load_dotenv

ROOT = Path(__file__).parent
SR = 24000  # OpenAI Realtime は入力 PCM レート 24000 以上が必須
CHUNK_MS = 100
BYTES_PER_CHUNK = SR * 2 * CHUNK_MS // 1000


def read_pcm(name):
    p = ROOT / "audio" / f"{name}_ja.wav"
    with wave.open(str(p), "rb") as w:
        src = w.readframes(w.getnframes())
        inrate = w.getframerate()
    # 16kHz 音声を OpenAI 要件の 24kHz へアップサンプル
    out, _ = audioop.ratecv(src, 2, 1, inrate, SR, None)
    return out


async def main():
    load_dotenv()
    key = get_key("openai")
    model = "gpt-4o-mini-transcribe"
    pcm = read_pcm("short")

    url = "wss://api.openai.com/v1/realtime?intent=transcription"
    headers = {"Authorization": f"Bearer {key}"}
    try:
        ws = await websockets.connect(url, additional_headers=headers, max_size=None)
    except TypeError:
        ws = await websockets.connect(url, extra_headers=headers, max_size=None)

    async with ws:
        await ws.send(json.dumps({
            "type": "session.update",
            "session": {
                "type": "transcription",
                "audio": {
                    "input": {
                        "format": {"type": "audio/pcm", "rate": SR},
                        "transcription": {"model": model, "language": "ja"},
                        "turn_detection": None,
                    }
                },
            },
        }))

        async def send():
            # 先頭 2 秒分だけ流して commit（デバッグなので短く）
            for i in range(0, min(len(pcm), SR * 2 * 2), BYTES_PER_CHUNK):
                await ws.send(json.dumps({
                    "type": "input_audio_buffer.append",
                    "audio": base64.b64encode(pcm[i:i + BYTES_PER_CHUNK]).decode(),
                }))
                await asyncio.sleep(CHUNK_MS / 1000)
            await ws.send(json.dumps({"type": "input_audio_buffer.commit"}))

        async def recv():
            n = 0
            async for msg in ws:
                d = json.loads(msg)
                typ = d.get("type", "")
                # error と最初の数件は中身を全部出す
                if typ == "error" or n < 6 or "transcription" in typ:
                    print(f"[{typ}] {json.dumps(d, ensure_ascii=False)[:400]}")
                else:
                    print(f"[{typ}]")
                n += 1
                if n > 40:
                    break

        asyncio.create_task(send())
        try:
            await asyncio.wait_for(recv(), timeout=20)
        except asyncio.TimeoutError:
            print("...timeout")


if __name__ == "__main__":
    asyncio.run(main())
