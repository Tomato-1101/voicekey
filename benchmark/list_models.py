#!/usr/bin/env python3
"""
各プロバイダの API から「実際に利用可能な文字起こしモデル」を取得して一覧表示する。

最新モデルやリアルタイム対応の有無を推測でなく事実で確認するためのツール。
日本語対応・ストリーミング対応が分かるものはそれも表示する。
"""

import json
import httpx

from run_benchmark import load_dotenv, get_key


def show(title, items):
    print(f"\n=== {title} ===")
    if not items:
        print("  (取得できず)")
        return
    for it in items:
        print(f"  {it}")


def openai_like(name, base, key):
    """OpenAI / Groq: GET /models から transcribe/whisper 系を抽出"""
    try:
        r = httpx.get(f"{base}/models", headers={"Authorization": f"Bearer {key}"}, timeout=30)
        r.raise_for_status()
        ids = sorted(m["id"] for m in r.json().get("data", []))
        stt = [i for i in ids if any(k in i.lower() for k in ("transcribe", "whisper"))]
        show(f"{name} 文字起こしモデル", stt or ids)
    except Exception as e:
        show(f"{name} エラー", [f"{type(e).__name__}: {e}"])


def elevenlabs(key):
    """ElevenLabs: STT モデルのエンドポイント候補を順に試す"""
    headers = {"xi-api-key": key}
    # まず STT 専用候補、ダメなら汎用 /v1/models
    for url in (
        "https://api.elevenlabs.io/v1/speech-to-text/models",
        "https://api.elevenlabs.io/v1/models",
    ):
        try:
            r = httpx.get(url, headers=headers, timeout=30)
            if r.status_code == 404:
                continue
            r.raise_for_status()
            data = r.json()
            print(f"\n=== ElevenLabs ({url}) 生レスポンス抜粋 ===")
            print(json.dumps(data, ensure_ascii=False, indent=2)[:1500])
            return
        except Exception as e:
            show(f"ElevenLabs エラー ({url})", [f"{type(e).__name__}: {e}"])
    show("ElevenLabs", ["STT モデル一覧エンドポイントが見つからず"])


def deepgram(key):
    """Deepgram: GET /v1/models（公開モデル）。STT モデルと言語を表示"""
    try:
        r = httpx.get(
            "https://api.deepgram.com/v1/models",
            headers={"Authorization": f"Token {key}"}, timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        stt = data.get("stt", data) if isinstance(data, dict) else data
        print("\n=== Deepgram STT モデル ===")
        if isinstance(stt, list):
            for m in stt:
                name = m.get("canonical_name") or m.get("name")
                langs = m.get("languages", [])
                ja = "ja" if any(str(l).startswith("ja") for l in langs) else ""
                streaming = m.get("streaming")
                print(f"  {name:28s} 日本語:{('対応' if ja else '?'):4s} "
                      f"streaming:{streaming} langs={len(langs)}")
        else:
            print(json.dumps(data, ensure_ascii=False, indent=2)[:1500])
    except Exception as e:
        show("Deepgram エラー", [f"{type(e).__name__}: {e}"])


def main():
    load_dotenv()
    keys = {p: get_key(p) for p in ("openai", "groq", "elevenlabs", "deepgram")}
    for p, k in keys.items():
        print(f"  {p:11s}: {'キーあり' if k else 'キーなし（スキップ）'}")

    if keys["openai"]:
        openai_like("OpenAI", "https://api.openai.com/v1", keys["openai"])
    if keys["groq"]:
        openai_like("Groq", "https://api.groq.com/openai/v1", keys["groq"])
    if keys["elevenlabs"]:
        elevenlabs(keys["elevenlabs"])
    if keys["deepgram"]:
        deepgram(keys["deepgram"])


if __name__ == "__main__":
    main()
