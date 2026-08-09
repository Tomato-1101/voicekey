/// ライブ字幕の検証ハーネス（CLI モード）の入口
///
/// 通常起動（引数なし）には一切影響しない。以下の引数が付いたときだけ GUI を出さずに
/// 計測を実行し、機械判定できる形式で標準出力（`--log-file` 指定時はファイルにも）へ流して終了する。
///
///   --caption-pipeline-test <秒数>  : キャプチャ＋認識＋翻訳を N 秒回して全文保証まで判定
///   --caption-tts-loop-test         : 読み上げ音声を自分で拾い直していないかを実測
///   --caption-scope-test            : 2 プロセス同時再生で「最前面のアプリだけ」の絞り込みを実測
///   --caption-bench                 : 翻訳エンジンの一括ベンチ（**課金あり・手動のみ**）
///   --caption-groq-models           : Groq のモデル一覧＋候補 3 文の小テスト
///
/// `open` 経由で起動すると標準出力が捨てられるため、`--log-file <path>` を併用する。
import AppKit
import Foundation

/// 検証ハーネスの引数解釈と実行
enum CaptionTestMode {

    /// 引数を見てハーネスを実行する
    ///
    /// - Returns: ハーネスとして実行した（＝通常のアプリ起動をしない）なら true
    static func runIfRequested() -> Bool {
        let arguments = CommandLine.arguments
        let modes = [
            "--caption-pipeline-test", "--caption-tts-loop-test", "--caption-scope-test",
            "--caption-bench", "--caption-groq-models",
        ]
        guard let mode = modes.first(where: { arguments.contains($0) }) else { return false }

        guard #available(macOS 26.0, *) else {
            print("[ERROR] ライブ字幕のハーネスは macOS 26 以降でのみ実行できます")
            exit(2)
        }

        let logFilePath = optionValue("--log-file", in: arguments)
        switch mode {
        case "--caption-pipeline-test":
            let seconds = Double(optionValue("--caption-pipeline-test", in: arguments) ?? "") ?? 20.0
            runAsync { await CaptionPipelineTestRunner.run(seconds: seconds, logFilePath: logFilePath) }
        case "--caption-tts-loop-test":
            runAsync { await CaptionTTSLoopTestRunner.run(logFilePath: logFilePath) }
        case "--caption-scope-test":
            runAsync { await CaptionScopeTestRunner.run(logFilePath: logFilePath) }
        case "--caption-bench":
            runAsync { await CaptionBenchRunner.run(logFilePath: logFilePath) }
        default:
            runAsync { await CaptionBenchRunner.runGroqModelTest(logFilePath: logFilePath) }
        }
    }

    /// 指定オプションの直後の値を取り出す
    ///
    /// - Parameters:
    ///   - name: オプション名（例 `--log-file`）
    ///   - arguments: コマンドライン引数
    /// - Returns: 値。指定が無ければ nil
    private static func optionValue(_ name: String, in arguments: [String]) -> String? {
        guard let index = arguments.firstIndex(of: name), index + 1 < arguments.count else { return nil }
        let value = arguments[index + 1]
        // 次のオプションが来ていたら値ではない
        return value.hasPrefix("--") ? nil : value
    }

    /// 非同期のハーネスを実行してプロセスを終える
    ///
    /// メインスレッドは **セマフォで止めず run loop を回す**。
    /// AVSpeechSynthesizer の読み上げ完了通知はメインキューへ配送されるため、
    /// メインスレッドをブロックすると読み上げの完了を永久に検知できない。
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
