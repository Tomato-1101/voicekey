#!/usr/bin/env python3
"""
gpt-realtime-whisper の delay パラメータ（遅延/精度チューニング）スイープ測定。

Realtime transcription の `delay`（minimal/low/medium/high/xhigh）ごとに
TTFB・確定レイテンシ・CER を実測し、未指定（サーバー既定）と比較する。
「delay=minimal にすれば Deepgram nova-3 の確定 ~70-100ms に届くか」の裏取り用。

実行: .venv/bin/python delay_sweep.py
"""

import asyncio
import time

from run_benchmark import get_key, load_dotenv, cer
from stream_benchmark import read_pcm, run_openai

MODEL = "gpt-realtime-whisper"
DELAYS = [None, "minimal", "low", "medium", "high"]  # None=未指定（既定）


async def main():
    load_dotenv()
    key = get_key("openai")
    if not key:
        print("OpenAI キーなし（中止）")
        return
    clips = {name: read_pcm(name) for name in ("short", "long")}

    rows = []
    for delay in DELAYS:
        label = delay or "(既定)"
        for clip_name, (pcm, truth, dur) in clips.items():
            try:
                t, text = await run_openai(key, MODEL, pcm, delay=delay)
                ttfb = (t["first"] - t["start"]) if t["first"] else None
                settle = (t["final"] - t["audio_end"]) if (t["final"] and t["audio_end"]) else None
                c = cer(text, truth) if text else None
                ts = f"{ttfb*1000:.0f}ms" if ttfb else "-"
                ss = f"{settle*1000:.0f}ms" if settle else "-"
                cs = f"{c*100:.1f}%" if c is not None else "-"
                err = text if text.startswith("[error:") else ""
                print(f"  o delay={label} [{clip_name}]: TTFB={ts} 確定={ss} CER={cs} {err}")
                rows.append((label, clip_name, ts, ss, cs))
            except Exception as e:
                print(f"  x delay={label} [{clip_name}]: {type(e).__name__}: {e}")
                rows.append((label, clip_name, f"error:{type(e).__name__}", "-", "-"))

    print(f"\n## {MODEL} delay スイープ\n")
    print("| delay | 音声 | TTFB | 確定レイテンシ | CER |")
    print("|---|---|---|---|---|")
    for label, clip_name, ts, ss, cs in rows:
        clip = "短文" if clip_name == "short" else "長文"
        print(f"| {label} | {clip} | {ts} | {ss} | {cs} |")


if __name__ == "__main__":
    asyncio.run(main())
