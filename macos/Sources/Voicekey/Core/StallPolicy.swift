//
//  StallPolicy.swift
//  録音・文字起こしが無応答になったと見なす基準と、打ち切った世代の台帳（純ロジック）
//
//  2026-09-02 の障害（AVAudioEngine の HAL 呼び出しが coreaudiod 過負荷で無期限ブロックし、
//  ディクテーションが「変換中」のまま永久に固まった）を受けて追加した。
//  ブロックそのものはアプリ側では防げないので、**無限に待たずに数秒でユーザーへ正直に伝えて
//  待機へ戻す**ための判断だけをここに置く。時間の値と世代の突き合わせは副作用を持たない
//  純ロジックなので、実オーディオ・実タイマーなしでテストできる。
//
//  エンジンの作り直しは**ループでは絶対に指示しない**（HAL をループで叩くと coreaudiod ごと
//  巻き込んで Mac 全体のオーディオを殺す）。ただし詰まりからの復帰としては、上限付き
//  （10 分に 3 回）で 1 回だけ作り直すことを許す。ping が返るのを待つだけの旧方式では
//  14 日分のログで一度も復帰しておらず、ユーザーが「アプリ再起動＝エンジン作り直し」を
//  人手でやらされていたため（2026-09-19）。
//

import Foundation

/// 無応答と見なすまでの時間を決める
enum StallPolicy {

    /// 録音開始要求を出してから開始完了が返るまでの上限（秒）。
    /// 正常時は実測で数十 ms なので 5 秒あれば誤発火しない。
    static let recordStartTimeout: TimeInterval = 5

    /// 文字起こしが返るまでの上限（秒）。長い録音の分割並列送信やリトライを含めても
    /// 正当に 60 秒を超えることはない。
    static let transcribeTimeout: TimeInterval = 60

    /// ローカル（Apple）認識の上限（秒）。オンデバイスでネットワークを待たないぶん短くしてよい。
    /// 実障害では「開始」だけ記録されて「確定」が永久に来なかったため、ここを短めにして
    /// ユーザーを待たせない。
    static let localTranscribeTimeout: TimeInterval = 20

    /// バックエンドに応じた文字起こしの上限を返す。
    /// - Parameter backend: 録音開始時に確定したバックエンド（不明なら nil）
    /// - Returns: この秒数を過ぎても結果が来なければ打ち切る
    static func transcribeTimeout(for backend: Backend?) -> TimeInterval {
        backend == .appleLocal ? localTranscribeTimeout : transcribeTimeout
    }

    // MARK: - 詰まった制御キューからの復帰

    /// 詰まりを検知したあと、ping（キューが動いた証拠）を待つ上限（秒）。
    /// これを過ぎても返事が無ければ、キューは詰まったまま＝ping を積んでも一生走らないので、
    /// 録音器そのものを作り直す。
    static let audioQueueRecoveryTimeout: TimeInterval = 3

    /// 録音器の作り直しを許す回数（下の窓あたり）。
    static let maxRecorderRebuilds = 3

    /// 作り直し回数を数える時間窓（秒）。
    static let recorderRebuildWindow: TimeInterval = 600

    /// いま録音器を作り直してよいか（純ロジック・テスト対象）。
    ///
    /// HAL を叩き続ける暴走を防ぐため、窓内の作り直し回数だけで判断する。
    /// 上限に達したら作り直さず、ping の復帰待ちに留める（＝ループしない）。
    /// - Parameters:
    ///   - rebuildTimes: これまでに作り直した時刻（`systemUptime` 基準）
    ///   - now: 現在時刻（同じく `systemUptime` 基準）
    /// - Returns: 窓内の回数が上限未満なら true
    static func shouldRebuildRecorder(rebuildTimes: [TimeInterval], now: TimeInterval) -> Bool {
        let recent = rebuildTimes.filter { now - $0 < recorderRebuildWindow }
        return recent.count < maxRecorderRebuilds
    }
}

/// マイクのタップが黙り込んだ（構成変更のあとバッファが届かなくなった）かの判定（純ロジック）
///
/// マイク入力のタップは無音でも一定間隔（実測 ~43ms）でバッファを配る。つまり
/// 「録音中なのに一定時間 1 つも届かない」は確実な停止判定になる。
/// `engine.isRunning` は true を返し続けるのに音が来ない実事故（2026-09-15 01:57 / 09-16 20:41、
/// 19.8 秒押して 6.9 秒しか録れていない）を拾うために使う。
enum TapStallPolicy {

    /// 構成変更の通知を受けてから、バッファの有無を確かめるまでの猶予（秒）。
    /// 正常なら ~43ms で届くので 1 秒あれば誤発火しない。
    static let bufferSilenceGrace: TimeInterval = 1.0

    /// 構成変更のあとタップが死んだと見なすか。
    /// - Parameters:
    ///   - lastBufferUptime: 最後にバッファを受け取った時刻（`systemUptime` 基準。未受信は 0）
    ///   - notifiedAt: 構成変更の通知を受けた時刻（同上）
    /// - Returns: 通知以降に 1 つもバッファが届いていなければ true（＝再構成が要る）
    static func needsRestart(lastBufferUptime: TimeInterval, notifiedAt: TimeInterval) -> Bool {
        lastBufferUptime < notifiedAt
    }
}

/// ウォッチドッグで打ち切った録音セッションの台帳（世代ガードの実体）
///
/// 打ち切ったあとにブロックが解けて遅れて結果が届くことがある。そのとき
/// **古い世代の結果で UI を触らない・貼り付けない**ために、世代番号を突き合わせて捨てる。
/// 「クリアの時点をずらす」方式では競合が残るので必ず世代で判定する（このリポジトリの定石）。
struct AbandonedSessions: Equatable {

    /// 台帳に残す上限。遅れて一生届かない世代がたまり続けないよう、古いものから落とす
    static let capacity = 16

    /// 打ち切った世代（昇順・重複なし）
    private(set) var generations: [Int] = []

    /// この世代を打ち切ったものとして記録する
    mutating func abandon(_ generation: Int) {
        guard !generations.contains(generation) else { return }
        generations.append(generation)
        generations.sort()
        if generations.count > Self.capacity {
            generations.removeFirst(generations.count - Self.capacity)
        }
    }

    /// この世代は打ち切り済みか（結果を捨てるべきか）
    func contains(_ generation: Int) -> Bool {
        generations.contains(generation)
    }

    /// 遅れて届いた完了通知を受けたときに呼ぶ。
    /// - Returns: true なら打ち切り済み＝後片付けは済んでいるので二重に行わない
    mutating func consume(_ generation: Int) -> Bool {
        guard let index = generations.firstIndex(of: generation) else { return false }
        generations.remove(at: index)
        return true
    }
}
