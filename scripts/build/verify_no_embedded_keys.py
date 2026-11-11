"""
配布生成物に長期プロバイダーキーが混入していないか検査する（漏洩回帰チェック）。

配布するのは personal 版で、利用者が自分の API キーを設定画面で入れる。作者のキーが
配布バイナリに 1 バイトでも混ざってはならない（Windows の exe・Mac の .app の両方で使う）。

使い方:
    python scripts/build/verify_no_embedded_keys.py <path> [<path> ...]

検査内容:
  1. 各 path（ファイル / ディレクトリ）をバイト列として走査し、既知のプロバイダーキーの
     接頭辞パターン（OpenAI sk- / Groq gsk_ / ElevenLabs sk_ / Gemini AIza）を探す。
  2. Mac の生成マーカー（EmbeddedKeys.generated.swift。build_release.sh が渡す）が
     キーレス（payload / mask / key(forService:) を持たない＝フラグのみ）であることを確認する。
     ※ Deepgram は固定接頭辞が無く誤検出を避けるためバイト走査の対象外。
     ※ Windows 版は生成マーカー（embedded_keys.py）を作らなくなったので、その検査は無い。

いずれかに該当すれば非 0 で終了し、ビルドを止める。
誤検出を避けるため、走査対象は自分のバイナリ・生成物に絞って渡すこと
（torch 等の OSS 同梱物が大量にあるディレクトリ全体は渡さない）。
Mac の .app バンドル（Sparkle 同梱）は全体を渡してよい（build_release.sh が zip と同じ中身を走査する）。
"""

import re
import sys
from pathlib import Path

# 既知のプロバイダーキー接頭辞。バイナリのノイズで誤検出しにくいよう接頭辞＋十分な桁を要求する。
KEY_PATTERNS = [
    re.compile(rb"sk-[A-Za-z0-9_-]{20,}"),  # OpenAI（sk-proj- 含む）
    re.compile(rb"gsk_[A-Za-z0-9]{20,}"),   # Groq
    # ElevenLabs。直前が英数字なら識別子の途中なので除外する（Swift のマングル名
    # `…translationTask_6actionQr0D00D7Session…`＝ View.translationTask(_:action:) に当たる誤検出の対策）
    re.compile(rb"(?<![A-Za-z0-9])sk_[A-Za-z0-9]{32,}"),
    # Gemini（Google API キー）。ライブ字幕の翻訳で利用者が入れるキーと同じ形式（AIza＋35 桁）
    re.compile(rb"AIza[0-9A-Za-z_-]{35}"),
]

# 埋め込み痕跡（旧 XOR 埋め込みの名残）の検出語。キーレスならどれも含まれない。
_BANNED_MARKER_TOKENS = ("payload", "_mask", "def get_key", "func get_key", "key(forservice")

_CHUNK = 1024 * 1024  # 1MB ずつ読む（巨大バイナリの全読み回避）


def _scan_bytes_file(path: Path) -> list:
    """ファイルをチャンク走査してヒットしたパターン名を返す。"""
    hits = set()
    try:
        with path.open("rb") as f:
            tail = b""
            while True:
                chunk = f.read(_CHUNK)
                if not chunk:
                    break
                buf = tail + chunk
                for pat in KEY_PATTERNS:
                    if pat.search(buf):
                        hits.add(pat.pattern.decode())
                tail = buf[-64:]  # チャンク境界をまたぐキーの取りこぼし対策
    except OSError:
        return []
    return sorted(hits)


def _iter_files(root: Path):
    if root.is_file():
        yield root
        return
    for p in root.rglob("*"):
        if p.is_file():
            yield p


def _marker_problems(root: Path) -> list:
    """Mac の生成マーカーにキー埋め込みの痕跡が無いか検査する。"""
    if root.is_file():
        candidates = [root] if root.name == "EmbeddedKeys.generated.swift" else []
    else:
        candidates = list(root.rglob("EmbeddedKeys.generated.swift"))
    problems = []
    for mod in candidates:
        try:
            text = mod.read_text(encoding="utf-8", errors="replace").lower()
        except OSError:
            continue
        for token in _BANNED_MARKER_TOKENS:
            if token in text:
                problems.append(f"{mod}: キー埋め込みの痕跡 '{token}' を検出")
    return problems


def main(argv) -> int:
    if not argv:
        print("使い方: verify_no_embedded_keys.py <path> [<path> ...]", file=sys.stderr)
        return 2

    leaks = {}
    marker_problems = []
    for arg in argv:
        root = Path(arg)
        if not root.exists():
            print(f"警告: 存在しないパスをスキップ: {root}", file=sys.stderr)
            continue
        for f in _iter_files(root):
            hits = _scan_bytes_file(f)
            if hits:
                leaks[str(f)] = hits
        marker_problems.extend(_marker_problems(root))

    if leaks or marker_problems:
        print("エラー: 配布生成物にプロバイダーキーの痕跡を検出しました", file=sys.stderr)
        for path, hits in leaks.items():
            print(f"  鍵パターン {hits} : {path}", file=sys.stderr)
        for p in marker_problems:
            print(f"  {p}", file=sys.stderr)
        return 1

    print("==> 漏洩チェック OK（プロバイダーキーの混入なし）")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
