//
//  UpdaterController.swift
//  Sparkle 自動アップデートの薄いラッパー
//
//  配布（DIST）ビルドのみ有効化する:
//  - 開発ビルドで有効だと、公開済みの新バージョンを検知して
//    開発中のアプリに更新ダイアログが出てしまう
//  - swift run などの未バンドル実行では Sparkle が正しく動作しない
//
//  ObservableObject 化し、新バージョン検知の状態を publish する。設定の「バージョン情報」
//  タブとホーム画面の「更新ピル」がこれを購読して表示を切り替える。
//
//  アップデート UX（Phase B・2026-07-03 指示）:
//  - 定期チェックは Sparkle の checkForUpdateInformation()（UI を一切出さない情報チェック）を
//    起動 5 分後＋以後 6 時間ごとに自前 Timer で回す。バックグラウンド検知でダイアログを勝手に
//    出さないため、Sparkle 自身の定期バックグラウンドチェック（更新検知でダイアログを出す）は
//    automaticallyChecksForUpdates=false で止める。
//  - 検知はホーム左上の「更新ピル」だけで通知し、対話フロー（DL→インストール）はユーザーが
//    ピル or 設定のボタンを押したときだけ checkForUpdates() で開始する。
//

import Foundation
import Sparkle

@MainActor
final class UpdaterController: NSObject, ObservableObject, SPUUpdaterDelegate {

    static let shared = UpdaterController()

    // controller は super.init() 後に self を delegate として渡すため var にする
    private var controller: SPUStandardUpdaterController?
    /// サイレント情報チェックのタイマー（初回=5 分後・以後 6 時間ごと）。プロセス生存中保持する
    private var silentCheckTimer: Timer?

    /// 検知された新バージョン（無ければ nil）。ホームのピルと「バージョン情報」タブが購読する。
    /// これを単一の情報源にし、updateAvailable / availableVersionString はここから導出する。
    @Published var availableVersion: String?

    /// 新バージョンが利用可能か（ホームの更新ピルの表示判定に使う）。
    /// @Published な availableVersion から導出するため、変化は購読側へ自動伝播する。
    var updateAvailable: Bool { availableVersion != nil }

    /// 既存の「バージョン情報」タブ（AboutTab）向けの別名。Phase B では AboutTab を変更しない。
    var availableVersionString: String? { availableVersion }

    /// 自動アップデートが有効か（メニュー項目・設定タブ・ホームのピルの表示判定に使う）
    var isAvailable: Bool { controller != nil }

    private override init() {
        super.init()
        // DIST ビルド かつ .app バンドルとして実行されているときのみ起動
        guard EmbeddedKeys.isDist, Bundle.main.bundlePath.hasSuffix(".app") else {
            return
        }
        // startingUpdater: true で updater を起動する。updaterDelegate に self を渡し、検知結果を
        // availableVersion へ反映する。
        controller = SPUStandardUpdaterController(
            startingUpdater: true,
            updaterDelegate: self,
            userDriverDelegate: nil
        )
        // Sparkle 自身の定期バックグラウンドチェック（更新検知でダイアログを勝手に出す）を止める。
        // 代わりに UI を出さない情報チェックを自前 Timer で回す（検知はホームのピルだけで通知する）。
        controller?.updater.automaticallyChecksForUpdates = false
        scheduleSilentChecks()
    }

    /// メニュー/設定/ホームのピルからの手動チェック。
    /// 新バージョン検知済みなら Sparkle の更新ダイアログ（DL→インストール）へ進む。
    func checkForUpdates() {
        controller?.checkForUpdates(nil)
    }

    /// サイレント情報チェックを予約する。起動直後の負荷を避けて 5 分後に初回、以後 6 時間ごと。
    /// checkForUpdateInformation() は UI を一切出さず、結果は SPUUpdaterDelegate 経由で反映される。
    private func scheduleSilentChecks() {
        silentCheckTimer = Timer.scheduledTimer(withTimeInterval: 5 * 60, repeats: false) { [weak self] _ in
            Task { @MainActor in
                guard let self else { return }
                self.performSilentCheck()
                // 初回後は 6 時間ごとに繰り返す
                self.silentCheckTimer = Timer.scheduledTimer(withTimeInterval: 6 * 60 * 60, repeats: true) { [weak self] _ in
                    Task { @MainActor in self?.performSilentCheck() }
                }
            }
        }
    }

    /// UI を出さない情報チェック（結果は delegate 経由で availableVersion に反映される）
    private func performSilentCheck() {
        controller?.updater.checkForUpdateInformation()
    }

    // MARK: - SPUUpdaterDelegate（チェック結果を publish）
    // SPUUpdaterDelegate は MainActor 非分離プロトコルのため nonisolated で満たし、
    // 値だけ取り出して MainActor にホップして publish する（Sparkle はメインで呼ぶが安全側）。

    nonisolated func updater(_ updater: SPUUpdater, didFindValidUpdate item: SUAppcastItem) {
        let version = item.displayVersionString
        Task { @MainActor in self.availableVersion = version }
    }

    nonisolated func updaterDidNotFindUpdate(_ updater: SPUUpdater) {
        Task { @MainActor in self.availableVersion = nil }
    }
}
