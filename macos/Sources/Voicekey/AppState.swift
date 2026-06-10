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
    /// 録音中（autoEnter: ダブルタップによる自動 Enter 送信モードか）
    case recording(autoEnter: Bool)
    /// 文字起こし中
    case transcribing

    /// メニューに表示する日本語ラベル
    var label: String {
        switch self {
        case .idle: return "待機中"
        case .recording(let autoEnter): return autoEnter ? "録音中（自動送信）" : "録音中"
        case .transcribing: return "変換中…"
        }
    }
}
