//
//  UpdaterController.swift
//  Sparkle 自動アップデートの薄いラッパー
//
//  build_release.sh で作った配布リリース（EmbeddedKeys.isRelease）だけで有効化する:
//  - GitHub Releases で配る作者署名の版（2026-10-01〜）は更新を受け取れるようにする
//  - ソースから自分でビルドした personal 版で有効だと、作者署名の版へ更新されて署名が変わり、
//    TCC（マイク・アクセシビリティ等）と Keychain の許可が外れてしまう
//  - 開発ビルドで有効だと、公開済みの新バージョンを検知して開発中のアプリに更新ダイアログが出てしまう
//  - swift run などの未バンドル実行では Sparkle が正しく動作しない
//
//  ObservableObject 化し、更新の状態を publish する。ホームの「更新ピル」・メニューバーのメニュー・
//  設定の「バージョン情報」タブがこれを見て表示を切り替える。
//
//  アップデート UX（2026-10-02 作り直し。意図:「窓をめったに開かない人でも確実に更新される／
//  押すなら 1 回で終わる」）:
//  - 確認とダウンロードは Sparkle 自身のスケジューラに任せる（automaticallyChecksForUpdates と
//    automaticallyDownloadsUpdates を ON・間隔は Info.plist の SUScheduledCheckInterval＝6 時間）。
//    自動ダウンロードが ON のとき Sparkle は SPUAutomaticUpdateDriver で裏 DL→展開まで進め、
//    ダイアログは出さない。以前の自前 Timer＋checkForUpdateInformation() は「検知だけ」で
//    DL しないため、押すとダイアログが数枚続いていた。
//  - 準備ができたら updater(_:willInstallUpdateOnQuit:immediateInstallationBlock:) で YES を返し、
//    即時インストールのブロックを預かる（Sparkle の UI を出させない）。ボタン 1 回でそのブロックを
//    呼べば、確認なしで入れ替え＋再起動する。
//  - 押さなくても、次にアプリを終了したときに Sparkle が自動で入れる（Mac のシャットダウン時は
//    Autoupdate が SIGTERM で即終了するため保証できない）。
//  - 録音中・変換中には再起動しない。押されたら（Sparkle 標準 UI からの再起動も）終わるのを待ってから入れる。
//  - 裏の確認・ダウンロード中に押されたら、準備でき次第そのまま入れ替える（Sparkle はセッション中の
//    checkForUpdates を黙って捨てるため、押した意思をこちらで覚えておく）。
//

import Combine
import Foundation
import Sparkle
import os.log

private let log = Logger(subsystem: "com.voicekey.app", category: "updater")

@MainActor
final class UpdaterController: NSObject, ObservableObject, SPUUpdaterDelegate {

    static let shared = UpdaterController()

    /// 待機に戻ってから入れ替えるまでの猶予。貼り付け後のクリップボード復元（Paster.restoreDelay＝1 秒）を
    /// 終えてから終了させないと、利用者のクリップボードが文字起こし結果のまま残るため
    private static let idleGraceBeforeInstall: TimeInterval = 2.0

    // controller は super.init() 後に self を delegate として渡すため var にする
    private var controller: SPUStandardUpdaterController?

    /// 検知された新バージョン（無ければ nil）。ホームのピルと「バージョン情報」タブが購読する。
    @Published var availableVersion: String?

    /// ダウンロードと展開が済み、ボタン 1 回で入れ替えられる版（無ければ nil）
    @Published private(set) var readyVersion: String?

    /// Sparkle から預かった即時インストールのブロック（呼ぶと入れ替え＋再起動。UI は出ない）
    private var immediateInstall: (() -> Void)?
    /// 録音中・変換中か（AppController の状態から結線する。未結線なら常に暇とみなす）
    private var isBusy = false
    private var busyObservation: AnyCancellable?

    /// 録音中・変換中なので待機に戻るまで預かっている入れ替え。ボタンの即時インストールと
    /// Sparkle の再起動延期（shouldPostponeRelaunch）の両方をここ 1 本で待たせる
    private var deferredInstall: (() -> Void)?
    /// 待機に戻ってから deferredInstall を呼ぶ予約（busy になったら取り消し、待機に戻るたびに積み直す）
    private var idleInstallWork: DispatchWorkItem?
    /// アプリ自前の再起動を更新で肩代わりしている（もう再起動すると決まっているので録音中でも待たない）
    private var bypassBusy = false

    /// 裏の確認・ダウンロード中に押された。準備ができたらそのまま入れ替える
    @Published private(set) var installWhenReady = false
    /// 録音中・変換中なので、終わってから入れ替える
    @Published private(set) var waitingForIdle = false
    /// 即時インストールのブロックを呼んだ（Sparkle が入れ替え＋再起動を進めている）
    @Published private(set) var installStarted = false

    /// 押した後の手応え（nil なら押せる状態）。ボタンはこの文言に置き換えて押せなくする
    var pendingInstallMessage: String? {
        if waitingForIdle { return "録音が終わったら更新します" }
        if installStarted { return "更新して再起動しています…" }
        if installWhenReady { return "ダウンロード中… 終わったら再起動します" }
        return nil
    }

    /// 新バージョンが利用可能か（ホームの更新ピルの表示判定に使う）。
    /// @Published な availableVersion から導出するため、変化は購読側へ自動伝播する。
    var updateAvailable: Bool { availableVersion != nil }

    /// 既存の「バージョン情報」タブ（AboutTab）向けの別名
    var availableVersionString: String? { availableVersion }

    /// 自動アップデートが有効か（メニュー項目・設定タブ・ホームのピルの表示判定に使う）
    var isAvailable: Bool { controller != nil }

    private override init() {
        super.init()
        // 配布リリース（build_release.sh 製）かつ .app バンドルとして実行されているときのみ起動
        guard EmbeddedKeys.isRelease, Bundle.main.bundlePath.hasSuffix(".app") else {
            return
        }
        // 設定を確定させてから起動する（起動後に変えると Sparkle が更新サイクルを組み直すため）
        let controller = SPUStandardUpdaterController(
            startingUpdater: false,
            updaterDelegate: self,
            userDriverDelegate: nil
        )
        // 旧版（〜2.1.0）は自動チェックを false にして user defaults へ永続化していたので、明示的に戻す。
        // 利用者が切り替える設定項目は無いので、毎起動で固定してよい
        controller.updater.automaticallyChecksForUpdates = true
        controller.updater.automaticallyDownloadsUpdates = true
        controller.startUpdater()
        self.controller = controller
    }

    /// 録音中・変換中かを流す publisher を結線する（StatusItemController が起動時に呼ぶ）。
    /// ここは状態を覚えるだけで、録音〜貼り付けの経路には何も足さない。
    func observeBusy<P: Publisher>(_ busy: P) where P.Output == Bool, P.Failure == Never {
        busyObservation = busy.sink { [weak self] busy in
            guard let self else { return }
            self.isBusy = busy
            if busy {
                // 猶予中にまた録音が始まったら予約を捨てる（「最後に待機へ戻ってから 2 秒」にするため）
                self.idleInstallWork?.cancel()
                self.idleInstallWork = nil
            } else {
                self.scheduleDeferredInstall()
            }
        }
    }

    /// 預かっている入れ替えを、待機に戻ってから idleGraceBeforeInstall 後に呼ぶ予約を積み直す。
    /// 待機に戻った直後はクリップボードの復元が残っているので、すぐには呼ばない。
    private func scheduleDeferredInstall() {
        guard deferredInstall != nil, !isBusy else { return }
        idleInstallWork?.cancel()
        let work = DispatchWorkItem { [weak self] in
            guard let self, !self.isBusy else { return }
            self.idleInstallWork = nil
            self.takeDeferredInstall()?()
        }
        idleInstallWork = work
        DispatchQueue.main.asyncAfter(deadline: .now() + Self.idleGraceBeforeInstall, execute: work)
    }

    /// 録音中・変換中なら待機に戻るまで預ける（予約は待機に戻ったときに observeBusy が積む）。
    private func deferUntilIdle(_ install: @escaping () -> Void) {
        deferredInstall = install
        waitingForIdle = true
    }

    /// 預かっている入れ替えを取り出す（同じブロックを二度呼ばないよう、取り出したら空にする）
    private func takeDeferredInstall() -> (() -> Void)? {
        let install = deferredInstall
        deferredInstall = nil
        waitingForIdle = false
        idleInstallWork?.cancel()
        idleInstallWork = nil
        return install
    }

    /// 「更新して再起動」（ホームのピル・メニュー・設定のボタン）。確認ダイアログは出さない。
    /// - 準備済み: 入れ替え＋再起動（録音中なら終わってから）
    /// - 裏の確認・ダウンロード中: 押した意思を覚え、準備でき次第入れ替える
    /// - それ以外（検知だけで裏 DL しない更新など）: Sparkle の対話フローへ
    func installUpdate() {
        guard immediateInstall != nil else {
            // セッション中の checkForUpdates は Sparkle が黙って捨てるので、呼べるときだけ呼ぶ
            if controller?.updater.canCheckForUpdates == true {
                checkForUpdates()
            } else if controller != nil {
                log.notice("ダウンロード中に押されたので、準備でき次第更新します")
                installWhenReady = true
            }
            return
        }
        guard !installStarted, deferredInstall == nil else { return }
        if isBusy {
            log.notice("録音中・変換中のため、終わってから更新します")
            deferUntilIdle { [weak self] in self?.startImmediateInstall() }
            return
        }
        startImmediateInstall()
    }

    /// 預かった即時インストールのブロックを 1 度だけ呼ぶ（Sparkle が入れ替え＋再起動する）
    private func startImmediateInstall() {
        guard let immediateInstall, !installStarted else { return }
        installStarted = true
        installWhenReady = false
        log.notice("準備済みの更新を入れて再起動します (version=\(self.readyVersion ?? "?", privacy: .public))")
        immediateInstall()
    }

    /// アプリ自前の再起動（オーディオ停止からの復帰・オンボーディング）を、準備済みの更新で肩代わりする。
    /// Sparkle は最初のインスタンスの終了しか見ないので、更新を抱えたまま `open -n` で自前の再起動をすると、
    /// 新しいインスタンスの起動中に .app が差し替わってしまう。呼び出し側はもう再起動すると決めているので、
    /// 録音中・変換中でも待たない。
    /// - Returns: 引き受けたら true（呼び出し側は自前の再起動をしない）。準備済みの更新が無ければ false。
    func relaunchIntoPreparedUpdate() -> Bool {
        guard immediateInstall != nil else { return false }
        bypassBusy = true
        log.notice("アプリの再起動を、準備済みの更新での再起動に置き換えます")
        if let deferred = takeDeferredInstall() {
            deferred()
        } else {
            startImmediateInstall()
        }
        return true
    }

    /// 手動の「アップデートを確認」。新バージョンがあれば Sparkle の更新ダイアログへ進む。
    /// 裏の確認・ダウンロード中や準備済みのときは Sparkle が何もしない（その間はピル等から入れる）。
    func checkForUpdates() {
        controller?.checkForUpdates(nil)
    }

    // MARK: - SPUUpdaterDelegate（状態を publish）
    // SPUUpdaterDelegate は MainActor 非分離プロトコルのため nonisolated で満たす。Sparkle はこれらを
    // メインスレッドから呼ぶので、MainActor.assumeIsolated で同期的に反映する
    // （Task { @MainActor } は常駐状態で App Nap により遅れることがある）。

    nonisolated func updater(_ updater: SPUUpdater, didFindValidUpdate item: SUAppcastItem) {
        let version = item.displayVersionString
        MainActor.assumeIsolated { self.availableVersion = version }
    }

    nonisolated func updaterDidNotFindUpdate(_ updater: SPUUpdater) {
        MainActor.assumeIsolated { self.availableVersion = nil }
    }

    /// 裏で DL→展開まで済んだ。YES を返して Sparkle の UI を出させず、即時インストールのブロックを預かる。
    /// YES の間は Sparkle の次の更新サイクルが止まるが、終了時のインストールは Sparkle が必ず行う。
    nonisolated func updater(
        _ updater: SPUUpdater,
        willInstallUpdateOnQuit item: SUAppcastItem,
        immediateInstallationBlock immediateInstallHandler: @escaping () -> Void
    ) -> Bool {
        let version = item.displayVersionString
        MainActor.assumeIsolated {
            log.notice("更新の準備ができました (version=\(version, privacy: .public))")
            self.immediateInstall = immediateInstallHandler
            self.availableVersion = version
            self.readyVersion = version
            // ダウンロード中に押されていたら、そのまま入れ替える（録音中なら終わってから）。
            // ブロック自体がメインへ非同期に積むので、ここで呼んでも YES を返す前に入れ替えは始まらない
            if self.installWhenReady {
                self.installUpdate()
            }
        }
        return true
    }

    /// 再起動の直前に Sparkle が聞いてくる。即時インストールのブロック経由でも、Sparkle 標準 UI
    /// （重要な更新・自動 DL 失敗時のダイアログ等）経由でも通るので、録音中を避ける判定はここに一本化する。
    /// Sparkle はこれを 1 回の入れ替えにつき 1 度しか呼ばない（2 度目は延期せずに進む）。
    nonisolated func updater(
        _ updater: SPUUpdater,
        shouldPostponeRelaunchForUpdate item: SUAppcastItem,
        untilInvokingBlock installHandler: @escaping () -> Void
    ) -> Bool {
        MainActor.assumeIsolated {
            guard self.isBusy, !self.bypassBusy else { return false }
            // ボタン経由は即時インストールを呼ぶ前に待機を確かめているので、ここで待つのは
            // 呼んだ直後に録音が始まった場合か、Sparkle 標準 UI からの再起動の場合だけ（二重には待たない）
            log.notice("録音中・変換中のため、終わってから再起動します")
            self.deferUntilIdle(installHandler)
            return true
        }
    }

    /// 更新サイクルが終わった（新版なし・失敗・ダイアログを閉じた等）。預かっていたブロックは
    /// もう効かないので捨て、押した後の表示も戻す（「ダウンロード中…」のまま固まらないように）。
    /// 準備済みでブロックを預かっている間は Sparkle のセッションが続くので、ここには来ない。
    nonisolated func updater(
        _ updater: SPUUpdater,
        didFinishUpdateCycleFor updateCheck: SPUUpdateCheck,
        error: (any Error)?
    ) {
        MainActor.assumeIsolated {
            self.installWhenReady = false
            self.installStarted = false
            self.immediateInstall = nil
            self.readyVersion = nil
            _ = self.takeDeferredInstall()
        }
    }
}
