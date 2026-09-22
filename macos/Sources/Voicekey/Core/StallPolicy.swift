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
//  HAL を**ループで叩かない**のは不変（叩くと coreaudiod ごと巻き込んで Mac 全体のオーディオが
//  死ぬ）。詰まりからの復帰手段は 2026-09-22 の実測で「プロセスの再起動」に確定した:
//  詰まりの正体は AVFAudio の IOUnit プロパティリスナーが暴走して HAL へ同期問い合わせを
//  撃ち続ける状態で、こちらの inputFormat(forBus:) はその裏で永久に順番待ちになる。
//  AudioRecorder を作り直しても**古い AVAudioEngine は解放できない**（ブロック中の呼び出しが
//  参照を握ったままになる）ため暴走は生き残り、実測で voicekey 37% + coreaudiod 66% の CPU と
//  毎時数 GB のメモリを食い続けた（11 時間で 107GB）。プロセスを落とす以外に止める API が無い。
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
    /// これを過ぎても返事が無ければ、キューは詰まったまま＝ping を積んでも一生走らない。
    static let audioQueueRecoveryTimeout: TimeInterval = 3

    /// 待機中に制御キューの生死を確かめる間隔（秒）。
    /// 暴走はユーザーがホットキーを押す前から始まっている（2026-09-22 の実例では 30 分前）。
    /// 押されるまで気づかないと、その間ずっと CPU 1 コアとメモリを食われるので待機中も見る。
    static let audioQueueHeartbeatInterval: TimeInterval = 60

    /// 待機中のエンジンを新品へ入れ替える間隔。
    /// 実測した詰まり 5 件はすべて「6 分以上ほったらかした後の最初の録音」で起きており、
    /// 録音中や連続使用中には一度も起きていない（2026-09-22 に 13 日分のログで確認）。
    /// 腐る前に健康なうちに捨てれば、詰まりも漏れも起きない（詰まってから捨てると
    /// ブロック中の呼び出しが掴んだままで解放できず、暴走が生き残る）。
    /// 最短の 6 分に対して余裕を取り 4 分。入れ替えは実 IO を起こさないので
    /// マイクインジケータは点かず、押下の待ち時間も増えない（実測 58ms < 温存 90ms）。
    /// なお通常は**入力のたび**に入れ替わる（文字起こしの裏で終わるので待ちゼロ）。
    /// このタイマーは、何時間も触らない日のための保険。
    static let engineRefreshInterval: TimeInterval = 240

    /// 待機中のエンジンを入れ替えるべきか（純ロジック）
    static func shouldRefreshIdleEngine(preparedAt: TimeInterval, now: TimeInterval) -> Bool {
        now - preparedAt >= engineRefreshInterval
    }

    /// オーディオ停止からの自動再起動を許す回数（下の窓あたり）。
    static let maxStallRelaunches = 3

    /// 再起動回数を数える時間窓（秒）。プロセスをまたいで数えるので実時刻で持つ。
    static let stallRelaunchWindow: TimeInterval = 1800

    /// いまオーディオ停止からの自動再起動をしてよいか（純ロジック・テスト対象）。
    ///
    /// 「起動直後にまた詰まって再起動」のループだけは避けたいので、窓内の回数で頭打ちにする。
    /// 上限に達したら再起動せず、ユーザーに伝えて待機へ戻る。
    /// - Parameters:
    ///   - relaunchTimes: これまでに自動再起動した時刻（`Date.timeIntervalSinceReferenceDate` 基準）
    ///   - now: 現在時刻（同上）
    /// - Returns: 窓内の回数が上限未満なら true
    static func shouldRelaunchForStall(relaunchTimes: [TimeInterval], now: TimeInterval) -> Bool {
        prunedRelaunchTimes(relaunchTimes, now: now).count < maxStallRelaunches
    }

    /// 台帳から窓外の記録を落とす（際限なく伸びないように・回数表示も窓内に揃える）。
    /// 時計が巻き戻った場合に備えて未来の記録も捨てる。
    static func prunedRelaunchTimes(_ times: [TimeInterval], now: TimeInterval) -> [TimeInterval] {
        times.filter { now - $0 < stallRelaunchWindow && $0 <= now }
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
