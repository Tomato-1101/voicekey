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
import os

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

            let speaker = try await tools.evaluate(MeetBotService.activeSpeakerScript)
            writer.write("[ACTIVE-SPEAKER] \((speaker as? String) ?? "(なし)")")

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

    /// ヘッドレス Chrome の音声を Process Tap で拾えるかを実測する（`--meetbot-audio-test`）
    ///
    /// **ボットの文字起こしはこの経路の上に全部載っている**（会議音声 → タップ → オンデバイス認識）。
    /// ここが壊れると議事録が丸ごと空になるので、機械判定できる形で残す。
    ///
    /// 手順: ヘッドレス Chrome で 440Hz のトーンを鳴らし、その PID に固定したタップの RMS を測る。
    ///
    /// - Parameters:
    ///   - seconds: 計測する秒数
    ///   - logFilePath: ログの追記先
    /// - Returns: 終了コード（0=PASS / 1=FAIL）
    static func runAudioTest(seconds: Double, logFilePath: String?) async -> Int32 {
        let writer = CaptionTestLogWriter(path: logFilePath)
        defer { writer.close() }
        writer.write("[INFO] meetbot-audio-test 開始 seconds=\(seconds)")

        guard ChromeDevTools.isChromeInstalled else {
            writer.write("[VERDICT] status=fail reason=no-chrome")
            return 1
        }

        // 440Hz を鳴らし続けるだけのページ。外部通信もファイルも要らないよう data URL で渡す。
        let page = """
            <html><body><script>
            const ctx = new AudioContext();
            const osc = ctx.createOscillator();
            const gain = ctx.createGain();
            gain.gain.value = 0.3;
            osc.frequency.value = 440;
            osc.connect(gain); gain.connect(ctx.destination);
            osc.start();
            document.title = 'tone-' + ctx.state;
            </script></body></html>
            """
        let encoded = page.addingPercentEncoding(withAllowedCharacters: .alphanumerics) ?? ""
        let dataURL = "data:text/html,\(encoded)"

        let port = 9336
        let profile = FileManager.default.temporaryDirectory
            .appendingPathComponent("voicekey-meetbot-audio-profile", isDirectory: true)
        var chrome: Process?
        defer { if let chrome, chrome.isRunning { chrome.terminate() } }

        do {
            chrome = try ChromeDevTools.launchChrome(port: port, profileDirectory: profile, headless: true)
            try await ChromeDevTools.waitForDevTools(port: port)
            let socket = try await ChromeDevTools.openTab(url: dataURL, port: port)
            let tools = ChromeDevTools()
            tools.connect(to: socket)
            defer { tools.close() }
            try? await Task.sleep(for: .seconds(3))
            let title = (try? await tools.evaluate("document.title") as? String) ?? ""
            writer.write("[AUDIO-CONTEXT] \(title)")
        } catch {
            writer.write("[ERROR] Chrome の準備に失敗: \(String(describing: error))")
            writer.write("[VERDICT] status=fail reason=launch")
            return 1
        }

        guard let pid = chrome?.processIdentifier else {
            writer.write("[VERDICT] status=fail reason=no-pid")
            return 1
        }
        writer.write("[INFO] Chrome pid=\(pid) の音声を拾います")

        let pipeline = CapturePipeline(locale: CaptionSettings.language.locale, fixedPID: pid)
        let peak = OSAllocatedUnfairLock<Float>(initialState: 0)
        let frames = OSAllocatedUnfairLock<Int>(initialState: 0)
        pipeline.onLevel = { level in
            peak.withLock { $0 = max($0, level.rms) }
            frames.withLock { $0 += level.frames }
        }
        do {
            try await pipeline.start()
        } catch {
            writer.write("[ERROR] タップを開始できません: \(String(describing: error))")
            writer.write("[VERDICT] status=fail reason=tap")
            return 1
        }
        try? await Task.sleep(for: .seconds(seconds))
        await pipeline.stop()

        let maxRMS = peak.withLock { $0 }
        let totalFrames = frames.withLock { $0 }
        writer.write("[RESULT] maxRMS=\(maxRMS) frames=\(totalFrames)")
        // 無音は 1e-6 未満。トーンが届いていれば 0.01 は軽く超える。
        guard totalFrames > 0 else {
            writer.write("[VERDICT] status=fail reason=no-frames（システムオーディオ録音の許可を確認）")
            return 1
        }
        guard maxRMS > 0.01 else {
            writer.write("[VERDICT] status=fail reason=silent maxRMS=\(maxRMS)")
            return 1
        }
        writer.write("[VERDICT] status=ok maxRMS=\(maxRMS) frames=\(totalFrames)")
        return 0
    }

    /// 「ブラウザの音声 → ローカル認識 → 文字起こし」を通しで実測する（`--meetbot-stt-test <音声>`）
    ///
    /// 実会議の代わりに**音声ファイルを Chrome に再生させて**、ボットと同じ経路
    /// （PID 固定のタップ + Apple のオンデバイス認識）で文字が出るかを見る。
    /// 会議 URL とログインが無くても、ボットの中核を機械判定できる。
    ///
    /// - Parameters:
    ///   - audioPath: 再生する音声ファイル（wav / m4a など Chrome が鳴らせる形式）
    ///   - expected: 認識テキストに含まれるべき語（1 つでも欠けたら FAIL）
    ///   - logFilePath: ログの追記先
    /// - Returns: 終了コード（0=PASS / 1=FAIL）
    static func runSpeechTest(audioPath: String, expected: [String], logFilePath: String?) async -> Int32 {
        let writer = CaptionTestLogWriter(path: logFilePath)
        defer { writer.close() }
        writer.write("[INFO] meetbot-stt-test 開始 audio=\(audioPath)")

        guard ChromeDevTools.isChromeInstalled else {
            writer.write("[VERDICT] status=fail reason=no-chrome")
            return 1
        }
        guard FileManager.default.fileExists(atPath: audioPath) else {
            writer.write("[VERDICT] status=fail reason=no-audio-file")
            return 1
        }

        // file:// の音声を data URL のページから参照するとセキュリティで弾かれるので、
        // 音声と同じ場所に置いた HTML を file:// で開く。
        let workDirectory = FileManager.default.temporaryDirectory
            .appendingPathComponent("voicekey-meetbot-stt", isDirectory: true)
        try? FileManager.default.createDirectory(at: workDirectory, withIntermediateDirectories: true)
        let source = URL(fileURLWithPath: audioPath)
        let copied = workDirectory.appendingPathComponent("clip." + source.pathExtension)
        try? FileManager.default.removeItem(at: copied)
        do {
            try FileManager.default.copyItem(at: source, to: copied)
        } catch {
            writer.write("[VERDICT] status=fail reason=copy-audio")
            return 1
        }
        let pageURL = workDirectory.appendingPathComponent("play.html")
        let html = """
            <html><body>
            <audio id="a" src="\(copied.lastPathComponent)" autoplay></audio>
            <script>
            const a = document.getElementById('a');
            a.volume = 1.0;
            a.play().then(() => { document.title = 'playing'; }).catch((e) => { document.title = 'blocked:' + e; });
            </script>
            </body></html>
            """
        try? html.write(to: pageURL, atomically: true, encoding: .utf8)

        let port = 9337
        let profile = workDirectory.appendingPathComponent("profile", isDirectory: true)
        var chrome: Process?
        defer { if let chrome, chrome.isRunning { chrome.terminate() } }

        let transcript = OSAllocatedUnfairLock<[String]>(initialState: [])

        do {
            chrome = try ChromeDevTools.launchChrome(port: port, profileDirectory: profile, headless: true)
            try await ChromeDevTools.waitForDevTools(port: port)
            guard let pid = chrome?.processIdentifier else {
                writer.write("[VERDICT] status=fail reason=no-pid")
                return 1
            }

            // 先にタップと認識を用意してから再生する（頭を取りこぼさないため）
            let ready = CapturePipeline(locale: CaptionSettings.language.locale, fixedPID: pid)
            ready.onSegment = { segment in
                guard segment.isFinal else { return }
                transcript.withLock { $0.append(segment.text) }
            }
            try await ready.start()
            defer { Task { await ready.stop() } }
            writer.write("[INFO] タップ開始 pid=\(pid) locale=\(CaptionSettings.language.rawValue)")

            let socket = try await ChromeDevTools.openTab(url: pageURL.absoluteString, port: port)
            let tools = ChromeDevTools()
            tools.connect(to: socket)
            defer { tools.close() }
            try? await Task.sleep(for: .seconds(2))
            let title = (try? await tools.evaluate("document.title") as? String) ?? ""
            writer.write("[PLAYBACK] \(title)")

            // 音声の長さぶん＋確定待ちの余裕
            let duration = (try? await tools.evaluate("document.getElementById('a').duration") as? Double) ?? 20
            writer.write("[DURATION] \(duration)s")
            try? await Task.sleep(for: .seconds(min(duration + 8, 90)))
            await ready.stop()
        } catch {
            writer.write("[ERROR] \(String(describing: error))")
            writer.write("[VERDICT] status=fail reason=setup")
            return 1
        }
        let lines = transcript.withLock { $0 }
        let joined = lines.joined(separator: " ")
        writer.write("[TEXT] \(joined)")
        guard !joined.isEmpty else {
            writer.write("[VERDICT] status=fail reason=no-transcript")
            return 1
        }
        let missing = expected.filter { !joined.contains($0) }
        writer.write("[MATCH] expected=\(expected.count) missing=\(missing.count) \(missing.joined(separator: ","))")
        guard missing.isEmpty else {
            writer.write("[VERDICT] status=fail reason=missing-words")
            return 1
        }
        writer.write("[VERDICT] status=ok chars=\(joined.count) finals=\(lines.count)")
        return 0
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
