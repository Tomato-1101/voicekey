"""テキスト整形 LLM（Groq Chat Completions）の速度ベンチマーク。

設定 UI で選べる全整形モデルに同一の整形リクエスト（おまかせ自動判断プロンプト +
フィラー多めの日本語文）を送り、レイテンシだけを測定する。
精度は測らない（速度のみ。ユーザー指示 2026-06-11）。

API キーは環境変数 → Keychain の順で取得し、値は一切表示しない。
.env は読まない（settings の Read(**/.env) deny を尊重するため、dotenv 経由も使わない）。
"""

import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from run_benchmark import get_key  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent.parent))
from src.core.text_formatter import (  # noqa: E402
    DEFAULT_AUTO_PROMPT,
    KNOWN_FORMAT_MODELS,
    build_system_prompt,
)

import httpx  # noqa: E402

API_URL = "https://api.groq.com/openai/v1/chat/completions"
RUNS = 3

# 実使用に近い、フィラー多めの音声入力想定文（約 110 文字）
INPUT = (
    "えーとですね、あの、明日の打ち合わせなんですけど、まあ、15時からに変更してほしくて、"
    "えっと、場所は、あ、場所はそのままで、なんか、資料だけ先に送ってもらえると、まあ助かります。"
    "あとあの、議題は3つあって、予算と、えっと、スケジュールと、あとなんだっけ、人員の話です"
)


def bench(key: str, model: str) -> dict:
    """1 モデルを RUNS 回計測し、レイテンシ統計を返す（失敗時は error を返す）。"""
    times = []
    out_len = 0
    for _ in range(RUNS):
        t0 = time.perf_counter()
        try:
            resp = httpx.post(
                API_URL,
                headers={"Authorization": f"Bearer {key}"},
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": build_system_prompt("auto", "", "")},
                        {"role": "user", "content": INPUT},
                    ],
                    "temperature": 0.2,
                },
                timeout=30.0,
            )
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}"}
        if resp.status_code != 200:
            return {"error": f"HTTP {resp.status_code}: {resp.text[:120]}"}
        ms = (time.perf_counter() - t0) * 1000
        times.append(ms)
        out_len = len(resp.json()["choices"][0]["message"]["content"])
    return {"min": min(times), "median": statistics.median(times), "out_len": out_len}


def main() -> None:
    key = get_key("groq")
    if not key:
        print("GROQ_API_KEY が見つかりません（env / Keychain）")
        sys.exit(1)

    print(f"入力: {len(INPUT)} 文字 / システムプロンプト: おまかせ（自動判断） / 各 {RUNS} 回")
    print(f"{'model':<32} {'min(ms)':>8} {'median(ms)':>11}  出力文字数")
    results = []
    for model in KNOWN_FORMAT_MODELS:
        r = bench(key, model)
        if "error" in r:
            print(f"{model:<32} エラー: {r['error']}")
            continue
        results.append((model, r))
        print(f"{model:<32} {r['min']:>8.0f} {r['median']:>11.0f}  {r['out_len']}")

    if results:
        fastest = min(results, key=lambda x: x[1]["median"])
        print(f"\n最速（median）: {fastest[0]} ({fastest[1]['median']:.0f}ms)")


if __name__ == "__main__":
    main()
