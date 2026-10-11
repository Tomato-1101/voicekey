/// Meet ボットの通知
///
/// ボットは見えない Chrome で動くので、参加できた・承認待ち・断られた・終わった を
/// 本人が気づける手段が通知しかない。メニューを開かないと分からない状態を無くすために出す。
///
/// **会議の発言（文字起こし本文）は通知に載せない**（通知センターに残り、画面共有で見えるため）。
import Foundation
import OSLog
import UserNotifications

/// ボットの出来事を macOS の通知で知らせる
@MainActor
final class MeetBotNotifier {

    private let logger = makeCaptionLogger("MeetBotNotifier")

    /// 通知の許可を一度でも求めたか
    private var authorizationRequested = false

    /// 通知センターが使えるか
    ///
    /// `.app` として起動していないプロセス（`swift test` など）で `UNUserNotificationCenter` に
    /// 触ると落ちるので、アプリとして動いているときだけ使う。
    private var isUsable: Bool {
        Bundle.main.bundleURL.pathExtension == "app"
    }

    /// 通知の許可を求める（初めてボットを使うときに 1 回だけ）
    func prepare() {
        guard isUsable, !authorizationRequested else { return }
        authorizationRequested = true
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound]) { [logger] granted, error in
            if let error {
                logger.error("通知の許可を求められません: \(error.localizedDescription, privacy: .public)")
            } else if !granted {
                logger.notice("通知が許可されていません（ボットの状態はメニューと設定画面で確認できます）")
            }
        }
    }

    /// 1 件通知する
    ///
    /// - Parameter body: 本文（会議の発言は入れないこと）
    func post(_ body: String) {
        guard isUsable else { return }
        let content = UNMutableNotificationContent()
        content.title = "議事録ボット"
        content.body = body
        let request = UNNotificationRequest(identifier: UUID().uuidString, content: content, trigger: nil)
        UNUserNotificationCenter.current().add(request) { [logger] error in
            if let error {
                logger.error("通知を出せません: \(error.localizedDescription, privacy: .public)")
            }
        }
    }
}
