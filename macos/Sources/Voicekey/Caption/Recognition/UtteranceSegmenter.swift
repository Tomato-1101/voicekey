//
//  UtteranceSegmenter.swift
//  連続音声を「ひとまとまりの発話」に切る判定（純ロジック）
//
//  ライブ字幕と Meet ボットの録音で、録音後に送る方式の文字起こし（OpenAI / ElevenLabs /
//  Microsoft MAI / Groq）や、発話ごとに繋ぐライブ方式（OpenAI ライブ）を使うための区切り。
//  **無音は送らない**（声のある区間だけを API へ渡す＝無音に課金させない）。
//
//  時計・タイマー・通信を持たせない（区切りの回帰をユニットテストで固定するため）。
//

import Foundation

/// 連続音声を発話ごとに切る
struct UtteranceSegmenter {

    /// 区切りの出来事
    enum Event: Equatable {
        /// 発話が始まった
        case begin
        /// 発話の音声（始まりの直前の少しの音を含む）
        case audio([Float])
        /// 発話が終わった。`isSpeech` が false なら声が短すぎるので文字起こしに回さない
        case end(isSpeech: Bool)
    }

    /// 音声のサンプルレート（CapturePipeline の外部向け形式と同じ）
    static let sampleRate: Double = 16000
    /// 声とみなす RMS（字幕の切れ目判定・Soniox の無音判定と同じ基準）
    static let voiceRMS: Float = PauseFinalizeTrigger.silenceRMS
    /// 無音がこれだけ続いたら発話の終わりとする（秒）
    ///
    /// 短いと文の途中の息継ぎで切れて認識が悪くなり、長いと字幕が出るのが遅れる。
    static let silenceToEnd: TimeInterval = 0.6
    /// 1 回に送る長さの上限（秒）。切れ目なく話し続けても、これで一度区切って字幕を出す
    static let maxLength: TimeInterval = 15
    /// 声がこれより短い区間は送らない（秒）。物音やクリックの 1 発を文字起こしに回さないため
    static let minVoiced: TimeInterval = 0.25
    /// 発話の始まりの前に付けておく長さ（秒）。語頭の弱い子音が欠けないようにする
    static let preRoll: TimeInterval = 0.2

    /// 発話が始まる前の音（直近 `preRoll` 秒ぶん）
    private var preRollBuffer: [Float] = []
    /// 発話の途中か
    private(set) var isOpen = false
    /// いまの発話の長さ・声のあった長さ・末尾の無音の長さ（サンプル数）
    private var length = 0
    private var voiced = 0
    private var trailingSilence = 0
    /// 最後にフレームが届いた時刻（フレームが途絶えたことを `tick` で見るため）
    private var lastFrameAt: CFAbsoluteTime = 0

    /// フレームを足す
    ///
    /// - Parameters:
    ///   - frame: 16kHz モノラル Float32
    ///   - now: いまの時刻
    /// - Returns: 起きた出来事（順番どおり）
    mutating func append(_ frame: [Float], now: CFAbsoluteTime) -> [Event] {
        guard !frame.isEmpty else { return [] }
        let isVoiced = SonioxIdleTracker.rms(frame) >= Self.voiceRMS
        lastFrameAt = now
        guard isOpen else {
            guard isVoiced else {
                preRollBuffer.append(contentsOf: frame)
                let limit = Int(Self.preRoll * Self.sampleRate)
                if preRollBuffer.count > limit { preRollBuffer.removeFirst(preRollBuffer.count - limit) }
                return []
            }
            isOpen = true
            let head = preRollBuffer + frame
            preRollBuffer = []
            length = head.count
            voiced = frame.count
            trailingSilence = 0
            return [.begin, .audio(head)]
        }
        length += frame.count
        if isVoiced {
            voiced += frame.count
            trailingSilence = 0
        } else {
            trailingSilence += frame.count
        }
        var events: [Event] = [.audio(frame)]
        if Double(trailingSilence) >= Self.silenceToEnd * Self.sampleRate
            || Double(length) >= Self.maxLength * Self.sampleRate {
            events.append(close())
        }
        return events
    }

    /// 時間の経過を伝える
    ///
    /// タップは対象が黙っている間フレームを 1 つも配らない。無音のフレームを待っていると
    /// 発話がいつまでも終わらないので、フレームが途絶えた時間でも終わりを判定する。
    ///
    /// - Returns: 発話が終わったらその出来事
    mutating func tick(now: CFAbsoluteTime) -> Event? {
        guard isOpen, now - lastFrameAt >= Self.silenceToEnd else { return nil }
        return close()
    }

    /// 停止のために、途中の発話を終わらせる
    mutating func flush() -> Event? {
        isOpen ? close() : nil
    }

    private mutating func close() -> Event {
        let isSpeech = Double(voiced) >= Self.minVoiced * Self.sampleRate
        isOpen = false
        length = 0
        voiced = 0
        trailingSilence = 0
        return .end(isSpeech: isSpeech)
    }
}

/// 発話ごとの文字起こしが失敗したとき、いつ諦めて Apple に切り替えるか（純ロジック）
struct UtteranceFailurePolicy {

    /// 連続でこれだけ失敗したら諦める（通信の一時的な失敗 1〜2 回では切り替えない）
    static let maxConsecutive = 3

    /// 直近の連続失敗回数
    private(set) var failures = 0

    /// 成功した（失敗の回数を戻す）
    mutating func noteSuccess() {
        failures = 0
    }

    /// 失敗を 1 回数える
    ///
    /// - Parameter isFatal: キーが無い・無効・レート制限など、続けても直らない失敗か
    /// - Returns: 諦めるべきなら true
    mutating func noteFailure(isFatal: Bool) -> Bool {
        failures += 1
        return isFatal || failures >= Self.maxConsecutive
    }
}
