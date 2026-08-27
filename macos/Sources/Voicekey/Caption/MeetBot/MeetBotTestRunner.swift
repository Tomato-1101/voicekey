/// Meet 議事録ボットの疎通ハーネス（`--meetbot-test [URL]`）
///
/// 会議に入る前段（**ここが壊れると何も動かない**）を機械判定する:
/// 1. 専用プロファイルの Chrome を起動できるか
/// 2. DevTools に接続して JS を実行できるか
/// 3. 開いたページで Meet のロビー / 会議画面の手掛かりが見えるか
///
/// URL を省略すると `https://meet.google.com/`（ロビー）を開く。実会議の URL を渡すと、
/// **参加まではせず**にログイン状態と参加ボタンの有無だけを報告する（勝手に入室しない）。
import Foundation

/// ボットの疎通確認
///
/// ログ出力に字幕ハーネス共通の `CaptionTestLogWriter`（macOS 26 限定）を使うため、
/// ボット本体（OS 制限なし）と違ってここだけ 26 以降に絞っている。
@available(macOS 26.0, *)
enum MeetBotTestRunner {

    /// ハーネス本体
    ///
    /// - Parameters:
    ///   - urlString: 開く URL（省略時は Meet のトップ）
    ///   - logFilePath: ログの追記先
    /// - Returns: 終了コード（0=PASS / 1=FAIL）
    static func run(urlString: String?, logFilePath: String?) async -> Int32 {
        let writer = CaptionTestLogWriter(path: logFilePath)
        defer { writer.close() }

        let url = urlString ?? "https://meet.google.com/"
        writer.write("[INFO] meetbot-test 開始 url=\(url)")

        guard ChromeDevTools.isChromeInstalled else {
            writer.write("[ERROR] Google Chrome が見つかりません: \(ChromeDevTools.chromeExecutable)")
            writer.write("[VERDICT] status=fail reason=no-chrome")
            return 1
        }

        let port = 9334  // 本番（9333）と別にして、動作中のボットを邪魔しない
        let profile = FileManager.default.temporaryDirectory
            .appendingPathComponent("voicekey-meetbot-test-profile", isDirectory: true)

        var chrome: Process?
        defer {
            if let chrome, chrome.isRunning { chrome.terminate() }
        }

        do {
            chrome = try ChromeDevTools.launchChrome(port: port, profileDirectory: profile, headless: true)
            writer.write("[INFO] Chrome を起動しました pid=\(chrome?.processIdentifier ?? -1)")
            try await ChromeDevTools.waitForDevTools(port: port)
            writer.write("[INFO] DevTools に接続できました port=\(port)")
        } catch {
            writer.write("[ERROR] Chrome の起動に失敗: \(String(describing: error))")
            writer.write("[VERDICT] status=fail reason=launch")
            return 1
        }

        let tools = ChromeDevTools()
        do {
            let socket = try await ChromeDevTools.openTab(url: url, port: port)
            tools.connect(to: socket)
            writer.write("[INFO] タブを開きました")
        } catch {
            writer.write("[ERROR] タブを開けませんでした: \(String(describing: error))")
            writer.write("[VERDICT] status=fail reason=open-tab")
            return 1
        }
        defer { tools.close() }

        // ページが組み上がるまで待つ（Meet は描画が非同期）
        try? await Task.sleep(for: .seconds(6))

        do {
            let title = (try await tools.evaluate("document.title") as? String) ?? ""
            writer.write("[TITLE] \(title)")

            let signedIn = (try await tools.evaluate(signedInScript) as? Bool) ?? false
            writer.write("[SIGNED-IN] \(signedIn)")

            let buttons = (try await tools.evaluate(visibleButtonsScript) as? [Any]) ?? []
            let labels = buttons.compactMap { $0 as? String }.prefix(12)
            writer.write("[BUTTONS] \(labels.joined(separator: " | "))")

            let caption = try await tools.evaluate(MeetBotService.captionScript)
            let found = ((caption as? [String: Any])?["found"] as? Bool) ?? false
            writer.write("[CAPTION-REGION] found=\(found)")

            let inMeeting = (try await tools.evaluate(MeetBotService.inMeetingScript) as? Bool) ?? false
            writer.write("[IN-MEETING] \(inMeeting)")

            guard !title.isEmpty else {
                writer.write("[VERDICT] status=fail reason=empty-title")
                return 1
            }
            writer.write("[VERDICT] status=ok title=\(title) signedIn=\(signedIn) inMeeting=\(inMeeting)")
            return 0
        } catch {
            writer.write("[ERROR] JS の実行に失敗: \(String(describing: error))")
            writer.write("[VERDICT] status=fail reason=evaluate")
            return 1
        }
    }

    /// Google にログイン済みか（アカウント切り替えのリンクや画像の有無で見る）
    private static let signedInScript = """
        (() => {
            const hasAccount = !!document.querySelector('a[href*="SignOutOptions"], a[aria-label*="Google アカウント"], a[aria-label*="Google Account"]');
            const hasSignIn = [...document.querySelectorAll('a,button')]
                .some((el) => /ログイン|Sign in/i.test((el.innerText || '').trim()));
            return hasAccount || !hasSignIn;
        })()
        """

    /// 画面に出ているボタンのラベル一覧（Meet の UI が変わったときの調査用）
    private static let visibleButtonsScript = """
        (() => {
            const vis = (el) => !!el && !!el.offsetParent;
            return [...document.querySelectorAll('button,[role="button"]')]
                .filter(vis)
                .map((b) => ((b.getAttribute('aria-label') || '') + ' ' + (b.innerText || '')).replace(/\\s+/g, ' ').trim())
                .filter((t) => t)
                .slice(0, 20);
        })()
        """
}
