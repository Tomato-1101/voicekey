/// タップ出力 PCM を音声認識が要求するフォーマットへ変換する
///
/// タップは既定出力デバイス由来（44.1/48kHz・ステレオ・Float32）で来るが、
/// SpeechTranscriber が受け取れるのは 16kHz mono 相当。デバイス再構成でタップ側の
/// フォーマットが変わりうるので、入力フォーマットの変化を検知して変換器を作り直す。
import AVFoundation
import Foundation

/// サンプルレート・チャンネル数変換器
///
/// スレッド安全ではない。CoreAudio の IO キュー **1 本からのみ** 呼ぶこと
/// （AVAudioConverter 自体がスレッド安全でないため）。
@available(macOS 26.0, *)
final class AudioFormatConverter {

    private let logger = makeCaptionLogger("AudioFormatConverter")

    /// 変換先フォーマット（音声認識器が要求するもの）
    let outputFormat: AVAudioFormat

    private var converter: AVAudioConverter?
    private var inputFormat: AVAudioFormat?

    /// - Parameter outputFormat: 変換先フォーマット
    init(outputFormat: AVAudioFormat) {
        self.outputFormat = outputFormat
    }

    /// 1 ブロックを変換する
    ///
    /// - Parameter input: タップから届いた PCM バッファ
    /// - Returns: 変換後バッファ。変換できなかった場合は nil
    func convert(_ input: AVAudioPCMBuffer) -> AVAudioPCMBuffer? {
        guard input.frameLength > 0 else { return nil }

        // 入力フォーマットが変わった（＝デバイス再構成）ら変換器を作り直す
        if inputFormat != input.format || converter == nil {
            guard let newConverter = AVAudioConverter(from: input.format, to: outputFormat) else {
                logger.error("AVAudioConverter を生成できません（入力フォーマット非対応）")
                return nil
            }
            converter = newConverter
            inputFormat = input.format
            logger.notice(
                "変換器を再構成 in=\(input.format.sampleRate, privacy: .public)Hz/\(input.format.channelCount, privacy: .public)ch out=\(self.outputFormat.sampleRate, privacy: .public)Hz/\(self.outputFormat.channelCount, privacy: .public)ch"
            )
        }
        guard let converter else { return nil }

        // ダウンサンプルでもフレーム数が端数で膨らむことがあるため少し余裕を持たせる
        let ratio = outputFormat.sampleRate / input.format.sampleRate
        let capacity = AVAudioFrameCount(Double(input.frameLength) * ratio) + 1024
        guard let output = AVAudioPCMBuffer(pcmFormat: outputFormat, frameCapacity: capacity) else {
            return nil
        }

        // 入力ブロックは 1 回だけデータを渡し、2 回目以降は noDataNow で打ち切る
        var consumed = false
        var conversionError: NSError?
        let status = converter.convert(to: output, error: &conversionError) { _, inputStatus in
            if consumed {
                inputStatus.pointee = .noDataNow
                return nil
            }
            consumed = true
            inputStatus.pointee = .haveData
            return input
        }

        if let conversionError {
            logger.error("PCM 変換に失敗: \(conversionError.localizedDescription, privacy: .public)")
            return nil
        }
        guard status != .error, output.frameLength > 0 else { return nil }
        return output
    }
}

/// PCM バッファの RMS と最大振幅を求める
///
/// デバッグ経路（毎秒のレベル表示）と E2E テストの機械判定に使う。
/// SpeechAnalyzer が要求するフォーマットは環境によって Float32 と Int16 が変わるため
/// 両方に対応する（Int16 を取りこぼすと「認識は動くのにレベルが常に 0」になる）。
/// 値は ±1.0 スケールへ正規化して返すので、どちらでも同じしきい値で判定できる。
///
/// - Parameter buffer: 計測対象（第 1 チャンネルのみ見る）
/// - Returns: (二乗和, 最大絶対値, フレーム数)。RMS は呼び出し側で秒単位に集計する。
@available(macOS 26.0, *)
func measureAudioLevel(_ buffer: AVAudioPCMBuffer) -> (sumOfSquares: Double, peak: Float, frames: Int) {
    let frames = Int(buffer.frameLength)
    guard frames > 0 else { return (0, 0, 0) }
    // インターリーブ時はチャンネル 0 のポインタに全チャンネルが交互に並ぶ
    let stride = buffer.format.isInterleaved ? Int(buffer.format.channelCount) : 1

    var sumOfSquares = 0.0
    var peak: Float = 0

    if let channelData = buffer.floatChannelData {
        let samples = channelData[0]
        for index in 0..<frames {
            let value = samples[index * stride]
            sumOfSquares += Double(value) * Double(value)
            peak = max(peak, abs(value))
        }
    } else if let channelData = buffer.int16ChannelData {
        let samples = channelData[0]
        for index in 0..<frames {
            let value = Float(samples[index * stride]) / 32768.0
            sumOfSquares += Double(value) * Double(value)
            peak = max(peak, abs(value))
        }
    } else {
        return (0, 0, 0)
    }

    return (sumOfSquares, peak, frames)
}
