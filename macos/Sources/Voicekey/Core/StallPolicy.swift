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
//  ここでエンジンの作り直しやデバイス再列挙は**絶対に指示しない**（HAL をループで叩くと
//  coreaudiod ごと巻き込んで Mac 全体のオーディオを殺す。再構成は既存の構成変更ハンドラの担当）。
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
