//
//  AIDestination.swift
//  貼り付け先が AI（チャットアプリ・ターミナルの CLI）かの判定
//
//  Claude などの AI は「えー」「あの」入りの文でも意味が分かるので、貼り付け先が AI のときは
//  整形（Groq の LLM 呼び出し）を省いて 0.2〜0.5 秒速く・安く入力する
//  （2026-10-19 Tomato 指示「貼り付け先が AI のときに文の整形を無しにして自動オフにして。
//  この機能のオンオフも設定でできるようにして」）。
//
//  判定は録音開始時の最前面アプリの bundle ID だけで行う（貼り付け直前に問い合わせて待ち時間を足さない）。
//  ブラウザで chatgpt.com 等を開いている場合は対象外（タブの URL を取るには権限と手間が要るため）。
//  省くのは整形だけ。数字変換・辞書の置換・翻訳して入力は今どおり動かす。
//

import Foundation

/// 貼り付け先が AI かの判定（純関数・テストで固定する）
enum AIDestination {

    /// AI とみなすアプリの bundle ID（小文字で持ち、比較も小文字で行う＝LaunchServices は大小を区別しないため）。
    ///
    /// 足すときは理由を 1 行添えること。ここが唯一の置き場所（Windows は `src/core/ai_destination.py`）。
    static let bundleIDs: Set<String> = [
        // --- AI チャットアプリ（文の意味さえ通じればよい相手） ---
        "com.anthropic.claudefordesktop",   // Claude デスクトップ
        "com.openai.chat",                  // ChatGPT デスクトップ
        "ai.perplexity.mac",                // Perplexity（※ bundle ID は未確認。実機で違えば直す）

        // --- ターミナル類（Claude Code・Codex CLI など AI の CLI へ話しかける前提） ---
        "com.apple.terminal",               // ターミナル（標準）
        "com.googlecode.iterm2",            // iTerm2
        "com.mitchellh.ghostty",            // Ghostty
        "dev.warp.warp-stable",             // Warp（安定版）
        "dev.warp.warp-preview",            // Warp（プレビュー版。※ 未確認）
        "net.kovidgoyal.kitty",             // kitty
        "org.alacritty",                    // Alacritty（※ 未確認。io.alacritty の版もあるかもしれない）
        "com.github.wez.wezterm",           // WezTerm

        // --- AI 前提のエディタ（チャット欄・エージェントへの指示に貼ることが多い） ---
        "com.todesktop.230313mzl4w4u92",    // Cursor（ToDesktop 製なので ID が数字列）
    ]

    /// その bundle ID のアプリが AI とみなす貼り付け先か
    static func isAI(bundleID: String?) -> Bool {
        guard let bundleID, !bundleID.isEmpty else { return false }
        return bundleIDs.contains(bundleID.lowercased())
    }

    /// この録音で整形を省くか（純関数）。
    /// - Parameters:
    ///   - formatEnabled: 録音キーの整形 ON/OFF（OFF ならそもそも整形しないので「省く」ことにはならない）
    ///   - skipForAIEnabled: 設定「貼り付け先が AI のときは整形しない」
    ///   - bundleID: 録音開始時の最前面アプリ
    static func shouldSkipFormat(formatEnabled: Bool, skipForAIEnabled: Bool, bundleID: String?) -> Bool {
        formatEnabled && skipForAIEnabled && isAI(bundleID: bundleID)
    }

    /// `[計測]` の整形欄に出す表記（空白を含めない＝集計スクリプトが空白区切りで読むため）
    static let skippedLabel = "省略(AI宛て)"
}
