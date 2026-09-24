//
//  AudioEngineCostTestMode.swift
//  録音の「押してから開始まで」の待ち時間と、HAL に作られる集約デバイスの数を実測する検証ハーネス（CLI モード）
//
//  経緯: 当初は「押すたびに AVAudioEngine を作り直す」案のコストを測るために作った。
//  その後 AVAudioEngine が生成のたびに coreaudiod へ集約デバイス（CADefaultDeviceAggregate）を
//  作ることが分かり、待機中の入れ替え（4 分ごと＋入力のたび）が HAL を揺らし続けて
//  dealloc のメインスレッド永久ブロック（2026-09-23 の hang）を招いたため、AudioRecorder を
//  入力専用 AUHAL に置き換えた。このハーネスはその置き換えの回帰判定に使う:
//    - 押下→開始の待ち（[COLD*] / [WARM] / [CYCLE]）
//    - AudioRecorder の生成・開始・停止・破棄で集約デバイスが 1 個も増えないこと（[AGGREGATE]）
//    - 検出方法そのものが効いていること（[CONTROL] で AVAudioEngine を 1 個作って増えるのを見る）
//
//  使い方:
//    open -n dist/voicekey.app --args --audio-engine-cost-test --log-file <path>
//    （`open` 経由だと標準出力が捨てられるので --log-file を併用する。直接起動なら不要）
//  [AGGREGATE] delta=0・[FAIL] なし・0.3 秒の押下で半分以上のサンプルが届いたときだけ `[VERDICT] status=ok`。
//
//  録音はするがサンプルは捨てるだけで、文字起こしにも履歴にも一切送らない（課金ゼロ）。
//

import AVFoundation
import CoreAudio
import Foundation

enum AudioEngineCostTestMode {

    /// 1 ラウンドあたりの録音時間（開始コストだけが知りたいので最短で足りる）
    private static let holdSeconds: TimeInterval = 0.3
    private static let rounds = 4
    /// 同じインスタンスで押す・離すを繰り返す回数（日常の使い方の再現）
    private static let cycles = 20

    /// 標準出力と --log-file の両方へ書く（字幕側の writer は macOS 26 限定なので使わない）
    private static var logHandle: FileHandle?
    private static var failures = 0
    /// 0.3 秒押して届いた 16kHz サンプルの最小数（音が本当に届いているかの確認）
    private static var minHoldSamples = Int.max

    static func runIfRequested() -> Bool {
        let arguments = CommandLine.arguments
        guard arguments.contains("--audio-engine-cost-test") else { return false }
        if let index = arguments.firstIndex(of: "--log-file"), index + 1 < arguments.count {
            let path = arguments[index + 1]
            FileManager.default.createFile(atPath: path, contents: nil)
            logHandle = FileHandle(forWritingAtPath: path)
        }
        Task {
            await runCases()
            fflush(stdout)
            try? logHandle?.close()
            exit(0)
        }
        while true { RunLoop.main.run(until: Date().addingTimeInterval(1.0)) }
    }

    private static func log(_ line: String) {
        print(line)
        logHandle?.write(Data((line + "\n").utf8))
    }

    private static func runCases() async {
        let aggregatesBefore = aggregateDeviceCount()
        log("[AGGREGATE-BEFORE] count=\(aggregatesBefore)")

        // COLD: 毎回まっさらな AudioRecorder を作り、prewarm 込みで開始までを測る
        for i in 1...rounds {
            let recorder = AudioRecorder()
            let t0 = Date()
            recorder.prewarm()
            await drain(recorder)
            let prewarmMs = ms(since: t0)
            let t1 = Date()
            await startRecording(recorder)
            let startMs = ms(since: t1)
            log("[COLD] round=\(i) prewarm=\(prewarmMs)ms start=\(startMs)ms 合計=\(prewarmMs + startMs)ms")
            try? await Task.sleep(nanoseconds: UInt64(holdSeconds * 1_000_000_000))
            await stopRecording(recorder)
        }

        // COLD-NOPREWARM: prewarm を省いて「押されてから作って即開始」を測る
        for i in 1...rounds {
            let recorder = AudioRecorder()
            let t0 = Date()
            await startRecording(recorder)
            log("[COLD-NOPREWARM] round=\(i) start=\(ms(since: t0))ms")
            try? await Task.sleep(nanoseconds: UInt64(holdSeconds * 1_000_000_000))
            await stopRecording(recorder)
        }

        // COLD-PREPARE: 実 IO は起動せず（マイクインジケータを点けず）準備だけした新品
        for i in 1...rounds {
            let recorder = AudioRecorder()
            let tp = Date()
            recorder.prewarm(warmIO: false)
            await drain(recorder)
            let prepareMs = ms(since: tp)
            let t0 = Date()
            await startRecording(recorder)
            log("[COLD-PREPARE] round=\(i) prepare=\(prepareMs)ms start=\(ms(since: t0))ms")
            try? await Task.sleep(nanoseconds: UInt64(holdSeconds * 1_000_000_000))
            await stopRecording(recorder)
        }

        // WARM: 本番と同じく 1 個を温存して使い回す
        let warm = AudioRecorder()
        warm.prewarm()
        await drain(warm)
        // 待機中（prewarm 後・録音前）に既定入力が動いていないこと＝マイクインジケータが点かない
        log("[IDLE] \(idleRunningReport())")
        for i in 1...rounds {
            let t0 = Date()
            await startRecording(warm)
            let startMs = ms(since: t0)
            try? await Task.sleep(nanoseconds: UInt64(holdSeconds * 1_000_000_000))
            let samples = await stopRecording(warm)
            log("[WARM] round=\(i) start=\(startMs)ms samples=\(samples)")
        }

        // CYCLE: 同じインスタンスで短い押下を繰り返す（押すたびに HAL を作り直していないかの確認）
        var samples: [Int] = []
        for _ in 1...cycles {
            let t0 = Date()
            await startRecording(warm)
            samples.append(ms(since: t0))
            try? await Task.sleep(nanoseconds: 100_000_000)
            _ = await stopRecording(warm, countsTowardHold: false)
        }
        let sorted = samples.sorted()
        log("[CYCLE] n=\(cycles) min=\(sorted.first ?? -1)ms median=\(sorted[sorted.count / 2])ms max=\(sorted.last ?? -1)ms")
        log("[IDLE-AFTER] \(idleRunningReport())")

        // ここまでの AudioRecorder は破棄済み（deinit の後始末は制御キューで非同期）なので少し待ってから数える
        try? await Task.sleep(nanoseconds: 1_000_000_000)
        let aggregatesAfter = aggregateDeviceCount()
        let delta = aggregatesAfter - aggregatesBefore
        log("[AGGREGATE] before=\(aggregatesBefore) after=\(aggregatesAfter) delta=\(delta)")

        // CONTROL: 数え方が正しいことの確認。AVAudioEngine の inputNode に触れると集約デバイスが増えるはず
        let controlBefore = aggregateDeviceCount()
        var control: AVAudioEngine? = AVAudioEngine()
        _ = control?.inputNode.inputFormat(forBus: 0)
        let controlAfter = aggregateDeviceCount()
        control = nil
        _ = control
        log("[CONTROL] AVAudioEngine before=\(controlBefore) after=\(controlAfter) delta=\(controlAfter - controlBefore)")

        // 0.3 秒押して半分（0.15 秒ぶん）も届かなければ、開始は速くても音が来ていない
        let expectedSamples = Int(holdSeconds * AudioRecorder.sampleRate)
        log("[SAMPLES] min=\(minHoldSamples) expected≈\(expectedSamples)")
        let ok = delta == 0 && failures == 0 && minHoldSamples >= expectedSamples / 2
        log("[VERDICT] status=\(ok ? "ok" : "FAIL") aggregateDelta=\(delta) failures=\(failures) minSamples=\(minHoldSamples)")
    }

    private static func ms(since: Date) -> Int { Int(Date().timeIntervalSince(since) * 1000) }

    /// このプロセスから見える HAL デバイスのうち、AVAudioEngine が作る集約デバイスの数
    private static func aggregateDeviceCount() -> Int {
        var address = AudioObjectPropertyAddress(
            mSelector: kAudioHardwarePropertyDevices,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        var size: UInt32 = 0
        guard AudioObjectGetPropertyDataSize(
            AudioObjectID(kAudioObjectSystemObject), &address, 0, nil, &size) == noErr
        else { return -1 }
        var ids = [AudioDeviceID](repeating: 0, count: Int(size) / MemoryLayout<AudioDeviceID>.size)
        guard AudioObjectGetPropertyData(
            AudioObjectID(kAudioObjectSystemObject), &address, 0, nil, &size, &ids) == noErr
        else { return -1 }
        let marker = "CADefaultDeviceAggregate"
        return ids.filter { id in
            let uid = AudioDevices.stringProperty(id, kAudioDevicePropertyDeviceUID) ?? ""
            let name = AudioDevices.stringProperty(id, kAudioObjectPropertyName) ?? ""
            return uid.contains(marker) || name.contains(marker)
        }.count
    }

    /// 既定入力デバイスが動いているか（このプロセス内 / どこかのプロセス）
    /// 常用アプリなど他プロセスが録音中だと somewhere は 1 になるので、判定には thisProcess を見る
    private static func idleRunningReport() -> String {
        guard let id = AudioDevices.defaultInputDeviceID() else { return "defaultInput=none" }
        return "device=\(id) thisProcess=\(uint32Property(id, kAudioDevicePropertyDeviceIsRunning)) "
            + "somewhere=\(uint32Property(id, kAudioDevicePropertyDeviceIsRunningSomewhere))"
    }

    private static func uint32Property(_ id: AudioDeviceID, _ selector: AudioObjectPropertySelector) -> Int {
        var address = AudioObjectPropertyAddress(
            mSelector: selector,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        var value: UInt32 = 0
        var size = UInt32(MemoryLayout<UInt32>.size)
        guard AudioObjectGetPropertyData(id, &address, 0, nil, &size, &value) == noErr else { return -1 }
        return Int(value)
    }

    /// 制御キューが空になるまで待つ（prewarm の完了待ち）
    private static func drain(_ recorder: AudioRecorder) async {
        await withCheckedContinuation { (c: CheckedContinuation<Void, Never>) in
            recorder.ping { c.resume() }
        }
    }

    private static func startRecording(_ recorder: AudioRecorder) async {
        await withCheckedContinuation { (c: CheckedContinuation<Void, Never>) in
            recorder.start { failure in
                if let failure {
                    failures += 1
                    log("[FAIL] 録音開始失敗: \(failure)")
                }
                c.resume()
            }
        }
    }

    /// 停止して、届いた 16kHz サンプル数を返す
    @discardableResult
    private static func stopRecording(_ recorder: AudioRecorder, countsTowardHold: Bool = true) async -> Int {
        let count = await withCheckedContinuation { (c: CheckedContinuation<Int, Never>) in
            recorder.stop { samples in c.resume(returning: samples.count) }
        }
        if countsTowardHold { minHoldSamples = min(minHoldSamples, count) }
        return count
    }
}
