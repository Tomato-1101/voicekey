/// 訳文の読み上げ（AVSpeechSynthesizer / 日本語）
///
/// 既定 OFF。ユーザーがメニューで明示的に ON にしたときだけ喋る。
/// ライブ字幕なので、詰まったら古い行を捨てて新しい行を優先する
/// （溜めて全部読むと映像から何十秒も遅れて意味がなくなる）。
import AVFoundation
import Foundation
import OSLog
import os

/// 日本語読み上げのキュー付きラッパ
@available(macOS 26.0, *)
final class SpeechNarrator: NSObject, AVSpeechSynthesizerDelegate, @unchecked Sendable {

    /// 待機中に保持する最大件数（超えたら古い順に捨てる）
    private static let maximumPendingCount = 2

    private let logger = makeCaptionLogger("SpeechNarrator")
    private let synthesizer = AVSpeechSynthesizer()
    private let voice: AVSpeechSynthesisVoice?
    private let state = OSAllocatedUnfairLock(initialState: State())

    private struct State {
        var pending: [String] = []
        var isSpeaking = false
    }

    /// 読み上げ中かどうか（TTS 再キャプチャ計測ハーネスから参照する）
    var isSpeaking: Bool { state.withLock { $0.isSpeaking } }

    /// - Parameter voice: 使用する音声。nil なら日本語の最良音声を自動選択する
    ///   （TTS 再キャプチャ検証ハーネスが、英語の合い言葉を読ませて
    ///   認識テキストへの混入を調べるために差し替える）
    init(voice: AVSpeechSynthesisVoice? = nil) {
        self.voice = voice ?? Self.preferredJapaneseVoice()
        super.init()
        synthesizer.delegate = self
        logger.notice("読み上げ音声: \(self.voice?.identifier ?? "(既定)", privacy: .public)")
    }

    /// 読み上げを 1 件積む
    ///
    /// - Parameter text: 読み上げる日本語テキスト
    func enqueue(_ text: String) {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }

        let shouldStart = state.withLock { current -> Bool in
            current.pending.append(trimmed)
            if current.pending.count > Self.maximumPendingCount {
                current.pending.removeFirst(current.pending.count - Self.maximumPendingCount)
            }
            guard !current.isSpeaking else { return false }
            current.isSpeaking = true
            return true
        }
        if shouldStart { speakNext() }
    }

    /// 読み上げを止めてキューを空にする
    func stop() {
        state.withLock { current in
            current.pending.removeAll()
            current.isSpeaking = false
        }
        synthesizer.stopSpeaking(at: .immediate)
    }

    // MARK: - AVSpeechSynthesizerDelegate

    func speechSynthesizer(_ synthesizer: AVSpeechSynthesizer, didFinish utterance: AVSpeechUtterance) {
        advance()
    }

    func speechSynthesizer(_ synthesizer: AVSpeechSynthesizer, didCancel utterance: AVSpeechUtterance) {
        advance()
    }

    // MARK: - 内部処理

    /// 次の 1 件を読む（無ければ待機に戻る）
    private func advance() {
        let hasNext = state.withLock { current -> Bool in
            if current.pending.isEmpty {
                current.isSpeaking = false
                return false
            }
            return true
        }
        if hasNext { speakNext() }
    }

    /// キューの先頭を読む
    private func speakNext() {
        let next = state.withLock { current -> String? in
            guard !current.pending.isEmpty else {
                current.isSpeaking = false
                return nil
            }
            return current.pending.removeFirst()
        }
        guard let next else { return }

        let utterance = AVSpeechUtterance(string: next)
        utterance.voice = voice
        // 既定より気持ち速く。字幕に追従させるため。
        utterance.rate = AVSpeechUtteranceDefaultSpeechRate * 1.06
        synthesizer.speak(utterance)
    }

    /// 一番品質の高い日本語音声を選ぶ
    ///
    /// premium / enhanced が入っていれば既定の compact より明らかに聞き取りやすい。
    private static func preferredJapaneseVoice() -> AVSpeechSynthesisVoice? {
        let japanese = AVSpeechSynthesisVoice.speechVoices().filter { $0.language.hasPrefix("ja") }
        let ranked = japanese.sorted { lhs, rhs in
            rank(lhs.quality) > rank(rhs.quality)
        }
        return ranked.first ?? AVSpeechSynthesisVoice(language: "ja-JP")
    }

    /// 音声品質の優先度
    private static func rank(_ quality: AVSpeechSynthesisVoiceQuality) -> Int {
        switch quality {
        case .premium: return 3
        case .enhanced: return 2
        case .default: return 1
        @unknown default: return 0
        }
    }
}
