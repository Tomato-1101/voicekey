//
//  AppState.swift
//  アプリ全体の状態定義
//

import Foundation

/// アプリケーションの状態。
/// メニューバーアイコンの色と HUD の表示内容はこの状態から一意に決まる。
enum AppState: Equatable {
    /// 待機中
    case idle
    /// 録音中（autoEnter: ダブルタップ自動 Enter / handsFree: 切替キー or toggle でハンズフリー）
    case recording(autoEnter: Bool, handsFree: Bool)
    /// 文字起こし中
    case transcribing

    /// 録音中か（サイドノッチの点灯など、録音状態だけを見たい箇所で使う）
    var isRecording: Bool {
        if case .recording = self { return true }
        return false
    }

    /// メニューに表示する日本語ラベル
    var label: String {
        switch self {
        case .idle: return "待機中"
        case .recording(let autoEnter, let handsFree):
            if handsFree { return "録音中（ハンズフリー）" }
            return autoEnter ? "録音中（自動送信）" : "録音中"
        case .transcribing: return "変換中…"
        }
    }
}
