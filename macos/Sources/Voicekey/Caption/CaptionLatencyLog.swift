/// 字幕の遅延計測
///
/// 「翻訳が遅れて置いていかれる」を体感でなく数値で扱うための計測点。
/// 改修の前後で同じ手法の数字を比べられるよう、計測はここ 1 か所に集約する。
///
/// 測る 2 つ:
///   - M1 確定→訳文表示: 認識が確定してから訳文が画面に出るまで
///   - M2 発話→訳文表示: その発話の最初の認識断片から、訳文が初めて出るまで（体感の「追従感」）
///
/// os.log は `.notice`（永続）で出す。`.info`/`.debug` は後から `log show` で追えない。
import Foundation
import OSLog

/// 遅延サンプルの記録と要約
@available(macOS 26.0, *)
final class CaptionLatencyLog: @unchecked Sendable {

    private let logger = makeCaptionLogger("Latency")
    private let lock = NSLock()

    /// 解析開始の実時刻（音声タイムラインの 0 に対応）
    private var audioOrigin: CFAbsoluteTime = 0
    /// 直近の確定が音声上で終わった秒位置
    private var lastFinalAudioEnd: Double?
    /// 直近のボラタイルが音声上で終わった秒位置
    private var lastVolatileAudioEnd: Double?
    /// 部分訳が出た時点で、生の音声からどれだけ遅れていたか
    private var audioToPartial: [Double] = []
    /// 認識途中の断片そのものが、生の音声からどれだけ遅れて届いたか（＝改善の下限）
    private var volatileArrivalLag: [Double] = []
    /// 実際に何秒遅れて字幕が出たか（体感に一番近い指標）
    private var speechEndToDisplay: [Double] = []

    /// いま進行中の発話が始まった時刻（最初のボラタイル）
    private var utteranceStartedAt: CFAbsoluteTime?
    /// 直近の確定時刻
    private var lastFinalAt: CFAbsoluteTime?
    /// この発話で既に何か訳文を出したか（M2 は最初の 1 回だけ測る）
    private var didShowTranslationForUtterance = false

    private var finalToDisplay: [Double] = []
    private var utteranceToDisplay: [Double] = []
    /// 部分訳が出るまでの時間（部分訳が無効なら空のまま）
    private var utteranceToPartial: [Double] = []

    /// 認識途中の断片が届いた
    ///
    /// - Parameter audioEndSeconds: その断片が音声上で終わった秒位置
    func noteVolatile(audioEndSeconds: Double? = nil) {
        lock.lock(); defer { lock.unlock() }
        lastVolatileAudioEnd = audioEndSeconds ?? lastVolatileAudioEnd
        if audioOrigin > 0, let audioEnd = audioEndSeconds {
            volatileArrivalLag.append(((CFAbsoluteTimeGetCurrent() - audioOrigin) - audioEnd) * 1000)
        }
        if utteranceStartedAt == nil {
            utteranceStartedAt = CFAbsoluteTimeGetCurrent()
            didShowTranslationForUtterance = false
        }
    }

    /// 音声タイムラインの基準（解析開始の実時刻）を設定する
    func setAudioOrigin(_ origin: CFAbsoluteTime) {
        lock.lock(); defer { lock.unlock() }
        audioOrigin = origin
    }

    /// 認識が確定した
    ///
    /// - Parameter audioEndSeconds: その発話が音声上で終わった秒位置（解析開始基準）
    func noteFinal(audioEndSeconds: Double?) {
        lock.lock(); defer { lock.unlock() }
        let now = CFAbsoluteTimeGetCurrent()
        // ボラタイルが一度も来ないまま確定することがある（短い発話）
        if utteranceStartedAt == nil {
            utteranceStartedAt = now
            didShowTranslationForUtterance = false
        }
        lastFinalAt = now
        lastFinalAudioEnd = audioEndSeconds
    }

    /// 部分訳（暫定の訳）を表示した
    func notePartialShown() {
        lock.lock(); defer { lock.unlock() }
        let now = CFAbsoluteTimeGetCurrent()
        // 生の音声からどれだけ遅れているか（確定を待たないぶん、ここが体感の追従感になる）
        if audioOrigin > 0, let audioEnd = lastVolatileAudioEnd {
            audioToPartial.append(((now - audioOrigin) - audioEnd) * 1000)
        }
        guard let start = utteranceStartedAt, !didShowTranslationForUtterance else { return }
        let elapsed = (now - start) * 1000
        utteranceToPartial.append(elapsed)
        didShowTranslationForUtterance = true
        logger.notice("部分訳表示 発話から \(elapsed, format: .fixed(precision: 0), privacy: .public)ms")
    }

    /// 確定訳を表示した（ここで 1 発話が閉じる）
    func noteTranslationShown() {
        lock.lock(); defer { lock.unlock() }
        let now = CFAbsoluteTimeGetCurrent()
        var finalElapsed: Double?
        if let lastFinalAt {
            let elapsed = (now - lastFinalAt) * 1000
            finalToDisplay.append(elapsed)
            finalElapsed = elapsed
        }
        // 体感に一番近い指標: 音声上で喋り終わってから字幕が出るまでの実遅れ
        var behind: Double?
        if audioOrigin > 0, let audioEnd = lastFinalAudioEnd {
            let elapsed = ((now - audioOrigin) - audioEnd) * 1000
            speechEndToDisplay.append(elapsed)
            behind = elapsed
        }
        if let start = utteranceStartedAt {
            let elapsed = (now - start) * 1000
            utteranceToDisplay.append(elapsed)
            logger.notice(
                "確定訳表示 確定から \(finalElapsed ?? -1, format: .fixed(precision: 0), privacy: .public)ms / 発話から \(elapsed, format: .fixed(precision: 0), privacy: .public)ms / 実遅れ \(behind ?? -1, format: .fixed(precision: 0), privacy: .public)ms"
            )
        }
        utteranceStartedAt = nil
        lastFinalAt = nil
        lastFinalAudioEnd = nil
        didShowTranslationForUtterance = false
    }

    /// 計測を初期化する
    func reset() {
        lock.lock(); defer { lock.unlock() }
        utteranceStartedAt = nil
        lastFinalAt = nil
        didShowTranslationForUtterance = false
        lastFinalAudioEnd = nil
        lastVolatileAudioEnd = nil
        audioToPartial.removeAll()
        volatileArrivalLag.removeAll()
        finalToDisplay.removeAll()
        utteranceToDisplay.removeAll()
        utteranceToPartial.removeAll()
        speechEndToDisplay.removeAll()
    }

    /// 機械判定できる 1 行の要約を返す
    func summaryLine() -> String {
        lock.lock(); defer { lock.unlock() }
        return String(
            format: "speechEndToDisplayMedian=%.0f speechEndToDisplayMax=%.0f audioToPartialMedian=%.0f volatileArrivalLagMedian=%.0f finalToDisplayMedian=%.0f utteranceToDisplayMedian=%.0f utteranceToPartialMedian=%.0f samples=%d partialSamples=%d",
            median(speechEndToDisplay), speechEndToDisplay.max() ?? -1, median(audioToPartial),
            median(volatileArrivalLag),
            median(finalToDisplay), median(utteranceToDisplay), median(utteranceToPartial),
            finalToDisplay.count, utteranceToPartial.count
        )
    }

    /// 中央値（サンプルが無ければ -1）
    private func median(_ values: [Double]) -> Double {
        guard !values.isEmpty else { return -1 }
        let sorted = values.sorted()
        let middle = sorted.count / 2
        if sorted.count % 2 == 1 { return sorted[middle] }
        return (sorted[middle - 1] + sorted[middle]) / 2
    }
}
