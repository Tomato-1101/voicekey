//
//  AudioEngineCostTestMode.swift
//  「押すたびに AVAudioEngine を作り直す」案のコストを実測する検証ハーネス（CLI モード）
//
//  待機中に置きっぱなしのエンジンが腐って HAL 呼び出しが永久に返らなくなる詰まり
//  （2026-09-22 に実測。11 時間で 107GB）を**起こさせない**唯一の確実な手は、
//  待機中にエンジンを持たない＝押されてから作ることだが、それは録音開始に待ち時間を足す。
//  足し算が何ミリ秒なのかを測らずに方針を決めないための計測。
//
//  使い方:
//    dist/voicekey.app/Contents/MacOS/voicekey --audio-engine-cost-test
//  [COLD] が新規インスタンスの開始コスト、[WARM] が現行（温存したエンジン）の開始コスト。
//
//  録音はするがサンプルは捨てるだけで、文字起こしにも履歴にも一切送らない（課金ゼロ）。
//

import AVFoundation
import Foundation

enum AudioEngineCostTestMode {

    /// 1 ラウンドあたりの録音時間（開始コストだけが知りたいので最短で足りる）
    private static let holdSeconds: TimeInterval = 0.3
    private static let rounds = 4

    static func runIfRequested() -> Bool {
        guard CommandLine.arguments.contains("--audio-engine-cost-test") else { return false }
        Task {
            await runCases()
            fflush(stdout)
            exit(0)
        }
        while true { RunLoop.main.run(until: Date().addingTimeInterval(1.0)) }
    }

    private static func runCases() async {
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
            print("[COLD] round=\(i) prewarm=\(prewarmMs)ms start=\(startMs)ms 合計=\(prewarmMs + startMs)ms")
            try? await Task.sleep(nanoseconds: UInt64(holdSeconds * 1_000_000_000))
            await stopRecording(recorder)
        }

        // COLD-NOPREWARM: prewarm を省いて「押されてから作って即開始」を測る
        for i in 1...rounds {
            let recorder = AudioRecorder()
            let t0 = Date()
            await startRecording(recorder)
            print("[COLD-NOPREWARM] round=\(i) start=\(ms(since: t0))ms")
            try? await Task.sleep(nanoseconds: UInt64(holdSeconds * 1_000_000_000))
            await stopRecording(recorder)
        }

        // COLD-PREPARE: 実 IO は起動せず（マイクインジケータを点けず）準備だけした新品
        for i in 1...rounds {
            let recorder = AudioRecorder()
            recorder.prewarm(warmIO: false)
            await drain(recorder)
            let t0 = Date()
            await startRecording(recorder)
            print("[COLD-PREPARE] round=\(i) start=\(ms(since: t0))ms")
            try? await Task.sleep(nanoseconds: UInt64(holdSeconds * 1_000_000_000))
            await stopRecording(recorder)
        }

        // WARM: 現行の実装と同じく 1 個を温存して使い回す
        let warm = AudioRecorder()
        warm.prewarm()
        await drain(warm)
        for i in 1...rounds {
            let t0 = Date()
            await startRecording(warm)
            print("[WARM] round=\(i) start=\(ms(since: t0))ms")
            try? await Task.sleep(nanoseconds: UInt64(holdSeconds * 1_000_000_000))
            await stopRecording(warm)
        }
        print("[VERDICT] status=ok")
    }

    private static func ms(since: Date) -> Int { Int(Date().timeIntervalSince(since) * 1000) }

    /// 制御キューが空になるまで待つ（prewarm の完了待ち）
    private static func drain(_ recorder: AudioRecorder) async {
        await withCheckedContinuation { (c: CheckedContinuation<Void, Never>) in
            recorder.ping { c.resume() }
        }
    }

    private static func startRecording(_ recorder: AudioRecorder) async {
        await withCheckedContinuation { (c: CheckedContinuation<Void, Never>) in
            recorder.start { failure in
                if let failure { print("[FAIL] 録音開始失敗: \(failure)") }
                c.resume()
            }
        }
    }

    private static func stopRecording(_ recorder: AudioRecorder) async {
        await withCheckedContinuation { (c: CheckedContinuation<Void, Never>) in
            recorder.stop { _ in c.resume() }
        }
    }
}
