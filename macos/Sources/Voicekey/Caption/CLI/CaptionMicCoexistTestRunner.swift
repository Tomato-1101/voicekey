/// `--caption-mic-coexist-test` モード（字幕とマイク録音の共存を実測する回帰ハーネス）
///
/// **なぜ要るか**: ライブ字幕を統合した直後、字幕を一度動かすと同一プロセス内の HAL クライアントが
/// 壊れ、以後 voicekey 本来のディクテーションがマイクの既定入力デバイスを解決できなくなる不具合が出た
/// （実測ログ: `HALC_ProxySystem::DestroyIOContext ... Error: 268435460` →
/// `HALDefaultDevice.cpp Could not find default device` → `入力デバイスが見つかりません`）。
/// subglass 時代は別プロセスだったので同じ壊れ方をしてもマイクに影響しなかった＝統合で初めて出た衝突。
///
/// **判定**: 「字幕パイプライン開始 → 数秒 → 停止」を 3 サイクル回し、各サイクルの後に
///   (a) 既定入力デバイスが解決できる
///   (b) AVAudioEngine の入力からフレームが実際に届く（環境ノイズで可・無音でもフレーム数は増える）
///   (c) 入力フォーマットが壊れていない（サンプルレート > 0）
/// を確かめる。1 つでも欠けたらそのサイクルは NG。
import AVFoundation
import Foundation
import os

/// 字幕とマイクの共存テスト
@available(macOS 26.0, *)
enum CaptionMicCoexistTestRunner {

    /// 1 サイクルで字幕を動かす秒数
    private static let captionSeconds: Double = 3.0
    /// マイク確認で録音する秒数
    private static let micProbeSeconds: Double = 1.2
    /// 回すサイクル数
    private static let cycleCount = 3

    /// テストを実行する
    ///
    /// - Parameter logFilePath: ログの追加出力先（nil なら標準出力のみ）
    /// - Returns: 終了コード（0=PASS / 1=FAIL）
    static func run(logFilePath: String?) async -> Int32 {
        let writer = CaptionTestLogWriter(path: logFilePath)
        defer { writer.close() }

        writer.write("[INFO] voicekey caption-mic-coexist-test 開始 pid=\(getpid()) cycles=\(cycleCount)")

        // 1. 字幕を動かす前のマイクを測る（ここが NG ならテスト自体が成立しない）
        let baseline = await probeMicrophone()
        writer.write(baseline.line(phase: "baseline"))
        guard baseline.isOK else {
            writer.write("[VERDICT] status=inconclusive reason=字幕を動かす前からマイクが使えません")
            return 1
        }

        var failedPhases: [String] = []

        for cycle in 1...cycleCount {
            writer.write("[CYCLE] \(cycle) 字幕パイプラインを開始します")
            await runCaptionCycle(writer: writer)
            writer.write("[CYCLE] \(cycle) 字幕パイプラインを停止しました")

            // 停止直後は HAL 側の後始末が非同期に走るので少しだけ待ってから測る
            try? await Task.sleep(for: .seconds(1))

            let result = await probeMicrophone()
            let phase = "cycle\(cycle)"
            writer.write(result.line(phase: phase))
            if !result.isOK { failedPhases.append(phase) }
        }

        let isPass = failedPhases.isEmpty
        writer.write(
            "[VERDICT] status=\(isPass ? "ok" : "fail") cycles=\(cycleCount) "
                + "failedPhases=\(failedPhases.isEmpty ? "なし" : failedPhases.joined(separator: ","))"
        )
        return isPass ? 0 : 1
    }

    /// 字幕パイプラインを 1 サイクル動かす（本番と同じ CapturePipeline を使う）
    ///
    /// 翻訳・HUD は使わない。**HAL を触る部分（タップ生成 → 破棄）だけ**を再現するのが目的。
    private static func runCaptionCycle(writer: CaptionTestLogWriter) async {
        // ハーネスは `say` などを鳴らさないので、対象を絞らない「すべて」モードで回す
        let pipeline = CapturePipeline(scopeMode: .all)
        let frames = OSAllocatedUnfairLock(initialState: 0)
        pipeline.onLevel = { level in
            frames.withLock { $0 += level.frames }
        }
        do {
            try await pipeline.start()
        } catch {
            writer.write("[CAPTION-ERROR] パイプラインを開始できません: \(String(describing: error))")
            return
        }
        try? await Task.sleep(for: .seconds(captionSeconds))
        await pipeline.stop()
        writer.write("[CAPTION] 受け取ったフレーム数=\(frames.withLock { $0 })")
    }

    /// マイクが使えるかを実測する
    ///
    /// AVAudioEngine の入力ノードにタップを張り、実際にフレームが届くかを見る
    /// （デバイス ID が引けるだけでは不十分。壊れているときは inputFormat が 0Hz になる）。
    private static func probeMicrophone() async -> MicProbe {
        var probe = MicProbe()
        probe.defaultDeviceID = AudioDevices.defaultInputDeviceID()

        let engine = AVAudioEngine()
        let input = engine.inputNode
        let format = input.inputFormat(forBus: 0)
        probe.sampleRate = format.sampleRate
        probe.channels = Int(format.channelCount)
        guard format.sampleRate > 0, format.channelCount > 0 else {
            probe.error = "入力フォーマットが不正（デバイス消失）"
            return probe
        }

        let counter = OSAllocatedUnfairLock(initialState: (frames: 0, peak: Float(0)))
        input.installTap(onBus: 0, bufferSize: 1024, format: format) { buffer, _ in
            let count = Int(buffer.frameLength)
            var peak: Float = 0
            if let channel = buffer.floatChannelData?[0] {
                for index in 0..<count { peak = max(peak, abs(channel[index])) }
            }
            counter.withLock { state in
                state.frames += count
                state.peak = max(state.peak, peak)
            }
        }
        do {
            try engine.start()
        } catch {
            input.removeTap(onBus: 0)
            probe.error = "エンジンを開始できません: \(error.localizedDescription)"
            return probe
        }
        try? await Task.sleep(for: .seconds(micProbeSeconds))
        engine.stop()
        input.removeTap(onBus: 0)

        let state = counter.withLock { $0 }
        probe.frames = state.frames
        probe.peak = state.peak
        return probe
    }

    /// マイク実測の結果
    private struct MicProbe {
        var defaultDeviceID: AudioDeviceID?
        var sampleRate: Double = 0
        var channels: Int = 0
        var frames: Int = 0
        var peak: Float = 0
        var error: String?

        /// 判定（デバイスが引けて・フォーマットが生きていて・フレームが届いている）
        var isOK: Bool {
            error == nil && defaultDeviceID != nil && sampleRate > 0 && frames > 0
        }

        /// ログ 1 行
        func line(phase: String) -> String {
            String(
                format: "[MIC] phase=%@ status=%@ device=%@ rate=%.0f ch=%d frames=%d peak=%.4f%@",
                phase, isOK ? "ok" : "ng",
                defaultDeviceID.map(String.init) ?? "なし",
                sampleRate, channels, frames, peak,
                error.map { " error=\($0)" } ?? ""
            )
        }
    }
}
