/// SwiftUI 専用の `.translationTask` を AppKit アプリから使うためのホスト
///
/// なぜ必要か（実測）:
/// `TranslationSession(installedSource:target:)` で直接作ったセッションは
/// **`canRequestDownloads` が false** で、言語モデルが未導入だと
/// `prepareTranslation()` でダウンロードを要求できない（本アプリで実測: 英→日が
/// `.supported`（未インストール）の状態で false）。
/// ダウンロードの確認 UI を出せるのは `.translationTask` が渡してくるセッションだけなので、
/// 画面外に 1x1 の透明ウィンドウを置き、そこに載せた SwiftUI ビューからセッションを受け取る。
///
/// `.translationTask` の action クロージャは「返った時点でセッションが無効」になるため、
/// クロージャの中で待ち続けてセッションを生かしたままにする。
import AppKit
import Foundation
import OSLog
import SwiftUI
@preconcurrency import Translation

/// `.translationTask` からセッションを受け取って保持する箱
@MainActor
@available(macOS 26.0, *)
final class AppleTranslationHostModel: ObservableObject {

    /// `.translationTask` に渡す構成（設定するとセッション生成が走る）
    @Published var configuration: TranslationSession.Configuration?

    private var session: TranslationSession?
    private var waiters: [CheckedContinuation<TranslationSession?, Never>] = []

    /// セッションが用意できるまで待つ
    ///
    /// - Returns: セッション。一定時間内に用意できなければ nil
    func waitForSession(timeout: TimeInterval) async -> TranslationSession? {
        if let session { return session }
        return await withCheckedContinuation { continuation in
            waiters.append(continuation)
            // SwiftUI 側が何らかの理由で走らなかった場合に永久に待たないための保険
            Task { [weak self] in
                try? await Task.sleep(for: .seconds(timeout))
                self?.failWaiters()
            }
        }
    }

    /// セッションを受け取り、ビューが生きている限り保持し続ける
    ///
    /// このクロージャから return するとセッションが無効化されるため、意図的に返さない。
    func hold(_ session: TranslationSession) async {
        self.session = session
        let pending = waiters
        waiters.removeAll()
        for continuation in pending { continuation.resume(returning: session) }

        while !Task.isCancelled {
            try? await Task.sleep(for: .seconds(3600))
        }
        self.session = nil
    }

    /// 待っている呼び出しを nil で打ち切る
    private func failWaiters() {
        guard session == nil, !waiters.isEmpty else { return }
        let pending = waiters
        waiters.removeAll()
        for continuation in pending { continuation.resume(returning: nil) }
    }
}

/// セッションを受け取るビュー
///
/// 普段は画面外に置いて見えないが、翻訳モデルのダウンロード承認 UI が要るときだけ
/// 画面中央に出す。そのとき何が起きているのか分からないと不気味なので、説明を書いておく。
@available(macOS 26.0, *)
private struct AppleTranslationHostView: View {
    @ObservedObject var model: AppleTranslationHostModel

    var body: some View {
        VStack(spacing: 10) {
            Text("英語 → 日本語の翻訳モデル")
                .font(.headline)
            Text("macOS のダウンロード確認が表示されます。\n許可すると字幕の日本語訳が始まります。")
                .font(.callout)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
        }
        .padding(20)
        .frame(width: Self.windowSize.width, height: Self.windowSize.height)
        .translationTask(model.configuration) { session in
            await model.hold(session)
        }
    }

    /// 承認 UI を出すときのウィンドウサイズ
    static let windowSize = CGSize(width: 420, height: 150)
}

/// 画面外ホストウィンドウの管理
@MainActor
@available(macOS 26.0, *)
enum AppleTranslationHost {

    private static let logger = makeCaptionLogger("AppleTranslationHost")
    private static let model = AppleTranslationHostModel()
    private static var window: NSWindow?

    /// 翻訳セッションを取得する（初回はホストを組み立てる）
    ///
    /// - Parameters:
    ///   - source: 翻訳元言語
    ///   - target: 翻訳先言語
    ///   - timeout: セッションが来るまで待つ上限（秒）
    /// - Returns: セッション。用意できなければ nil
    static func session(
        source: Locale.Language,
        target: Locale.Language,
        timeout: TimeInterval = 10
    ) async -> TranslationSession? {
        installIfNeeded(source: source, target: target)
        return await model.waitForSession(timeout: timeout)
    }

    /// ダウンロード承認 UI を出せるようにウィンドウを画面中央へ出す
    ///
    /// 実測: ウィンドウが画面外・alpha 0 のままだと OS 側の承認 UI を出せず、
    /// `TranslationErrorDomain Code=14`（remote UI finished but didn't get finished configuration）
    /// で失敗する。承認が要るときだけ本物のウィンドウとして見せる必要がある。
    static func presentForApproval() {
        guard let window else { return }
        logger.notice("翻訳モデルのダウンロード承認 UI を表示します（ユーザー操作待ち）")
        window.setContentSize(AppleTranslationHostView.windowSize)
        window.center()
        window.alphaValue = 1
        window.ignoresMouseEvents = false
        NSApp.activate(ignoringOtherApps: true)
        window.makeKeyAndOrderFront(nil)
    }

    /// 承認 UI 用の表示を畳んで画面外へ戻す
    static func dismissApproval() {
        guard let window else { return }
        window.orderOut(nil)
        window.alphaValue = 0
        window.ignoresMouseEvents = true
        window.setFrameOrigin(NSPoint(x: -10_000, y: -10_000))
    }

    /// 画面外にホストビューを載せたウィンドウを用意する
    ///
    /// 「非表示ウィンドウ」ではなく「見えない位置に表示されたウィンドウ」にしているのは、
    /// SwiftUI の task 系モディファイアはビューがウィンドウに載って初めて走るため。
    private static func installIfNeeded(source: Locale.Language, target: Locale.Language) {
        guard window == nil else { return }

        model.configuration = TranslationSession.Configuration(source: source, target: target)

        let hosting = NSHostingView(rootView: AppleTranslationHostView(model: model))
        hosting.frame = NSRect(origin: .zero, size: AppleTranslationHostView.windowSize)

        // 承認 UI（シート）を受けられるよう、最初から titled の実ウィンドウにしておく
        let hostWindow = NSWindow(
            contentRect: NSRect(origin: NSPoint(x: -10_000, y: -10_000), size: AppleTranslationHostView.windowSize),
            styleMask: [.titled, .closable],
            backing: .buffered,
            defer: false
        )
        hostWindow.title = "subglass — 翻訳モデルの準備"
        hostWindow.contentView = hosting
        hostWindow.alphaValue = 0
        hostWindow.ignoresMouseEvents = true
        hostWindow.isReleasedWhenClosed = false
        hostWindow.collectionBehavior = [.stationary, .ignoresCycle]
        hostWindow.orderBack(nil)
        window = hostWindow

        logger.notice("Apple 翻訳セッションのホストを作成しました")
    }
}
