//
//  CaptionSpeechStream.swift
//  連続音声を外部の文字起こしエンジンへ渡す共通の口（ライブ字幕と Meet ボットの録音で共用）
//
//  2026-11-20 ユーザー指示「全ての場所で全てのプロバイダーを自分で選べるように」。
//  それまで字幕の外部認識は Soniox だけだった。Soniox の接続（`SonioxCaptionStream`）と同じ形の
//  契約を切り、どのエンジンでも呼び出し側（CaptionService / MeetBotService）が同じ扱いで差し替える。
//

import AVFoundation
import Foundation

/// 外部の文字起こしへ音声を流すストリームの契約
///
/// 音声は CapturePipeline（`usesOnDeviceRecognizer: false`）の `onAudio` から 16kHz モノラル Float32 で渡す。
/// HAL には一切触れない（タップの作り直しは呼び出し側の役目で、ここからは起こさない）。
@available(macOS 26.0, *)
protocol CaptionSpeechStream: AnyObject, Sendable {
    /// ライブ行の更新（原文, 訳）。任意スレッドから呼ぶ
    var onLive: (@Sendable (String, String) -> Void)? { get set }
    /// 1 行の確定（原文, 訳）。任意スレッドから呼ぶ
    var onConfirmed: (@Sendable (String, String) -> Void)? { get set }
    /// 続けられなくなった（ユーザー向けの理由）。任意スレッドから 1 回だけ呼ぶ。呼び出し側は Apple へ切り替える
    var onGiveUp: (@Sendable (String) -> Void)? { get set }
    /// 接続状態が変わった（メニュー表示用）。常時接続しないエンジンは呼ばない
    var onLinkStateChange: (@Sendable (SonioxCaptionLinkState) -> Void)? { get set }
    /// 開始する。キー未設定などで開始できなければ false（呼び出し側は Apple へ回す）
    func start() -> Bool
    /// 音声を送る（CapturePipeline の IO スレッドから呼ばれる）
    func send(_ buffer: AVAudioPCMBuffer)
    /// 停止する（残っている分を確定させてから戻る。戻った後はコールバックを呼ばない）
    func stop() async
}

@available(macOS 26.0, *)
extension SonioxCaptionStream: CaptionSpeechStream {}

/// 選ばれたエンジンからストリームを作る
@available(macOS 26.0, *)
enum CaptionSpeechStreamFactory {

    /// - Parameters:
    ///   - choice: 文字起こしエンジンとモデル
    ///   - language: 認識言語のヒント（"en" / "ja"）
    ///   - sonioxTranslates: Soniox に英→日の翻訳までさせるか（字幕の翻訳エンジンが Soniox のときだけ true）
    /// - Returns: ストリーム。Apple ローカル（端末内の認識をそのまま使う）と、選べないエンジンは nil
    static func make(choice: EngineModelChoice, language: String, sonioxTranslates: Bool) -> CaptionSpeechStream? {
        let backend = choice.backend
        let model = choice.model
        switch backend {
        case .appleLocal, .deepgram:
            return nil
        case .soniox:
            return SonioxCaptionStream(translates: sonioxTranslates, language: language)
        case .openaiLive:
            // 発話ごとに繋いで、黙っている間は接続を持たない（無音に課金させない）
            return UtteranceCaptionStream(
                providerName: backend.providerName,
                canStart: { !EmbeddedKeys.isDist && !(Keychain.apiKey(for: backend) ?? "").isEmpty },
                makeRecognizer: {
                    LiveUtteranceRecognizer(session: OpenAILiveTranscriber(model: model, language: language))
                }
            )
        case .openai, .groq, .elevenlabs, .azureMAI:
            // 音声入力と同じ送信経路（Transcriber）を使う。発話 1 つを録音 1 回ぶんとして送る
            let transcriber = Transcriber(backend: backend, model: model, language: language, prompt: "")
            return UtteranceCaptionStream(
                providerName: backend.providerName,
                canStart: { !EmbeddedKeys.isDist && !(Keychain.apiKey(for: backend) ?? "").isEmpty },
                makeRecognizer: {
                    RestUtteranceRecognizer(providerName: backend.providerName) { samples in
                        try await transcriber.transcribe(samples: samples)
                    }
                }
            )
        }
    }
}
