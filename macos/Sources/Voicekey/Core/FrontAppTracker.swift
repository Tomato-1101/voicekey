//
//  FrontAppTracker.swift
//  前面アプリ（貼り付け先）の追跡
//
//  録音した音声の貼り付け先は「いま前面にあるアプリ」なので、その bundleID・名前・
//  アイコンを追跡する。HUD にアイコンを出したり、実績をアプリ別に集計するために使う。
//
//  voicekey 自身（設定ウィンドウ等）が前面のときは貼り付け先ではないため、直前に前面
//  だった他アプリを保持して返す（設定を開いたまま録音するケースの取り違えを防ぐ）。
//

import AppKit
import Foundation

/// 前面アプリを追跡する。録音開始時のスナップショット表示と、貼り付け直前の記録に使う。
@MainActor
final class FrontAppTracker {

    /// 貼り付け先アプリのスナップショット（記録用の軽量値。アイコンは含めない）
    struct Snapshot: Equatable {
        let bundleID: String?
        let name: String?
    }

    /// 自アプリの bundleID（前面がこれなら貼り付け先ではないと判断する）
    private let selfBundleID = "com.voicekey.app"
    /// 直前に前面だった「自分以外」のアプリ（自分が前面のときのフォールバック）
    private var lastNonSelf: NSRunningApplication?

    init() {
        // 起動直後の初期値を取り込む
        if let front = NSWorkspace.shared.frontmostApplication,
           front.bundleIdentifier != selfBundleID {
            lastNonSelf = front
        }
        // アプリ切替のたびに「自分以外の最前面」を控えておく
        NSWorkspace.shared.notificationCenter.addObserver(
            forName: NSWorkspace.didActivateApplicationNotification,
            object: nil, queue: .main
        ) { [weak self] note in
            guard let self else { return }
            let app = note.userInfo?[NSWorkspace.applicationUserInfoKey] as? NSRunningApplication
            MainActor.assumeIsolated {
                if let app, app.bundleIdentifier != self.selfBundleID {
                    self.lastNonSelf = app
                }
            }
        }
    }

    /// 現在の貼り付け先アプリ（自分が前面なら直前の他アプリ）
    private var currentApp: NSRunningApplication? {
        if let front = NSWorkspace.shared.frontmostApplication,
           front.bundleIdentifier != selfBundleID {
            return front
        }
        return lastNonSelf
    }

    /// 記録用のスナップショット（bundleID・名前）
    func snapshot() -> Snapshot {
        let app = currentApp
        return Snapshot(bundleID: app?.bundleIdentifier, name: app?.localizedName)
    }

    /// HUD 表示用のアプリアイコン（取得できなければ nil）
    func currentIcon() -> NSImage? {
        currentApp?.icon
    }
}
