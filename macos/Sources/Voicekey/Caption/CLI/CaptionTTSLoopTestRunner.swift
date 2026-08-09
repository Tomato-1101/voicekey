/// `--tts-loop-test` モード（TTS 再キャプチャループの実測検証）
///
/// CLAUDE.md の不変条件「TTS 再キャプチャループ禁止」を、実装の主張ではなく数値で確かめる。
///
/// 5 フェーズを 1 回のキャプチャの中で連続して測る:
///   1. baseline  — 無音。基準線 RMS を取る
///   2. selftone  — 自プロセスの AVAudioPlayer でシステム音を鳴らす（**拾ってはいけない**）
///   3. tts       — 自プロセスの AVSpeechSynthesizer で読み上げる（**拾ってはいけない**）
///   4. settle    — 余韻を流す無音
///   5. control   — 同じ音声・同じ文を外部プロセス `/usr/bin/say` で鳴らす（**拾えなければならない**）
///
/// selftone を挟むのは切り分けのため。tts だけが拾われるなら読み上げが別プロセス
/// （speechsynthesisd）でレンダリングされているということで、対策は半二重ゲート。
/// selftone も拾われるならタップの自プロセス除外そのものが効いていない。
///
/// control フェーズが要なのは、「tts で RMS が上がらなかった」の理由が
/// 除外構成ではなく「タップが死んでいた／音量がゼロだった」でも同じ数字になるため。
/// 同一音声・同一文を非除外プロセスから鳴らして初めて、除外が効いていると言える。
///
/// **主判定は RMS ではなく「合い言葉」**。他アプリ（ブラウザの動画等）が鳴っている環境では
/// RMS の基準線が汚れて判別できなくなるため（実測で基準線が 0.006 と 0.18 の間で揺れた）、
/// 読み上げには珍しい英単語を並べた合い言葉を使い、**認識テキストにその単語が出るか**で判定する。
/// 環境音がたまたま同じ珍語列を含む確率は無視できる。
/// 読み上げが日本語でなく英語なのは、認識器が en-US で、日本語だと合い言葉が照合できないため。
/// 検証対象は「AVSpeechSynthesizer が鳴らした音をタップが拾うか」であって言語ではない。
///
/// あわせて各フェーズで「いま出力を鳴らしている HAL プロセス」を列挙する。
/// control フェーズで外部 `say` の PID がそのまま出力プロセスとして現れるかどうかで、
/// 音声合成がリクエスト元プロセス内でレンダリングされるのか
/// speechsynthesisd 側で鳴るのか（＝自プロセス除外では防げないのか）が分かる。
import AVFoundation
import Foundation
import os

/// TTS 再キャプチャ検証の実行本体
@available(macOS 26.0, *)
enum CaptionTTSLoopTestRunner {

    /// 無音の基準線を測る秒数
    private static let baselineSeconds: Double = 5.0
    /// フェーズ間に挟む静穏時間（余韻とレベルタイマーの端数を逃がす）
    private static let settleSeconds: Double = 4.0
    /// 読み上げ完了を待つ上限
    private static let maximumSpeechSeconds: Double = 30.0
    /// 自プロセス内で普通の音を鳴らす秒数（切り分け用）
    private static let selfToneSeconds: Double = 6.0

    /// 検証に使う読み上げ文（合い言葉）
    ///
    /// 日常の動画・音楽にまず出てこない単語を並べ、認識テキストに現れたら
    /// 「読み上げた音が確かにタップへ入った」と言い切れるようにしている。
    static let phrase = "Purple kangaroo counted eleven violins beneath the pineapple bridge."

    /// 合い言葉の照合に使う単語（小文字で比較する）
    static let keywords = ["purple", "kangaroo", "eleven", "violin", "pineapple", "bridge"]

    /// 検証を実行する
    ///
    /// - Parameter logFilePath: ログの追加出力先（nil なら標準出力のみ）
    /// - Returns: プロセスの終了コード（0=正常終了 / 1=エラー）
    static func run(logFilePath: String?) async -> Int32 {
        let writer = CaptionTestLogWriter(path: logFilePath)
        defer { writer.close() }

        writer.write("[INFO] voicekey caption-tts-loop-test 開始 pid=\(getpid())")
        writer.write("[INFO] bundleID=\(Bundle.main.bundleIdentifier ?? "(なし)")")

        let state = OSAllocatedUnfairLock(initialState: PhaseState())
        let pipeline = CapturePipeline()

        pipeline.onLevel = { level in
            let phase = state.withLock { current -> String in
                current.samples[current.phase, default: []].append(level.rms)
                current.frames[current.phase, default: 0] += level.frames
                return current.phase
            }
            writer.write(
                String(format: "[RMS] phase=%@ rms=%.6f peak=%.6f frames=%d",
                       phase, level.rms, level.peak, level.frames)
            )
        }

        pipeline.onSegment = { segment in
            let phase = state.withLock { current -> String in
                if segment.isFinal {
                    current.texts[current.phase, default: []].append(segment.text)
                }
                // 合い言葉の照合は確定を待たない。途中経過（volatile）にも現れるうえ、
                // 確定は数秒遅れて別フェーズに落ちることがあるため。
                current.heard[current.phase, default: ""] += " " + segment.text.lowercased()
                return current.phase
            }
            writer.write("[\(segment.isFinal ? "FINAL" : "VOLATILE")] phase=\(phase) \(segment.text)")
        }

        do {
            try await pipeline.start()
        } catch {
            writer.write("[ERROR] パイプラインの開始に失敗: \(String(describing: error))")
            writer.write("[VERDICT] status=error")
            return 1
        }
        writer.write("[READY] キャプチャと音声認識を開始しました")

        // 使う音声を先に決める。control フェーズの `say` へ同じ音声名を渡して条件を揃える。
        let voice = preferredEnglishVoice()
        writer.write("[INFO] 読み上げ音声: \(voice?.name ?? "(既定)") / \(voice?.identifier ?? "-")")
        writer.write("[INFO] 合い言葉: \(phrase)")

        // 出力中プロセスの定期サンプリング（フェーズごとに集合を貯める）
        let sampler = Task {
            while !Task.isCancelled {
                let processes = readProcessesRunningOutput()
                state.withLock { current in
                    current.outputProcesses[current.phase, default: []].formUnion(processes)
                }
                try? await Task.sleep(for: .milliseconds(400))
            }
        }

        // --- 1. baseline ---
        await runPhase("baseline", state: state, writer: writer) {
            try? await Task.sleep(for: .seconds(baselineSeconds))
        }

        // --- 2. selftone（自プロセスの通常再生。除外の効き自体を確かめる） ---
        await runPhase("selftone", state: state, writer: writer) {
            await playSystemSoundInProcess(writer: writer)
        }

        await runPhase("gap", state: state, writer: writer) {
            try? await Task.sleep(for: .seconds(settleSeconds))
        }

        // --- 3. tts（自プロセス。本番と同じ SpeechNarrator を使う） ---
        let narrator = SpeechNarrator(voice: voice)
        await runPhase("tts", state: state, writer: writer) {
            narrator.enqueue(phrase)
            await waitWhileSpeaking(narrator: narrator, writer: writer)
        }
        narrator.stop()

        // --- 4. settle ---
        await runPhase("settle", state: state, writer: writer) {
            try? await Task.sleep(for: .seconds(settleSeconds))
        }

        // --- 5. control（外部プロセス） ---
        var controlLaunched = true
        await runPhase("control", state: state, writer: writer) {
            controlLaunched = await speakExternally(voiceName: voice?.name, writer: writer)
            // `say` の終了直後はまだ鳴り終えておらず、認識も数秒遅れるので余分に測る
            try? await Task.sleep(for: .seconds(settleSeconds))
        }

        sampler.cancel()
        await pipeline.stop()

        // --- 集計 ---
        let snapshot = state.withLock { $0 }
        for phase in ["baseline", "selftone", "gap", "tts", "settle", "control"] {
            let samples = snapshot.samples[phase] ?? []
            let maxRMS = samples.max() ?? 0
            let meanRMS = samples.isEmpty ? 0 : samples.reduce(0, +) / Float(samples.count)
            let texts = snapshot.texts[phase] ?? []
            writer.write(
                String(format: "[PHASE-RESULT] phase=%@ maxRMS=%.6f meanRMS=%.6f samples=%d frames=%d finals=%d",
                       phase, maxRMS, meanRMS, samples.count,
                       snapshot.frames[phase] ?? 0, texts.count)
            )
            let processes = (snapshot.outputProcesses[phase] ?? []).sorted { $0.pid < $1.pid }
            writer.write(
                "[PHASE-PROC] phase=\(phase) 出力中=\(processes.isEmpty ? "(なし)" : processes.map(\.description).joined(separator: " "))"
            )
            if !texts.isEmpty {
                writer.write("[PHASE-TEXT] phase=\(phase) \(texts.joined(separator: " | "))")
            }
        }

        // 「そのフェーズで新たに鳴り出したのは誰か」を差分で出す。
        // 背景で鳴り続けている他アプリは baseline にも入るので差分から自動的に消える。
        let ownPID = getpid()
        let baselineProcesses = snapshot.outputProcesses["baseline"] ?? []
        let ttsNew = (snapshot.outputProcesses["tts"] ?? []).subtracting(baselineProcesses)
        let controlNew = (snapshot.outputProcesses["control"] ?? [])
            .subtracting(baselineProcesses).subtracting(snapshot.outputProcesses["tts"] ?? [])
        writer.write("[TTS-NEW-PROC] \(ttsNew.isEmpty ? "(なし)" : ttsNew.map(\.description).joined(separator: " "))")
        writer.write("[CONTROL-NEW-PROC] \(controlNew.isEmpty ? "(なし)" : controlNew.map(\.description).joined(separator: " "))")

        // 読み上げが自プロセス内でレンダリングされていれば、tts フェーズで新たに
        // 鳴り出すのは自プロセスだけになる（＝タップの自プロセス除外がそのまま効く）。
        let foreignDuringTTS = ttsNew.filter { $0.pid != ownPID }
        writer.write(
            "[RENDER] ttsSelfOutput=\(ttsNew.contains { $0.pid == ownPID } ? "yes" : "no")"
            + " ttsForeignOutput=\(foreignDuringTTS.isEmpty ? "none" : foreignDuringTTS.map(\.description).joined(separator: ","))"
        )

        // --- 主判定: 合い言葉が認識テキストに現れたか ---
        // tts の音は認識が数秒遅れて settle フェーズに落ちることがあるため、
        // 「読み上げ窓」は tts + settle（control の say が鳴る前まで）で見る。
        let ttsHeard = (snapshot.heard["tts"] ?? "") + (snapshot.heard["settle"] ?? "")
        let controlHeard = snapshot.heard["control"] ?? ""
        let ttsHits = keywords.filter { ttsHeard.contains($0) }
        let controlHits = keywords.filter { controlHeard.contains($0) }
        writer.write("[KEYWORD] window=tts hits=\(ttsHits.count)/\(keywords.count) \(ttsHits.joined(separator: ","))")
        writer.write("[KEYWORD] window=control hits=\(controlHits.count)/\(keywords.count) \(controlHits.joined(separator: ","))")

        let baseline = snapshot.samples["baseline"]?.max() ?? 0
        let selftone = snapshot.samples["selftone"]?.max() ?? 0
        let tts = snapshot.samples["tts"]?.max() ?? 0
        let control = snapshot.samples["control"]?.max() ?? 0
        writer.write(
            String(format: "[VERDICT] status=ok controlLaunched=%@ ownPID=%d ttsKeywordHits=%d controlKeywordHits=%d baselineRMS=%.6f selftoneRMS=%.6f ttsRMS=%.6f controlRMS=%.6f foreignTTSOutput=%d",
                   controlLaunched ? "yes" : "no", ownPID, ttsHits.count, controlHits.count,
                   baseline, selftone, tts, control, foreignDuringTTS.count)
        )
        return 0
    }

    // MARK: - 内部処理

    /// フェーズを切り替えて処理を実行する
    private static func runPhase(
        _ name: String,
        state: OSAllocatedUnfairLock<PhaseState>,
        writer: CaptionTestLogWriter,
        body: () async -> Void
    ) async {
        state.withLock { $0.phase = name }
        writer.write("[PHASE] \(name)")
        await body()
    }

    /// 読み上げが終わるまで待つ
    private static func waitWhileSpeaking(narrator: SpeechNarrator, writer: CaptionTestLogWriter) async {
        let deadline = Date().addingTimeInterval(maximumSpeechSeconds)
        // 読み上げ開始まで少し猶予を置く（enqueue 直後は isSpeaking が立つ前のことがある）
        try? await Task.sleep(for: .seconds(0.5))
        while narrator.isSpeaking, Date() < deadline {
            try? await Task.sleep(for: .seconds(0.25))
        }
        if Date() >= deadline {
            writer.write("[WARN] 読み上げが \(maximumSpeechSeconds) 秒で終わりませんでした")
        }
        // 読み上げ終了通知の後にも出力バッファが鳴っている可能性があるため測り続ける
        try? await Task.sleep(for: .seconds(2.0))
    }

    /// 自プロセス内で普通のオーディオ再生を行う（切り分け用）
    ///
    /// AVAudioPlayer は間違いなく自プロセス内でレンダリングされるため、
    /// これが拾われたらタップの自プロセス除外そのものが効いていないことになる。
    private static func playSystemSoundInProcess(writer: CaptionTestLogWriter) async {
        let url = URL(fileURLWithPath: "/System/Library/Sounds/Submarine.aiff")
        guard let player = try? AVAudioPlayer(contentsOf: url) else {
            writer.write("[WARN] システム音の読み込みに失敗（selftone フェーズは無効）")
            return
        }
        player.numberOfLoops = -1
        player.volume = 1.0
        guard player.play() else {
            writer.write("[WARN] システム音の再生に失敗（selftone フェーズは無効）")
            return
        }
        try? await Task.sleep(for: .seconds(selfToneSeconds))
        player.stop()
        // 出力バッファが鳴り終わるのを待つ
        try? await Task.sleep(for: .seconds(1.0))
    }

    /// 外部プロセス `/usr/bin/say` で同じ文を鳴らす（対照実験）
    ///
    /// - Returns: 正常に実行できたか
    private static func speakExternally(voiceName: String?, writer: CaptionTestLogWriter) async -> Bool {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/say")
        var arguments: [String] = []
        if let voiceName { arguments += ["-v", voiceName] }
        arguments.append(phrase)
        process.arguments = arguments

        do {
            try process.run()
        } catch {
            writer.write("[WARN] 外部 say の起動に失敗: \(String(describing: error))")
            return false
        }
        // 「HAL がどのプロセスの出力として扱うか」を照合するため PID を残す
        writer.write("[INFO] 外部 say の pid=\(process.processIdentifier)")
        return await withCheckedContinuation { continuation in
            // waitUntilExit はブロックするため専用スレッドへ逃がす
            DispatchQueue.global().async {
                process.waitUntilExit()
                continuation.resume(returning: process.terminationStatus == 0)
            }
        }
    }

    /// 合い言葉を読ませる英語音声を選ぶ
    ///
    /// 認識器が en-US なので、合い言葉の照合には英語音声が要る。
    /// control フェーズの `say -v` にも同じ音声名を渡して条件を揃える。
    private static func preferredEnglishVoice() -> AVSpeechSynthesisVoice? {
        let english = AVSpeechSynthesisVoice.speechVoices().filter { $0.language.hasPrefix("en-US") }
        return english.sorted { lhs, rhs in
            rank(lhs.quality) > rank(rhs.quality)
        }.first ?? AVSpeechSynthesisVoice(language: "en-US")
    }

    /// 音声品質の優先度（SpeechNarrator と同じ順序）
    private static func rank(_ quality: AVSpeechSynthesisVoiceQuality) -> Int {
        switch quality {
        case .premium: return 3
        case .enhanced: return 2
        case .default: return 1
        @unknown default: return 0
        }
    }

    /// フェーズごとの計測値
    private struct PhaseState {
        var phase: String = "startup"
        var samples: [String: [Float]] = [:]
        var frames: [String: Int] = [:]
        var texts: [String: [String]] = [:]
        /// 合い言葉照合用に貯める認識テキスト（途中経過も含む・小文字）
        var heard: [String: String] = [:]
        var outputProcesses: [String: Set<AudioOutputProcess>] = [:]
    }
}
