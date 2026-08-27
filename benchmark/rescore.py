#!/usr/bin/env python3
"""
保存済みのベンチ結果を「アプリが実際に貼り付ける形」で採点し直す。

なぜ必要か: 素の CER はモデルの**表記の癖**を誤りとして数えてしまう。実測（2026-08-27）で
ElevenLabs は認識自体は完璧なのに数字を漢数字で返すため CER 33% と出た。voicekey は
貼り付け前に `NumeralNormalizer`（漢数字 → 算用数字）を必ず通すので、ユーザーが受け取る
テキストは正しい。**アプリと同じ正規化を通してから比べる**のが実態に合った採点になる。

API は叩き直さない（results/*.json に認識全文が入っている）。

使い方:
    python3 rescore.py                       # 最新の結果を採点し直す
    python3 rescore.py results/result_x.json # ファイル指定
    python3 rescore.py --clip hard2          # 対象クリップを絞る
"""

import argparse
import glob
import importlib.util
import json
import re
import sys
import unicodedata
from pathlib import Path

# アプリ本体の正規化をそのまま使う。ただし `src` パッケージとして import すると
# src/__init__ → config → yaml と芋づるで依存が要るため、ファイルを直接読み込む
# （ベンチ用 venv には yaml も PySide6 も入っていない）。
_NORMALIZER_PATH = Path(__file__).resolve().parents[1] / "src" / "core" / "numeral_normalizer.py"
_spec = importlib.util.spec_from_file_location("voicekey_numeral_normalizer", _NORMALIZER_PATH)
numeral_normalizer = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(numeral_normalizer)

ROOT = Path(__file__).parent


def app_normalize(text: str) -> str:
    """voicekey が貼り付け前にかける正規化（数字まわり）を再現する。"""
    return numeral_normalizer.normalize(text, enabled=True, convert_counter=True, protect_words=set())


def compare_normalize(s: str) -> str:
    """比較用の正規化。等価な表記ゆれを潰す（誤りではないため）。"""
    s = unicodedata.normalize("NFKC", s)
    s = s.replace("パーセント", "%").replace("％", "%")
    s = re.sub(r"[\s、。，．,.!！?？「」『』（）()・…〜~\-−–—:：;；]", "", s)
    return s


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
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (0 if ai == b[j - 1] else 1))
        prev = cur
    return prev[n]


def cer(hyp: str, truth: str):
    h = compare_normalize(app_normalize(hyp))
    t = compare_normalize(app_normalize(truth))
    if not t:
        return None
    return levenshtein(h, t) / len(t)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", nargs="?", help="結果 JSON（省略時は最新）")
    parser.add_argument("--clip", help="対象クリップ名で絞り込む")
    args = parser.parse_args()

    path = Path(args.path) if args.path else Path(sorted(glob.glob(str(ROOT / "results" / "result_*.json")))[-1])
    rows = json.load(open(path, encoding="utf-8"))
    if not isinstance(rows, list):
        rows = rows.get("rows", [])

    truths = {}
    for txt in (ROOT / "audio").glob("*_ja.txt"):
        truths[txt.stem.replace("_ja", "")] = txt.read_text(encoding="utf-8").strip()

    print(f"# 再採点: {path.name}（アプリと同じ数字正規化を通してから CER を測る）\n")
    print("| モデル | 音声 | レイテンシ | CER(素) | CER(アプリ正規化後) |")
    print("|---|---|---|---|---|")
    for r in rows:
        clip = r.get("clip", "")
        if args.clip and clip != args.clip:
            continue
        text = r.get("text") or ""
        if not text:
            continue
        truth = truths.get(clip.replace("_fast_noisy", ""))
        if not truth:
            continue
        raw = r.get("cer")
        fixed = cer(text, truth)
        latency = f"{r['latency'] * 1000:.0f}ms" if r.get("latency") else "-"
        raw_s = f"{raw * 100:.1f}%" if raw is not None else "-"
        print(f"| {r['provider']}/{r['model']} | {clip} | {latency} | {raw_s} | **{fixed * 100:.1f}%** |")
    return 0


if __name__ == "__main__":
    sys.exit(main())
