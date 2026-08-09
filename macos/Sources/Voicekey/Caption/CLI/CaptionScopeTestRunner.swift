/// `--scope-test` モード（キャプチャ対象の絞り込みを実測する）
///
/// 2026-08-09 のユーザー要望「一番手前に表示されているアプリだけの音声を取って翻訳して」に
/// 対する恒久回帰テスト。
///
/// 判定方法は TTS 再キャプチャ検証と同じ「合い言葉」方式にしている。RMS では
/// 「2 つの音が同時に鳴っている環境で、対象側だけを拾えたか」を区別できないため、
/// **珍しい単語を並べた 2 つの文**を別プロセスから同時に鳴らし、
/// 認識テキストにどちらの単語が出るかで判定する。
///
/// - 対象プロセス A の語が出る ＝ 対象の音は拾えている（タップが死んでいない証拠）
/// - 対象外プロセス B の語が出ない ＝ 絞り込みが効いている
import AVFoundation
import Foundation
import os

/// キャプチャ対象の絞り込みテスト
@available(macOS 26.0, *)
enum CaptionScopeTestRunner {

    /// 対象プロセスに読ませる文（4 回繰り返して約 20 秒喋らせる）
    private static let phraseA =
        "Purple kangaroo counted eleven violins beneath the pineapple bridge."
    /// 対象外プロセスに読ませる文
    private static let phraseB =
        "Silver dolphins carried seventeen umbrellas across the cinnamon harbor."

    /// 対象側の合い言葉
    private static let keywordsA = ["purple", "kangaroo", "eleven", "violin", "pineapple", "bridge"]
    /// 対象外側の合い言葉
    private static let keywordsB = ["silver", "dolphin", "seventeen", "umbrella", "cinnamon", "harbor"]

    /// 認識に使う時間（秒）
    private static let captureSeconds: Double = 20

    /// テストを実行する
    ///
    /// - Parameter logFilePath: ログの追加出力先（nil なら標準出力のみ）
    /// - Returns: プロセスの終了コード（0=正常終了 / 1=エラー）
    static func run(logFilePath: String?) async -> Int32 {
        let writer = CaptionTestLogWriter(path: logFilePath)
        defer { writer.close() }

        writer.write("[INFO] voicekey caption-scope-test 開始 pid=\(getpid())")

        // 2 つの外部プロセスで同時に喋らせる。`say` は自プロセス内で音をレンダリングするため、
        // それぞれの PID がそのまま HAL の音声プロセスになる（Phase 4 で実測済み）。
        let processA = makeSayProcess(text: repeated(phraseA), voice: "Samantha")
        let processB = makeSayProcess(text: repeated(phraseB), voice: "Daniel")
        do {
            try processA.run()
            try processB.run()
        } catch {
            writer.write("[ERROR] say の起動に失敗: \(String(describing: error))")
            writer.write("[VERDICT] status=error")
            return 1
        }
        defer {
            if processA.isRunning { processA.terminate() }
            if processB.isRunning { processB.terminate() }
        }

        let pidA = processA.processIdentifier
        let pidB = processB.processIdentifier
        writer.write("[INFO] 対象=pid \(pidA) / 対象外=pid \(pidB)")

        // HAL に音声プロセスが現れるまで少し待つ（再生開始と同時には出てこない）
        try? await Task.sleep(for: .seconds(2))
        let processes = readAudioProcesses()
        let matchedA = processes.filter { $0.pid == pidA }
        let matchedB = processes.filter { $0.pid == pidB }
        writer.write("[AUDIO-PROC] 対象=\(matchedA.map(\.description).joined(separator: ",")) 対象外=\(matchedB.map(\.description).joined(separator: ","))")
        guard !matchedA.isEmpty, !matchedB.isEmpty else {
            // どちらかが鳴っていないと「絞り込めた」のか「そもそも鳴っていない」のか区別できない
            writer.write("[VERDICT] status=inconclusive reason=両方のプロセスが音を出していません")
            return 1
        }

        // 本番と同じ経路（CaptureScopeTracker → SystemAudioTap）で絞り込ませる。
        // 最前面アプリの代わりに PID を固定するのが VOICEKEY_CAPTION_TARGET_PID。
        setenv("VOICEKEY_CAPTION_TARGET_PID", String(pidA), 1)
        let pipeline = CapturePipeline(scopeMode: .frontmost)

        let collected = OSAllocatedUnfairLock(initialState: Collected())
        pipeline.onLevel = { level in
            collected.withLock { $0.maxRMS = max($0.maxRMS, level.rms) }
        }
        pipeline.onSegment = { segment in
            collected.withLock { $0.text += " " + segment.text }
            if segment.isFinal { writer.write("[FINAL] \(segment.text)") }
        }
        pipeline.onTargetChanged = { name in
            writer.write("[TARGET] \(name ?? "(なし)")")
        }

        do {
            try await pipeline.start()
        } catch {
            writer.write("[ERROR] パイプラインの開始に失敗: \(String(describing: error))")
            writer.write("[VERDICT] status=error")
            return 1
        }
        writer.write("[READY] キャプチャと音声認識を開始しました")

        try? await Task.sleep(for: .seconds(captureSeconds))
        await pipeline.stop()

        let snapshot = collected.withLock { $0 }
        let lowered = snapshot.text.lowercased()
        let hitsA = keywordsA.filter { lowered.contains($0) }
        let hitsB = keywordsB.filter { lowered.contains($0) }

        writer.write("[TEXT] \(snapshot.text.trimmingCharacters(in: .whitespacesAndNewlines))")
        writer.write("[KEYWORD] window=target hits=\(hitsA.count)/\(keywordsA.count) \(hitsA.joined(separator: ","))")
        writer.write("[KEYWORD] window=other hits=\(hitsB.count)/\(keywordsB.count) \(hitsB.joined(separator: ","))")
        writer.write(
            String(
                format: "[VERDICT] status=ok targetPID=%d otherPID=%d targetHits=%d otherHits=%d maxRMS=%.6f",
                pidA, pidB, hitsA.count, hitsB.count, snapshot.maxRMS
            )
        )
        return 0
    }

    /// 収集した結果
    private struct Collected {
        var text = ""
        var maxRMS: Float = 0
    }

    /// 20 秒ほど喋り続けるように文を繰り返す
    private static func repeated(_ phrase: String) -> String {
        Array(repeating: phrase, count: 4).joined(separator: " ")
    }

    /// `say` の子プロセスを作る
    ///
    /// - Parameters:
    ///   - text: 読ませる文
    ///   - voice: 声（2 つのプロセスを別の声にして、聞き分けとログの区別をしやすくする）
    private static func makeSayProcess(text: String, voice: String) -> Process {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/say")
        process.arguments = ["-v", voice, "-r", "170", text]
        return process
    }
}
