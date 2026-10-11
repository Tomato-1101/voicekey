/// 発話の切れ目を見つけて「今すぐ確定して」を出す判定（純ロジック）
///
/// なぜ要るか（実測）: SpeechTranscriber は発話が終わってから確定を出すまで中央値 4.3 秒かかる。
/// 字幕の遅れの主因はここなので、こちらで切れ目（短い無音、またはタップからのフレーム途絶）を
/// 見つけたら `SpeechAnalyzer.finalize(through:)` で確定を前倒しする（2026-10-12 ユーザー指示
/// 「精度が落ちてもいいから速さ重視」）。
///
/// 判定に使うのは既に計算済みの RMS と時刻だけ。HAL（タップ・Aggregate Device）には一切触れない。
/// 通信・音声と切り離して単体テストできるように、時刻は呼び出し側から渡す。
import Foundation

/// 無音・途絶から確定の前倒しを判定する
struct PauseFinalizeTrigger {

    /// 無音とみなす RMS
    ///
    /// 配信音声の話し声は 0.02 以上、話の合間の背景ノイズは 0.001〜0.003 程度（実測の目安）。
    /// 背景に音楽が鳴り続ける場合は無音にならず前倒しされないが、その時は従来どおり
    /// SpeechTranscriber 自身の確定を待つだけ（壊れはしない）。
    static let silenceRMS: Float = 0.004

    /// これだけ無音（または途絶）が続いたら確定させる（秒）
    ///
    /// 0.3 秒は息継ぎより長く、文の切れ目よりは短い。これ以上短いと 1 語ごとに確定して
    /// 認識の精度が目に見えて落ち、長いと速くした効果が薄れる。
    static let pauseSeconds: Double = 0.3

    /// 前回の確定以降に話し声があったか（無ければ確定させる中身が無いので頼まない）
    private var hasSpeechSinceFinalize = false
    /// 話し声のあとに続いた無音の長さ（秒）
    private var silentRun: Double = 0
    /// 最後にフレームが届いた時刻
    private var lastFrameAt: Double?

    /// 音声バッファ 1 つ分を取り込む
    ///
    /// - Parameters:
    ///   - rms: このバッファの RMS
    ///   - duration: このバッファの長さ（秒）
    ///   - now: 届いた時刻（秒。単調増加なら基準は問わない）
    /// - Returns: いま確定を頼むべきなら true
    mutating func observeAudio(rms: Float, duration: Double, now: Double) -> Bool {
        lastFrameAt = now
        if rms >= Self.silenceRMS {
            hasSpeechSinceFinalize = true
            silentRun = 0
            return false
        }
        silentRun += duration
        return fireIfPaused(silentRun)
    }

    /// 定期的に呼んで、フレームの途絶を見る
    ///
    /// タップは対象が黙っている間フレームを 1 つも配らない（実測）。その場合は RMS が
    /// 来ないので、最後のフレームからの経過時間で切れ目を判定する。
    ///
    /// - Parameter now: 現在時刻（`observeAudio` と同じ基準）
    /// - Returns: いま確定を頼むべきなら true
    mutating func observeTick(now: Double) -> Bool {
        guard let lastFrameAt else { return false }
        return fireIfPaused(now - lastFrameAt)
    }

    /// 話し声のあとに十分な切れ目があれば 1 回だけ発火する
    private mutating func fireIfPaused(_ pause: Double) -> Bool {
        guard hasSpeechSinceFinalize, pause >= Self.pauseSeconds else { return false }
        hasSpeechSinceFinalize = false
        return true
    }
}
