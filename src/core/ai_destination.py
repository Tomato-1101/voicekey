"""
貼り付け先が AI（チャットアプリ・ターミナルの CLI）かの判定（Mac の AIDestination.swift と同じ趣旨）

Claude などの AI は「えー」「あの」入りの文でも意味が分かるので、貼り付け先が AI のときは
整形（Groq の LLM 呼び出し）を省いて 0.2〜0.5 秒速く・安く入力する
（2026-10-19 Tomato 指示「貼り付け先が AI のときに文の整形を無しにして自動オフにして。
この機能のオンオフも設定でできるようにして」）。

判定は録音開始時の最前面アプリの実行ファイル名だけで行う（貼り付け直前に問い合わせて待ち時間を足さない）。
ブラウザで chatgpt.com 等を開いている場合は対象外（タブの URL を取るには権限と手間が要るため）。
省くのは整形だけ。数字変換・辞書の置換・翻訳して入力は今どおり動かす。
"""

from typing import Optional

# AI とみなすアプリの実行ファイル名（小文字で持ち、比較も小文字で行う＝Windows のファイル名は大小を区別しないため）。
# 足すときは理由を 1 行添えること。ここが唯一の置き場所（Mac は macos/.../Core/AIDestination.swift）。
AI_APP_EXES = frozenset({
    # --- AI チャットアプリ（文の意味さえ通じればよい相手） ---
    "claude.exe",            # Claude デスクトップ
    "chatgpt.exe",           # ChatGPT デスクトップ
    "perplexity.exe",        # Perplexity（※ 実行ファイル名は未確認）
    # --- ターミナル類（Claude Code・Codex CLI など AI の CLI へ話しかける前提） ---
    "windowsterminal.exe",   # Windows ターミナル
    "wt.exe",                # Windows ターミナルの起動用（前面のプロセスとして見えることがある）
    "powershell.exe",        # Windows PowerShell（従来のコンソール窓）
    "pwsh.exe",              # PowerShell 7
    "cmd.exe",               # コマンドプロンプト
    "conhost.exe",           # 従来のコンソール窓のホスト（窓の持ち主として見える場合の保険）
    "alacritty.exe",         # Alacritty
    "wezterm-gui.exe",       # WezTerm
    "warp.exe",              # Warp（※ Windows 版の実行ファイル名は未確認）
    # --- AI 前提のエディタ（チャット欄・エージェントへの指示に貼ることが多い） ---
    "cursor.exe",            # Cursor
})

# [計測] の整形欄に出す表記（空白を含めない＝集計スクリプトが空白区切りで読むため・Mac と同じ）
SKIPPED_LABEL = "省略(AI宛て)"


def is_ai_app(exe_name: Optional[str]) -> bool:
    """実行ファイル名（例: "claude.exe"）が AI とみなす貼り付け先か。

    Args:
        exe_name: 最前面アプリの実行ファイル名（取れなければ None）

    Returns:
        AI とみなすなら True
    """
    if not exe_name:
        return False
    return exe_name.strip().lower() in AI_APP_EXES


def should_skip_format(format_enabled: bool, skip_for_ai: bool, exe_name: Optional[str]) -> bool:
    """この録音で整形を省くか（純関数）。

    Args:
        format_enabled: 録音キーの整形 ON/OFF（OFF ならそもそも整形しないので「省く」ことにはならない）
        skip_for_ai: 設定「貼り付け先が AI のときは整形しない」
        exe_name: 録音開始時の最前面アプリの実行ファイル名

    Returns:
        整形を省くなら True
    """
    return bool(format_enabled and skip_for_ai and is_ai_app(exe_name))
