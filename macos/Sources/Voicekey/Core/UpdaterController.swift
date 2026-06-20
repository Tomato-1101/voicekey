//
//  UpdaterController.swift
//  Sparkle 自動アップデートの薄いラッパー
//
//  配布（DIST）ビルドのみ有効化する:
//  - 開発ビルドで有効だと、公開済みの新バージョンを検知して
//    開発中のアプリに更新ダイアログが出てしまう
//  - swift run などの未バンドル実行では Sparkle が正しく動作しない
//
//  ObservableObject 化し、バックグラウンド/手動チェックで見つかった新バージョンを
//  publish する。設定の「バージョン情報」タブがこれを購読して「今すぐ更新する」
//  ボタンの表示を切り替える（Sparkle 既定の更新ダイアログ・メニュー導線はそのまま残す）。
//

import Foundation
import Sparkle

@MainActor
final class UpdaterController: NSObject, ObservableObject, SPUUpdaterDelegate {

    static let shared = UpdaterController()

    // controller は super.init() 後に self を delegate として渡すため var にする
    private var controller: SPUStandardUpdaterController?

    /// バックグラウンド/手動チェックで見つかった新バージョン（無ければ nil）。
    /// 「バージョン情報」タブが購読し、検知時のみ「今すぐ更新する」ボタンを出す。
    @Published var availableVersionString: String?

    /// 自動アップデートが有効か（メニュー項目・設定タブの表示判定に使う）
    var isAvailable: Bool { controller != nil }

    private override init() {
        super.init()
        // DIST ビルド かつ .app バンドルとして実行されているときのみ起動
        guard EmbeddedKeys.isDist, Bundle.main.bundlePath.hasSuffix(".app") else {
            return
        }
        // startingUpdater: true で初期化時から定期チェック（SUScheduledCheckInterval）が動く。
        // updaterDelegate に self を渡し、検知結果を availableVersionString へ反映する。
        controller = SPUStandardUpdaterController(
            startingUpdater: true,
            updaterDelegate: self,
            userDriverDelegate: nil
        )
    }

    /// メニュー/設定の「アップデートを確認…」から手動チェック。
    /// 新バージョン検知済みなら Sparkle の更新ダイアログ（DL→インストール）へ進む。
    func checkForUpdates() {
        controller?.checkForUpdates(nil)
    }

    // MARK: - SPUUpdaterDelegate（チェック結果を publish）
    // SPUUpdaterDelegate は MainActor 非分離プロトコルのため nonisolated で満たし、
    // 値だけ取り出して MainActor にホップして publish する（Sparkle はメインで呼ぶが安全側）。

    nonisolated func updater(_ updater: SPUUpdater, didFindValidUpdate item: SUAppcastItem) {
        let version = item.displayVersionString
        Task { @MainActor in self.availableVersionString = version }
    }

    nonisolated func updaterDidNotFindUpdate(_ updater: SPUUpdater) {
        Task { @MainActor in self.availableVersionString = nil }
    }
}
