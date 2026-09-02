"""
ビルド種別マーカー生成スクリプト（Windows 版）

src/config/embedded_keys.py（git 管理外）を生成する。このモジュールはビルド種別の
フラグだけを持つ（Mac の macos/scripts/generate_embedded_keys.sh と同じ役割）。

- 引数なし ........ 配布（DIST）ビルド。IS_DIST=True。is_dist_build() / 設定画面の
                    API キータブ非表示 / 自動アップデート有効化の判定に使う。
- --personal ...... 個人用最速版（personal）。IS_PERSONAL=True・IS_DIST=False。
                    Credential Manager のキーで直叩きし、自社サーバー・ログインを一切通らない
                    （Mac の EmbeddedKeys.isPersonal と同じ。詳細は docs/PERSONAL_EDITION.md）。

重要（2026-06-28 セキュリティ修正）:
- 長期プロバイダーキーは配布バイナリに 1 バイトも埋め込まない。製品版（release）は
  文字起こし・整形をすべて自社サーバー経由（短命 JWT 直叩き / プロキシ）で行うため、
  アプリ側にプロバイダーキーは不要。以前の XOR 難読化埋め込みは暗号化ではなく
  （マスクも同じバイナリに同梱され復元可能）、漏洩源だったため撤去した。
- よってこのスクリプトは API キーを一切読まない（.env.dist も環境変数も参照しない）。
- 生成された embedded_keys.py は絶対に git にコミットしない（.gitignore 済み）。

使い方:
    python scripts/build/generate_embedded_keys.py             # 配布ビルド用
    python scripts/build/generate_embedded_keys.py --personal  # 個人用最速版
"""

import sys
from pathlib import Path

# Windows の既定 stdout は cp1252（英語ロケール）で、日本語の進捗 print が
# UnicodeEncodeError になりスクリプトごと落ちる（CI の Windows ランナーで実害）。
# 出力を UTF-8 に固定して、どのロケールでも日本語メッセージを安全に出せるようにする。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

# リポジトリ直下（このファイルは scripts/build/ にある）
ROOT = Path(__file__).resolve().parent.parent.parent
OUT = ROOT / "src" / "config" / "embedded_keys.py"

_TEMPLATE = '''"""
（自動生成）ビルド種別マーカー

git にコミット絶対禁止（.gitignore 済み）。手で編集しない。
生成元: scripts/build/generate_embedded_keys.py

配布バイナリには長期プロバイダーキーを埋め込まない（製品版は自社サーバー経由）。
このモジュールはビルド種別を示すフラグだけを持つ。
"""

# 配布（DIST）ビルドかどうか。設定画面の API キータブ非表示や自動アップデートの判定に使う
IS_DIST = {is_dist}
# 個人用最速版（personal）かどうか。True なら認証セッションを常に無視し、
# Credential Manager のキーで直叩きする（サーバー往復ゼロ）
IS_PERSONAL = {is_personal}
'''


def main(argv=()) -> int:
    """マーカーを生成する。argv はコマンドライン引数（スクリプト名を除く）。"""
    personal = False
    for arg in argv:
        if arg == "--personal":
            personal = True
        else:
            print(f"エラー: 不明な引数: {arg}（使えるのは --personal のみ）", file=sys.stderr)
            return 2
    OUT.write_text(
        _TEMPLATE.format(is_dist=not personal, is_personal=personal),
        encoding="utf-8",
    )
    if personal:
        print(f"==> 生成: {OUT}（personal マーカー・IS_DIST=False・キー埋め込みなし＝Credential Manager 直読）")
    else:
        print(f"==> 生成: {OUT}（DIST マーカー・キー埋め込みなし）")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
