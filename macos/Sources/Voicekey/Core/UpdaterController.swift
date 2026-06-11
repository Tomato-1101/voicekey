//
//  UpdaterController.swift
//  Sparkle 自動アップデートの薄いラッパー
//
//  配布（DIST）ビルドのみ有効化する:
//  - 開発ビルドで有効だと、公開済みの新バージョンを検知して
//    開発中のアプリに更新ダイアログが出てしまう
//  - swift run などの未バンドル実行では Sparkle が正しく動作しない
//

import Foundation
import Sparkle

@MainActor
final class UpdaterController {

    static let shared = UpdaterController()

    private let controller: SPUStandardUpdaterController?

    /// 自動アップデートが有効か（メニュー項目の表示判定に使う）
    var isAvailable: Bool { controller != nil }

    private init() {
        // DIST ビルド かつ .app バンドルとして実行されているときのみ起動
        guard EmbeddedKeys.isDist, Bundle.main.bundlePath.hasSuffix(".app") else {
            controller = nil
            return
        }
        // startingUpdater: true で初期化時から定期チェック（SUScheduledCheckInterval）が動く
        controller = SPUStandardUpdaterController(
            startingUpdater: true,
            updaterDelegate: nil,
            userDriverDelegate: nil
        )
    }

    /// メニューの「アップデートを確認…」から手動チェック
    func checkForUpdates() {
        controller?.checkForUpdates(nil)
    }
}
