//
//  DoubleTapPolicy.swift
//  ダブルタップ（auto_enter）の成立判定
//
//  設計の要は「1 打目の離鍵で録音を止めない」こと。止めてしまうと 2 打目で録音を
//  作り直すことになり、(1) 1 打目に入った声が捨てられる (2) 録音の開始タイミングが
//  auto_enter のときだけ 2 打目までずれる、という 2 つの問題が出る。
//  そこで短いタップの離鍵では録音を継続したまま「保留（Pending）」に入り、
//  2 打目が来たら**同じ録音を auto_enter に切り替えるだけ**にする。
//  2 打目が来なければ、待っていたぶんの末尾を切り落として普通に確定する。
//
//  旧実装が「たまに検出されない」と言われた原因は、単一の窓 0.4 秒を
//  (1) 1 打目のホールド時間 (2) 離鍵→2 打目の押下間隔 の 2 か所に効かせていたこと。
//  人間の「タップ」は 400ms を普通に超えるので、どちらか一方で簡単に外れていた。
//  ホールド上限は 0.75 秒に緩め、間隔はシステムのダブルクリック間隔に追従させる。
//
//  時刻をすべて引数で受け取る純粋な判定にしてあるので単体テストできる
//  （DoubleTapPolicyTests）。
//

import Foundation

/// 1 打目が「タップ」と言える押しっぱなし時間の上限（秒）。
/// これを超えたら口述とみなし、離鍵でそのまま確定する（保留に入らない）。0.75 は、意識して
/// 2 回叩くときの 1 打目（実測でおおむね 0.1〜0.5 秒）を確実に含み、かつ短い口述
/// （1 語でも 1 秒は超える）を拾わない値として置いている。
let kTapHoldMax: TimeInterval = 0.75
/// ダブルタップ間隔のクランプ下限（秒）。システム設定が既定（0.5）より短くても、
/// これ以上は狭めない（狭いほど「検出されない」に戻るため）。
let kDoubleTapGapMin: TimeInterval = 0.5
/// ダブルタップ間隔のクランプ上限（秒）。この値がそのまま「2 打目が来ない単発タップの
/// 確定がどれだけ遅れるか」の上限にもなるので、無闇に広げない。
let kDoubleTapGapMax: TimeInterval = 1.2

/// ダブルタップ（auto_enter）の成立を副作用なしで判定する。
enum DoubleTapPolicy {

    /// 1 打目を離した直後の状態。**この間も録音は止まっていない。**
    struct Pending: Equatable {
        let slotId: Int
        let pressedAt: TimeInterval
        let releasedAt: TimeInterval

        /// 押しっぱなしだった時間（秒）
        var hold: TimeInterval { releasedAt - pressedAt }
    }

    /// 2 打目の押下に対する判定。不成立は理由まで持たせ、そのままログに出せるようにする
    /// （「たまに検出されない」を次回は推測でなく実測で追うため）。
    enum Decision: Equatable {
        /// 成立（録音を作り直さず auto_enter へ切り替える）
        case accepted
        /// 2 打目が遅すぎる
        case tooSlow
        /// 保留中の 1 打目が別スロット
        case otherSlot
        /// 保留中の 1 打目が無い（通常の録音開始）
        case noFirstTap
        /// 窓が切れるまで 2 打目が来なかった（＝単発のタップだった）。
        /// `decide` は返さない。保留のタイマー満了をログに残すためだけに使う
        case noSecondTap

        var isDoubleTap: Bool { self == .accepted }

        /// ログ用の短いラベル
        var logLabel: String {
            switch self {
            case .accepted: return "成立"
            case .tooSlow: return "不成立(gap超過)"
            case .otherSlot: return "不成立(別スロット)"
            case .noFirstTap: return "不成立(1打目なし)"
            case .noSecondTap: return "不成立(2打目なし)"
            }
        }
    }

    /// この離鍵を「ダブルタップの 1 打目かもしれない」として扱うか
    /// （＝録音を止めずに 2 打目を待つか）。長い口述の直後に素早く次を始めただけで
    /// Enter が自動送信される事故を防ぐガードは、この 1 か所だけに集約する。
    static func isTapCandidate(hold: TimeInterval) -> Bool {
        // ちょうど上限は成立側に倒す（境界で「たまに落ちる」のを避ける）
        hold <= kTapHoldMax
    }

    /// システム設定のダブルクリック間隔を、下限 `kDoubleTapGapMin` / 上限 `kDoubleTapGapMax` に
    /// クランプして使う間隔窓を返す（`NSEvent.doubleClickInterval` を渡す）。
    static func gapWindow(doubleClickInterval: TimeInterval) -> TimeInterval {
        min(max(doubleClickInterval, kDoubleTapGapMin), kDoubleTapGapMax)
    }

    /// 2 打目の押下がダブルタップとして成立するか。
    /// - Parameters:
    ///   - pending: 保留中の 1 打目（無ければ nil）
    ///   - slotId: 2 打目のスロット
    ///   - pressedAt: 2 打目の押下時刻（`ProcessInfo.systemUptime` 基準）
    ///   - gapWindow: `gapWindow(doubleClickInterval:)` で求めた間隔窓
    static func decide(
        pending: Pending?,
        slotId: Int,
        pressedAt: TimeInterval,
        gapWindow: TimeInterval
    ) -> Decision {
        guard let pending else { return .noFirstTap }
        guard pending.slotId == slotId else { return .otherSlot }
        // 保留はタイマーでも切れるので通常ここは通るが、タイマーが遅れて発火した場合の
        // 取りこぼし（窓を過ぎた押下を成立させてしまう）を防ぐために念のため見る
        guard pressedAt - pending.releasedAt <= gapWindow else { return .tooSlow }
        return .accepted
    }
}
