//
//  FlacEncoder.swift
//  FLAC ロスレス圧縮エンコーダ（アップロード時間の短縮用）
//
//  16kHz モノラル音声を FLAC に圧縮すると WAV 比で約半分のサイズになり、
//  API へのアップロード時間がその分縮む。可逆圧縮（16bit 量子化は既存の
//  WAV エンコードと同一）のため文字起こし精度には一切影響しない。
//  エンコード失敗時は nil を返し、呼び出し側が WAV にフォールバックする。
//

import AVFoundation
import Foundation
import os.log

private let log = Logger(subsystem: "com.voicekey.app", category: "flac")

enum FlacEncoder {

    /// Float32 サンプル列を FLAC（16bit）に符号化したバイト列を返す。
    /// AVAudioFile（CoreAudio 内蔵エンコーダ、外部依存なし）はファイル出力のみ
    /// 対応のため、一時ファイル経由で符号化して読み戻す（数 ms オーダー）
    static func encode(_ samples: [Float], sampleRate: Int = 16000) -> Data? {
        guard !samples.isEmpty else { return nil }
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent("voicekey-\(UUID().uuidString).flac")
        defer { try? FileManager.default.removeItem(at: url) }

        // 16bit Int に量子化してから渡す（WAV エンコードと同一の量子化）。
        // Float32 のまま渡すと CoreAudio が 32bit FLAC を生成し、WAV より大きくなる
        guard let format = AVAudioFormat(
            commonFormat: .pcmFormatInt16,
            sampleRate: Double(sampleRate),
            channels: 1,
            interleaved: true
        ), let buffer = AVAudioPCMBuffer(
            pcmFormat: format,
            frameCapacity: AVAudioFrameCount(samples.count)
        ), let channel = buffer.int16ChannelData?[0] else { return nil }
        for (i, s) in samples.enumerated() {
            // 範囲外サンプルのラップアラウンド（轟音ノイズ化）を防ぐためクリップ
            let clipped = max(-1.0, min(1.0, s))
            channel[i] = Int16(clipped * 32767)
        }
        buffer.frameLength = AVAudioFrameCount(samples.count)

        let settings: [String: Any] = [
            AVFormatIDKey: kAudioFormatFLAC,
            AVSampleRateKey: Double(sampleRate),
            AVNumberOfChannelsKey: 1,
            AVEncoderBitDepthHintKey: 16,
        ]
        do {
            // AVAudioFile はスコープを抜けた時点（deinit）でフラッシュ・クローズされる
            let file = try AVAudioFile(
                forWriting: url, settings: settings,
                commonFormat: .pcmFormatInt16, interleaved: true
            )
            try file.write(from: buffer)
        } catch {
            log.warning("FLAC エンコードに失敗（WAV にフォールバック）: \(error.localizedDescription)")
            return nil
        }
        return try? Data(contentsOf: url)
    }
}
