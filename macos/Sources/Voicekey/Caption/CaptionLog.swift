/// ライブ字幕モジュール共通のロガー定義
///
/// os.log は `.info` / `.debug` レベルだと既定でメモリ上にしか残らず、`log show` で後から
/// 追えない。字幕は「フィールドで黙って音が来なくなる」類の不具合を追う必要があるため、
/// 状態遷移（タップ開始/停止・デバイス再構成・アセットDL）はすべて `.notice` 以上で記録する。
///
/// サブシステムは voicekey 本体と同じにして、カテゴリを `caption.*` で揃える
/// （`log show --predicate 'subsystem == "com.voicekey.app"'` で本体と字幕をまとめて追える）。
import Foundation
import OSLog

/// os.log のサブシステム識別子（voicekey 本体と共通）
let kCaptionSubsystem = "com.voicekey.app"

/// 字幕モジュール用のカテゴリ別ロガーを作るヘルパ
///
/// - Parameter category: ログのカテゴリ名（クラス名などを渡す）
/// - Returns: `caption.<category>` のロガー
func makeCaptionLogger(_ category: String) -> Logger {
    Logger(subsystem: kCaptionSubsystem, category: "caption.\(category)")
}
