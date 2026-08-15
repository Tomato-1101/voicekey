//
//  DoubleTapPolicy.swift
//  ダブルタップ（auto_enter）の成立判定
//
//  「たまに検出されない」の原因は、旧実装が 0.4 秒の窓を
//  (1) 1 打目のホールド時間 (2) 離鍵→2 打目の押下間隔 の 2 か所に効かせていたこと。
//  人間の「タップ」は 400ms を普通に超えるので、どちらか一方で簡単に外れていた。
//
//  そこで判定を次の形に変える:
//  - 間隔は **押下→押下（press-to-press）** で見る。離鍵→押下だと 1 打目を長く押すほど
//    不利になり、「速く 2 回叩いた」というユーザーの体感とずれる。
//  - 窓はシステム設定のダブルクリック間隔に従う（ユーザーが伸ばしていれば追従する）。
//  - ホールド上限は「口述の直後に素早く次の録音を始めただけで Enter が自動送信される」
//    事故を防ぐためのガードとして残す（旧実装の意図はここだけに集約する）。
//
//  時刻をすべて引数で受け取る純粋な判定にしてあるので単体テストできる
//  （DoubleTapPolicyTests）。
//

import Foundation

/// 1 打目が「タップ」と言える押しっぱなし時間の上限（秒）。
/// これを超えたら口述とみなしダブルタップの起点にしない。0.75 は、意識して 2 回叩くときの
/// 1 打目（実測でおおむね 0.1〜0.5 秒）を確実に含み、かつ短い口述（1 語でも 1 秒は超える）を
/// 拾わない値として置いている。
let kTapHoldMax: TimeInterval = 0.75
/// ダブルタップ間隔のクランプ下限（秒）。システム設定が既定（0.5）より短くても、
/// これ以上は狭めない（狭いほど「検出されない」に戻るため）。
let kDoubleTapGapMin: TimeInterval = 0.5
/// ダブルタップ間隔のクランプ上限（秒）。これ以上広げると、別々の録音のつもりの
/// 2 回の押下まで auto_enter になる。
let kDoubleTapGapMax: TimeInterval = 1.2

/// ダブルタップ（auto_enter）の成立を副作用なしで判定する。
enum DoubleTapPolicy {

    /// 1 打目の記録（押下・離鍵の両方を持つ。press-to-press 判定に押下時刻が要る）
    struct Tap: Equatable {
        let slotId: Int
        let pressedAt: TimeInterval
        let releasedAt: TimeInterval

        /// 押しっぱなしだった時間（秒）
        var hold: TimeInterval { releasedAt - pressedAt }
    }

    /// 判定結果。不成立は理由まで持たせ、そのままログに出せるようにする
    /// （「たまに検出されない」を次回は推測でなく実測で追うため）。
    enum Decision: Equatable {
        /// 成立（この録音は auto_enter）
        case accepted
        /// 1 打目が長すぎる（＝口述とみなした）
        case holdTooLong
        /// 2 打目が遅すぎる
        case tooSlow
        /// 直前のタップが別スロット
        case otherSlot
        /// 直前のタップが無い（通常の録音開始）
        case noFirstTap

        var isDoubleTap: Bool { self == .accepted }

        /// ログ用の短いラベル
        var logLabel: String {
            switch self {
            case .accepted: return "成立"
            case .holdTooLong: return "不成立(hold超過)"
            case .tooSlow: return "不成立(gap超過)"
            case .otherSlot: return "不成立(別スロット)"
            case .noFirstTap: return "不成立(1打目なし)"
            }
        }
    }

    /// システム設定のダブルクリック間隔を、下限 `kDoubleTapGapMin` / 上限 `kDoubleTapGapMax` に
    /// クランプして使う間隔窓を返す（`NSEvent.doubleClickInterval` を渡す）。
    static func gapWindow(doubleClickInterval: TimeInterval) -> TimeInterval {
        min(max(doubleClickInterval, kDoubleTapGapMin), kDoubleTapGapMax)
    }

    /// 2 打目の押下がダブルタップとして成立するか。
    /// - Parameters:
    ///   - first: 直前に記録した 1 打目（無ければ nil）
    ///   - slotId: 2 打目のスロット
    ///   - pressedAt: 2 打目の押下時刻（`ProcessInfo.systemUptime` 基準）
    ///   - gapWindow: `gapWindow(doubleClickInterval:)` で求めた間隔窓
    static func decide(
        first: Tap?,
        slotId: Int,
        pressedAt: TimeInterval,
        gapWindow: TimeInterval
    ) -> Decision {
        guard let first else { return .noFirstTap }
        guard first.slotId == slotId else { return .otherSlot }
        // ちょうど上限は成立側に倒す（境界で「たまに落ちる」のを避ける）
        guard first.hold <= kTapHoldMax else { return .holdTooLong }
        // press-to-press の許容は「ホールド上限＋間隔窓」。こうすると 1 打目が短いほど
        // 離鍵後の猶予が実質的に広がり、素早く 2 回叩いたケースを取りこぼさない。
        guard pressedAt - first.pressedAt <= kTapHoldMax + gapWindow else { return .tooSlow }
        return .accepted
    }
}
