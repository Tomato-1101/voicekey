/// Meet ボットの通知
///
/// ボットは見えない Chrome で動くので、参加できた・承認待ち・断られた・終わった を
/// 本人が気づける手段が通知しかない。メニューを開かないと分からない状態を無くすために出す。
///
/// **会議の発言（文字起こし本文）は通知に載せない**（通知センターに残り、画面共有で見えるため）。
import AppKit
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
    private static var isUsable: Bool {
        Bundle.main.bundleURL.pathExtension == "app"
    }
    private var isUsable: Bool { Self.isUsable }

    /// 通知センターの delegate（center 側は弱参照なので、ここで持ち続ける）
    ///
    /// voicekey で `UNUserNotificationCenter` を使うのはこのボットだけなので、delegate を
    /// ここで設定しても他の通知と取り合わない。
    private static let delegate = NotificationDelegate()

    /// アプリ起動時に delegate だけ付ける（許可は求めない）
    ///
    /// delegate が `prepare()` / `post()` のときにしか付かないと、再起動後に前の議事録の通知
    /// （通知センターに残っている）を押しても、その時点では delegate が無く何も開かない。
    /// ボット自体は参照するまで作らない設計なので、ここは通知センターに触るだけの最小限にする。
    static func installDelegateAtLaunch() {
        guard isUsable else { return }
        let center = UNUserNotificationCenter.current()
        if center.delegate !== delegate { center.delegate = delegate }
    }

    /// 通知の許可を求める（初めてボットを使うときに 1 回だけ）
    func prepare() {
        guard isUsable else { return }
        installDelegate()
        guard !authorizationRequested else { return }
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
    /// - Parameters:
    ///   - body: 本文（会議の発言は入れないこと）
    ///   - transcript: 押したときに開く議事録。`opensTranscript` が true で、これが無い・消えていれば保存フォルダを開く
    ///   - opensTranscript: 押したときに議事録（かフォルダ）を開くか（参加・承認待ちなどの通知では開かない）
    func post(_ body: String, transcript: URL? = nil, opensTranscript: Bool = false) {
        guard isUsable else { return }
        installDelegate()
        let content = UNMutableNotificationContent()
        content.title = "議事録ボット"
        content.body = body
        if opensTranscript {
            var info: [String: Any] = [NotificationDelegate.opensTranscriptKey: true]
            if let transcript { info[NotificationDelegate.transcriptPathKey] = transcript.path }
            content.userInfo = info
        }
        let request = UNNotificationRequest(identifier: UUID().uuidString, content: content, trigger: nil)
        UNUserNotificationCenter.current().add(request) { [logger] error in
            if let error {
                logger.error("通知を出せません: \(error.localizedDescription, privacy: .public)")
            }
        }
    }

    /// 通知センターに delegate を付ける（前面でもバナーを出し、押されたら議事録を開くため）
    private func installDelegate() {
        let center = UNUserNotificationCenter.current()
        if center.delegate !== Self.delegate { center.delegate = Self.delegate }
    }
}

/// 通知の表示と、押されたときの動作
///
/// 既定では、アプリが前面にいる間は通知が出ない（メニューバーのアプリはほぼ常に「前面扱い」になり得る）。
/// ボットの通知は見えない Chrome の唯一の知らせなので、前面でもバナーを出す。
final class NotificationDelegate: NSObject, UNUserNotificationCenterDelegate {

    /// 通知の userInfo に入れる「押したら議事録を開く」の印
    static let opensTranscriptKey = "voicekeyOpensTranscript"
    /// 通知の userInfo に入れる議事録のパス
    static let transcriptPathKey = "voicekeyTranscriptPath"

    func userNotificationCenter(
        _ center: UNUserNotificationCenter, willPresent notification: UNNotification,
        withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void
    ) {
        completionHandler([.banner, .list])
    }

    func userNotificationCenter(
        _ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse,
        withCompletionHandler completionHandler: @escaping () -> Void
    ) {
        let info = response.notification.request.content.userInfo
        if response.actionIdentifier == UNNotificationDefaultActionIdentifier,
           info[Self.opensTranscriptKey] as? Bool == true,
           let target = Self.target(path: info[Self.transcriptPathKey] as? String) {
            DispatchQueue.main.async { NSWorkspace.shared.open(target) }
        }
        completionHandler()
    }

    /// 開く先を決める（議事録が残っていればそれ、無ければ保存フォルダ、フォルダも無ければ nil）
    static func target(path: String?, directory: URL = TranscriptRecorder.directory) -> URL? {
        let manager = FileManager.default
        if let path, manager.fileExists(atPath: path) { return URL(fileURLWithPath: path) }
        return manager.fileExists(atPath: directory.path) ? directory : nil
    }
}
