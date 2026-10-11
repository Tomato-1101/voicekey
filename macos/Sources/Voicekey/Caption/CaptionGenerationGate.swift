/// 字幕セッションの世代（純ロジック・スレッド安全）
///
/// 認識・翻訳・Soniox の結果は非同期に遅れて届く。停止や張り直しの後にそれが HUD へ届くと
/// 「止めたのに字幕がまた出る」「準備中に止めたのに起動が復活する」になる（2026-10-12 修正）。
/// 結果を出す側は始めたときの世代を持っておき、表示の直前に `isCurrent` で突き合わせて
/// 古いものを捨てる。時間の猶予（しきい値）で隠すのではなく、経路ごと断つため。
import Foundation
import os

@available(macOS 26.0, *)
final class CaptionGenerationGate: @unchecked Sendable {

    private let value = OSAllocatedUnfairLock(initialState: 0)

    /// 新しいセッションを始める
    ///
    /// - Returns: このセッションの世代（以前の世代はすべて無効になる）
    @discardableResult
    func begin() -> Int {
        value.withLock { current in
            current += 1
            return current
        }
    }

    /// いまのセッションを無効にする（停止・張り直し）
    ///
    /// - Returns: 無効にした後の世代（この後に新しいセッションが始まったかの判定に使う）
    @discardableResult
    func invalidate() -> Int {
        begin()
    }

    /// いまの世代（値は進めない。後から始めた非同期処理の結果を突き合わせるために控える）
    var current: Int {
        value.withLock { $0 }
    }

    /// この世代がまだ有効か
    func isCurrent(_ generation: Int) -> Bool {
        value.withLock { $0 == generation }
    }
}
