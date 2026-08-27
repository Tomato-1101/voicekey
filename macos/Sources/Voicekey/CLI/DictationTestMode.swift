//
//  DictationTestMode.swift
//  音声入力（ディクテーション）側の検証ハーネス（CLI モード）の入口
//
//  マイクを使わずに、既知の音声ファイルを流し込んで機械判定する。通常起動（引数なし）には
//  一切影響しない。以下の引数が付いたときだけ GUI を出さずに計測して終了する。
//
//    --local-stt-test <音声ファイル>  : ローカル（Apple）音声認識の E2E
//        --expect "語1,語2"          : 認識テキストに含まれるべき語（すべて含めば PASS）
//        --with-caption              : ライブ字幕のパイプラインも同時に走らせ、
//                                      SpeechAnalyzer を 2 本同時に動かせることを確かめる
//        --repeat <回数>             : 同じプロセスで N 回続けて認識する（連続録音の再現）。
//                                      アセット確認が 1 回目だけで済むことの回帰に使う
//    --translate-test <原文>         : 「翻訳して入力」の 1 往復（エンジン・出力言語は設定に従う）
//        --to <言語コード>           : 出力言語を上書き（既定は設定値）
//    --rest-stt-test <音声ファイル>  : REST バックエンド（Groq / Gemini 等）の 1 往復
//        --backend <識別子>          : gemini / groq / elevenlabs / openai / deepgram（既定 gemini）
//        --expect "語1,語2"          : 認識テキストに含まれるべき語
//        **課金 API を叩く**ので手動実行のみ（CI では回さない）
//
//  `open` 経由で起動すると標準出力が捨てられるため、`--log-file <path>` を併用する。
//

import AVFoundation
import AppKit
import Foundation

/// 音声入力側ハーネスの引数解釈と実行
enum DictationTestMode {

    /// 引数を見てハーネスを実行する
    ///
    /// - Returns: ハーネスとして実行した（＝通常のアプリ起動をしない）なら true
    static func runIfRequested() -> Bool {
        let arguments = CommandLine.arguments
        let modes = ["--local-stt-test", "--translate-test", "--rest-stt-test"]
        guard let mode = modes.first(where: { arguments.contains($0) }) else { return false }

        guard #available(macOS 26.0, *) else {
            print("[ERROR] このハーネスは macOS 26 以降でのみ実行できます")
            exit(2)
        }

        let writer = CaptionTestLogWriter(path: optionValue("--log-file", in: arguments))
        switch mode {
        case "--local-stt-test":
            let path = optionValue("--local-stt-test", in: arguments) ?? ""
            let expect = (optionValue("--expect", in: arguments) ?? "")
                .split(separator: ",").map { String($0).trimmingCharacters(in: .whitespaces) }
                .filter { !$0.isEmpty }
            let withCaption = arguments.contains("--with-caption")
            let repeats = max(1, Int(optionValue("--repeat", in: arguments) ?? "1") ?? 1)
            runAsync {
                var code: Int32 = 0
                for round in 1...repeats {
                    writer.write("[ROUND] \(round)/\(repeats)")
                    code = await LocalSttTestRunner.run(
                        audioPath: path, expected: expect, withCaption: withCaption,
                        writer: writer, closeWriter: round == repeats
                    )
                    if code != 0 { break }
                }
                return code
            }
        case "--rest-stt-test":
            let path = optionValue("--rest-stt-test", in: arguments) ?? ""
            let backend = Backend(rawValue: optionValue("--backend", in: arguments) ?? "gemini") ?? .gemini
            let expect = (optionValue("--expect", in: arguments) ?? "")
                .split(separator: ",").map { String($0).trimmingCharacters(in: .whitespaces) }
                .filter { !$0.isEmpty }
            runAsync {
                await RestSttTestRunner.run(
                    audioPath: path, backend: backend, expected: expect, writer: writer
                )
            }
        default:
            let text = optionValue("--translate-test", in: arguments) ?? ""
            let target = optionValue("--to", in: arguments)
            runAsync {
                await TranslateTestRunner.run(text: text, targetOverride: target, writer: writer)
            }
        }
    }

    /// 指定オプションの直後の値を取り出す（CaptionTestMode と同じ規則）
    private static func optionValue(_ name: String, in arguments: [String]) -> String? {
        guard let index = arguments.firstIndex(of: name), index + 1 < arguments.count else { return nil }
        let value = arguments[index + 1]
        return value.hasPrefix("--") ? nil : value
    }

    /// 非同期のハーネスを実行してプロセスを終える（メインは run loop を回す）
    private static func runAsync(_ body: @escaping @Sendable () async -> Int32) -> Never {
        Task {
            let code = await body()
            exit(code)
        }
        while true {
            RunLoop.main.run(until: Date().addingTimeInterval(1.0))
        }
    }
}

/// ローカル（Apple）音声認識の E2E 実行本体
@available(macOS 26.0, *)
enum LocalSttTestRunner {

    /// 音声ファイルを流し込んで認識結果を判定する
    ///
    /// - Parameters:
    ///   - audioPath: 読み込む音声ファイル（say -o で作った aiff など）
    ///   - expected: 認識テキストに含まれるべき語（小文字比較）
    ///   - withCaption: ライブ字幕も同時に動かして SpeechAnalyzer 2 本同時を確かめるか
    ///   - writer: 行出力先
    ///   - closeWriter: 最後の 1 回だけ true（--repeat で続けて呼ぶ間は閉じない）
    /// - Returns: 終了コード（0=PASS / 1=FAIL）
    static func run(
        audioPath: String, expected: [String], withCaption: Bool,
        writer: CaptionTestLogWriter, closeWriter: Bool = true
    ) async -> Int32 {
        defer { if closeWriter { writer.close() } }
        writer.write("[INFO] local-stt-test 開始 pid=\(getpid()) file=\(audioPath)")

        guard let samples = loadSamples(audioPath) else {
            writer.write("[ERROR] 音声ファイルを 16kHz モノラルへ読み込めませんでした: \(audioPath)")
            writer.write("[DONE] status=error")
            return 1
        }
        let seconds = Double(samples.count) / AudioRecorder.sampleRate
        writer.write(String(format: "[INFO] 音声 %.2fs (%d サンプル)", seconds, samples.count))

        // 同時実行の検証: 字幕のパイプラインを先に立ち上げ、SpeechAnalyzer を 1 本占有させておく。
        // その状態でディクテーション側がもう 1 本開ける＝2 本同時が成立する。
        var caption: CapturePipeline?
        if withCaption {
            let pipeline = CapturePipeline(scopeMode: .all)
            do {
                try await pipeline.start()
                caption = pipeline
                writer.write("[INFO] ライブ字幕の SpeechAnalyzer を並走させます（2 本同時の確認）")
            } catch {
                writer.write("[WARN] 字幕側を開始できませんでした（単独で続行）: \(String(describing: error))")
            }
        }
        defer { if let caption { Task { await caption.stop() } } }

        // 設定と同じ言語で認識する（ConfigStore は @MainActor なので永続キーを直接読む）
        let language = UserDefaults.standard.string(forKey: "language") ?? "ja"
        writer.write("[INFO] 認識ロケール=\(LocalSpeechTranscriber.recognitionLocale(for: language).identifier)")

        let session = LocalSpeechTranscriber(language: language)
        session.onAssetProgress = { writer.write("[ASSET] \($0)") }
        _ = session.start()

        // 実運用と同じく「録音中に少しずつ届く」形で流す（AudioRecorder のチャンクを模す）
        let chunk = Int(AudioRecorder.sampleRate / 10)  // 100ms
        var offset = 0
        while offset < samples.count {
            let end = min(offset + chunk, samples.count)
            session.send(Array(samples[offset..<end]))
            offset = end
        }

        let started = Date()
        let text = await session.finish()
        let elapsedMs = Int(Date().timeIntervalSince(started) * 1000)
        writer.write("[TEXT] \(text)")
        writer.write("[FINALIZE-MS] \(elapsedMs)")

        guard !text.isEmpty else {
            writer.write("[DONE] status=fail reason=empty")
            return 1
        }
        let lowered = text.lowercased()
        let missing = expected.filter { !lowered.contains($0.lowercased()) }
        writer.write("[MATCH] expected=\(expected.count) missing=\(missing.count) \(missing.joined(separator: ","))")
        guard missing.isEmpty else {
            writer.write("[DONE] status=fail reason=missing-words")
            return 1
        }
        writer.write("[DONE] status=ok chars=\(text.count) finalizeMs=\(elapsedMs) withCaption=\(withCaption)")
        return 0
    }

    /// 音声ファイルを 16kHz モノラル Float32 の配列として読む
    ///
    /// `say -o` が作る aiff（22.05kHz など）をそのまま渡せるよう、変換まで面倒を見る。
    static func loadSamples(_ path: String) -> [Float]? {
        guard let file = try? AVAudioFile(forReading: URL(fileURLWithPath: path)) else { return nil }
        guard let target = AVAudioFormat(
            commonFormat: .pcmFormatFloat32, sampleRate: AudioRecorder.sampleRate,
            channels: 1, interleaved: false
        ) else { return nil }
        guard let converter = AVAudioConverter(from: file.processingFormat, to: target) else { return nil }

        let sourceCapacity = AVAudioFrameCount(file.length)
        guard sourceCapacity > 0,
              let source = AVAudioPCMBuffer(pcmFormat: file.processingFormat, frameCapacity: sourceCapacity),
              (try? file.read(into: source)) != nil else { return nil }

        let ratio = target.sampleRate / file.processingFormat.sampleRate
        let capacity = AVAudioFrameCount(Double(source.frameLength) * ratio) + 4096
        guard let output = AVAudioPCMBuffer(pcmFormat: target, frameCapacity: capacity) else { return nil }

        var consumed = false
        var error: NSError?
        _ = converter.convert(to: output, error: &error) { _, status in
            if consumed {
                status.pointee = .endOfStream
                return nil
            }
            consumed = true
            status.pointee = .haveData
            return source
        }
        guard error == nil, output.frameLength > 0, let channel = output.floatChannelData?[0] else { return nil }
        return Array(UnsafeBufferPointer(start: channel, count: Int(output.frameLength)))
    }
}

/// 「翻訳して入力」の 1 往復を確かめる実行本体
@available(macOS 26.0, *)
enum TranslateTestRunner {

    /// 設定中のエンジン・出力言語で 1 文だけ訳す
    ///
    /// - Parameters:
    ///   - text: 原文
    ///   - targetOverride: 出力言語の上書き（nil なら設定値）
    ///   - writer: 行出力先
    /// - Returns: 終了コード（0=PASS / 1=FAIL）
    static func run(text: String, targetOverride: String?, writer: CaptionTestLogWriter) async -> Int32 {
        defer { writer.close() }
        let target = targetOverride ?? DictationTranslation.targetLanguage
        let engine = DictationTranslation.engine
        writer.write("[INFO] translate-test 開始 engine=\(engine.rawValue) to=\(target)")
        writer.write("[SOURCE] \(text)")

        let source = UserDefaults.standard.string(forKey: "language") ?? "ja"
        writer.write("[INFO] 翻訳元=\(source)")
        let translator = DictationTranslator(
            engine: engine, sourceLanguage: source, targetLanguage: target
        )
        let prepared = await translator.prepare()
        writer.write("[PREPARE] \(prepared ? "ok" : "未導入（原文フォールバックになる）")")

        let started = Date()
        let result = await translator.translate(text)
        let elapsedMs = Int(Date().timeIntervalSince(started) * 1000)
        writer.write("[TRANSLATED] \(result.text)")
        writer.write("[MS] \(elapsedMs)")

        guard result.didTranslate else {
            writer.write("[DONE] status=fail reason=\(result.failureReason ?? "unknown")")
            return 1
        }
        // 話す言語と出力言語が同じときは「訳さずそのまま」が正しい挙動なので、
        // 「変わっていない＝失敗」とは判定しない
        guard source != target else {
            writer.write("[DONE] status=ok reason=same-language-noop chars=\(result.text.count)")
            return 0
        }
        guard result.text != text else {
            writer.write("[DONE] status=fail reason=unchanged")
            return 1
        }
        writer.write("[DONE] status=ok chars=\(result.text.count) ms=\(elapsedMs)")
        return 0
    }
}

/// REST バックエンド（Gemini / Groq など）の 1 往復を確かめる実行本体
///
/// マイクを使わず既知の音声ファイルを Transcriber へ渡し、**アプリ本体と同じ経路**で
/// 文字起こしできることと所要時間を測る（curl での確認は実装の証明にならないため）。
/// **課金 API を叩く**ので自動実行はしない。
@available(macOS 26.0, *)
enum RestSttTestRunner {

    /// - Parameters:
    ///   - audioPath: 読み込む音声ファイル
    ///   - backend: 使うバックエンド
    ///   - expected: 認識テキストに含まれるべき語
    ///   - writer: 行出力先
    /// - Returns: 終了コード（0=PASS / 1=FAIL）
    static func run(
        audioPath: String, backend: Backend, expected: [String], writer: CaptionTestLogWriter
    ) async -> Int32 {
        defer { writer.close() }
        writer.write("[INFO] rest-stt-test 開始 backend=\(backend.rawValue) file=\(audioPath)")

        guard let samples = LocalSttTestRunner.loadSamples(audioPath) else {
            writer.write("[ERROR] 音声ファイルを 16kHz モノラルへ読み込めませんでした: \(audioPath)")
            writer.write("[DONE] status=error")
            return 1
        }
        let seconds = Double(samples.count) / AudioRecorder.sampleRate
        writer.write(String(format: "[INFO] 音声 %.2fs (%d サンプル)", seconds, samples.count))

        guard Keychain.apiKey(for: backend) != nil else {
            writer.write("[ERROR] \(backend.providerName) の API キーが見つかりません")
            writer.write("[DONE] status=error")
            return 1
        }

        let language = UserDefaults.standard.string(forKey: "language") ?? "ja"
        let transcriber = Transcriber(
            backend: backend, model: backend.defaultModel, language: language, prompt: ""
        )
        writer.write("[INFO] model=\(backend.defaultModel) language=\(language)")

        let started = Date()
        let text: String
        do {
            text = try await transcriber.transcribe(samples: samples)
        } catch {
            let message = (error as? TranscriptionError)?.message ?? String(describing: error)
            writer.write("[ERROR] \(message)")
            writer.write("[DONE] status=fail reason=request-failed")
            return 1
        }
        let elapsedMs = Int(Date().timeIntervalSince(started) * 1000)
        writer.write("[TEXT] \(text)")
        writer.write("[MS] \(elapsedMs)")

        guard !text.isEmpty else {
            writer.write("[DONE] status=fail reason=empty")
            return 1
        }
        let lowered = text.lowercased()
        let missing = expected.filter { !lowered.contains($0.lowercased()) }
        writer.write("[MATCH] expected=\(expected.count) missing=\(missing.count) \(missing.joined(separator: ","))")
        guard missing.isEmpty else {
            writer.write("[DONE] status=fail reason=missing-words")
            return 1
        }
        writer.write("[DONE] status=ok chars=\(text.count) ms=\(elapsedMs)")
        return 0
    }
}
