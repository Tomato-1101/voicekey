//
//  ApiKeyPromptPolicy.swift
//  キー未設定・無効で失敗したときに「設定 › API キー」を自動で開くかの判定（純ロジック）
//
//  初めて使う人を入力先へ案内するため 1 回目は開く。ただし失敗のたびに開くと、
//  設定画面が前面に出て入力中のアプリからフォーカスを奪い続ける。そこで自動で開くのは
//  起動中 1 回だけにし、2 回目以降は HUD 通知だけで知らせる。
//

import Foundation

struct ApiKeyPromptPolicy {
    /// この起動中にすでに自動で開いたか
    private(set) var hasAutoOpened = false

    /// 今回の失敗で設定画面を自動で開くか。true を返すのは起動中の最初の 1 回だけ。
    ///
    /// - Returns: 開くなら true（呼んだ時点で「開いた」扱いになる）
    mutating func shouldAutoOpenSettings() -> Bool {
        guard !hasAutoOpened else { return false }
        hasAutoOpened = true
        return true
    }
}
