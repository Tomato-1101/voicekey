//
//  ScreenshotAttach.swift
//  録音中に撮ったスクリーンショットを、音声入力の文と一緒に貼る（Mac のみ）
//
//  本人の依頼（2026-10-18）:「音声入力中にもう 1 つのキーを押すと、マウスで範囲を囲んでスクリーンショットを撮り、
//  音声入力の文と一緒に画像を AI（の入力欄）へ添付したい」。仕様の正本は docs/SCREENSHOT_ATTACH_PLAN.md。
//
//  ここに置くもの:
//  - 撮影キーの判定・設定画面の衝突警告（純関数）
//  - 文の中の `[画像N]` の位置決め・整形／翻訳で消えたときの置き直し（純関数）
//  - 前面アプリ別の貼り方の並び（純関数。ターミナルは文を印で区切って画像を差し込む）
//  - 1 回の録音で撮った画像の記録（ScreenshotSession）と、範囲選択の撮影（screencapture -i）
//

import AppKit
import CoreGraphics
import Foundation

/// スクリーンショット添付の判定（副作用のない純ロジック・テスト対象）
enum ScreenshotAttach {

    /// 撮影キーの既定（左⌃）。fn は押すだけで絵文字パネル／入力切替が出るので採らない
    static let defaultKey = ["ctrl_l"]

    // MARK: - キー

    /// 押されたキーが撮影キーの一部か
    static func isToken(_ token: String, key: [String]) -> Bool {
        key.contains { KeyToken.acceptableNames(for: $0).contains(token) }
    }

    /// 撮影キーが押されているか（1 回翻訳キーと同じ判定）
    ///
    /// 撮影キーが録音キー（とハンズフリーキー）に含まれていると、録音を始めるたびに撮影になって
    /// しまうので、その場合は押されていても撮影扱いにしない。
    static func isRequested(key: [String], slotHotkey: [String], pressed: Set<String>) -> Bool {
        guard !key.isEmpty else { return false }
        let slotNames = slotHotkey.reduce(into: Set<String>()) { $0.formUnion(KeyToken.acceptableNames(for: $1)) }
        let coveredBySlot = key.allSatisfy { !KeyToken.acceptableNames(for: $0).isDisjoint(with: slotNames) }
        if coveredBySlot { return false }
        return key.allSatisfy { !KeyToken.acceptableNames(for: $0).isDisjoint(with: pressed) }
    }

    /// 撮影キーが他のキー設定と重なっていないか（設定画面の警告文。重なりが無ければ空）
    ///
    /// 黙って効かなくなる組み合わせだけを拾う。再貼り付けキーは録音中には効かないので警告しない。
    static func keyWarnings(
        key: [String], slotHotkeys: [[String]], handsfreeKey: [String], translateOnceKey: [String]
    ) -> [String] {
        guard !key.isEmpty else { return [] }
        var warnings: [String] = []
        for (index, slot) in slotHotkeys.enumerated() where !slot.isEmpty {
            if KeyToken.conflicts(key, slot) || KeyToken.isCovered(key, by: slot) {
                warnings.append("録音キー \(index + 1) と重なっています。この撮影キーは効きません")
            }
        }
        if warnings.isEmpty, !handsfreeKey.isEmpty {
            let covered = KeyToken.conflicts(key, handsfreeKey) || KeyToken.isCovered(key, by: handsfreeKey)
                || slotHotkeys.contains { !$0.isEmpty && KeyToken.isCovered(key, by: $0 + handsfreeKey) }
            if covered {
                warnings.append("ハンズフリーキーと重なっています。ハンズフリーが優先され、この撮影キーは効きません")
            }
        }
        // 1 回翻訳キーは録音中の押下を撮影より先に見るので、重なると翻訳の付け外しになる
        if !translateOnceKey.isEmpty,
           KeyToken.conflicts(key, translateOnceKey) || KeyToken.isCovered(key, by: translateOnceKey)
            || KeyToken.isCovered(translateOnceKey, by: key) {
            warnings.append("1 回翻訳キーと重なっています。1 回翻訳が優先され、この撮影キーは効かないことがあります")
        }
        return warnings
    }

    // MARK: - 貼り先

    /// ターミナルの bundle ID（小文字）。ここに当たる貼り先は、文を印で区切って ⌃V で画像を差し込む
    /// （ターミナルの Claude Code は画像を ⌃V で受ける。⌘V はターミナル自体のテキスト貼り付け）
    static let terminalBundleIDs: Set<String> = [
        "com.apple.terminal",
        "com.googlecode.iterm2",
        "com.mitchellh.ghostty",
        "dev.warp.warp-stable",
        "dev.warp.warp-preview",
        "net.kovidgoyal.kitty",
        "org.alacritty",
        "io.alacritty",
        "com.github.wez.wezterm",
    ]

    /// 貼り先がターミナルか
    static func isTerminal(bundleID: String?) -> Bool {
        guard let bundleID, !bundleID.isEmpty else { return false }
        return terminalBundleIDs.contains(bundleID.lowercased())
    }

    /// 貼り付けの 1 手（画像は 0 始まりの番号）
    enum PasteStep: Equatable {
        case text(String)
        case image(Int)
    }

    /// 前面アプリ別の貼り方の並び
    ///
    /// - GUI（Claude アプリ・ChatGPT・ブラウザ等）: 画像を順に貼ってから、`[画像N]` 入りの文を貼る。
    /// - ターミナル: 文を印で区切り「文1 → 画像1 → 文2 …」と差し込む。Claude Code は画像を貼ると
    ///   自分で `[Image #n]` を入れるので、こちらの印は貼らない。
    static func pastePlan(text: String, imageCount: Int, terminal: Bool) -> [PasteStep] {
        guard terminal else {
            var steps = (0..<imageCount).map { PasteStep.image($0) }
            if !text.isEmpty { steps.append(.text(text)) }
            return steps
        }
        var steps: [PasteStep] = []
        var used = Set<Int>()
        var rest = text
        while let match = firstExactMarker(in: rest) {
            let before = String(rest[rest.startIndex..<match.range.lowerBound])
            if !before.isEmpty { steps.append(.text(before)) }
            let index = match.number - 1
            if (0..<imageCount).contains(index), !used.contains(index) {
                steps.append(.image(index))
                used.insert(index)
            }
            rest = String(rest[match.range.upperBound...])
        }
        if !rest.isEmpty { steps.append(.text(rest)) }
        // 印が見つからなかった画像は最後に足す（画像を落とさない）
        for index in 0..<imageCount where !used.contains(index) { steps.append(.image(index)) }
        return steps
    }

    // MARK: - 印の位置

    /// 文に入れる印
    static func marker(_ number: Int) -> String { "[画像\(number)]" }

    /// 撮影キーを押した時点の記録（印の位置決めに使う）
    struct ShotMark: Equatable {
        /// 録音開始から押下までの秒
        let elapsed: TimeInterval
        /// 押した時のライブ文字（ストリーミングの暫定テキスト全文。REST 経路では空）
        let liveText: String
    }

    /// 寄せ先とみなす文字（この文字の直後に印を置く）
    private static let breakCharacters: Set<Character> = [
        "。", "、", "．", "，", ".", ",", "!", "?", "！", "？", "…", "；", ";", "：", ":",
        ")", "）", "」", "』", "】",
    ]
    /// 句読点・空白へ寄せるときに探す幅（文字数）
    static let snapRadius = 10

    /// 各画像の印を置く位置（確定文の文字の位置。単調非減少）を決める
    ///
    /// 1. 押した時のライブ文字の末尾（8 文字、無ければ 4 文字）を確定文の中で探し、その直後
    ///    （複数見つかったら、文字数の割合で見積もった位置に最も近いもの）
    /// 2. 見つからなければ文字数の割合（押した時のライブ文字数 ÷ 録音終了時のライブ文字数）
    /// 3. ライブ文字が無い（REST 経路）なら経過秒の割合
    /// 4. 近くの句読点・空白の直後へ寄せる
    static func markerOffsets(
        text: String, shots: [ShotMark], finalLiveText: String, recordingSeconds: TimeInterval
    ) -> [Int] {
        let chars = Array(text)
        let count = chars.count
        let finalLiveCount = finalLiveText.count
        var offsets: [Int] = []
        var previous = 0
        for shot in shots {
            let live = Array(shot.liveText)
            // 割合による見積もり（アンカーが複数見つかったときの選択と、見つからないときの位置）
            let ratio: Double
            if !live.isEmpty {
                ratio = finalLiveCount > 0 ? Double(live.count) / Double(finalLiveCount) : 1
            } else {
                ratio = recordingSeconds > 0 ? shot.elapsed / recordingSeconds : 1
            }
            let estimate = offset(forRatio: ratio, count: count)
            var offset = estimate
            if !live.isEmpty, let anchored = anchorOffset(chars: chars, live: live, estimate: estimate) {
                offset = anchored
            }
            offset = max(snap(chars: chars, offset: offset), previous)
            offsets.append(offset)
            previous = offset
        }
        return offsets
    }

    /// 決めた位置に `[画像1]` `[画像2]` … を入れる。英数字に隣接するときだけ空白を挟む
    static func insertMarkers(into text: String, offsets: [Int]) -> String {
        var chars = Array(text)
        // 後ろから入れると前の位置がずれない
        for (index, offset) in offsets.enumerated().reversed() {
            let at = min(max(0, offset), chars.count)
            var piece = marker(index + 1)
            if at > 0, isASCIIAlphanumeric(chars[at - 1]) { piece = " " + piece }
            if at < chars.count, isASCIIAlphanumeric(chars[at]) { piece += " " }
            chars.insert(contentsOf: Array(piece), at: at)
        }
        return String(chars)
    }

    /// 印の相対位置（0〜1。整形・翻訳で印が消えたときの置き直しに使う）
    static func ratios(offsets: [Int], textCount: Int) -> [Double] {
        offsets.map { textCount > 0 ? Double($0) / Double(textCount) : 1 }
    }

    /// 整形・翻訳を通した後の文に `[画像1]`…`[画像N]` が順番どおり 1 回ずつ残っているか確かめ、
    /// 崩れていたら印らしきものを全部消して相対位置で置き直す
    static func ensureMarkers(in text: String, count: Int, ratios: [Double]) -> String {
        guard count > 0 else { return text }
        if exactMarkerNumbers(in: text) == Array(1...count) { return text }
        let stripped = markerLikeRegex.stringByReplacingMatches(
            in: text, range: NSRange(text.startIndex..., in: text), withTemplate: "")
        let chars = Array(stripped)
        var offsets: [Int] = []
        var previous = 0
        for index in 0..<count {
            let ratio = index < ratios.count ? ratios[index] : 1
            let offset = max(snap(chars: chars, offset: offset(forRatio: ratio, count: chars.count)), previous)
            offsets.append(offset)
            previous = offset
        }
        return insertMarkers(into: stripped, offsets: offsets)
    }

    /// 文の加工（記号の自動削除・数字の正規化）から印を守る
    ///
    /// 記号の自動削除は既定で角かっこを消すので、そのまま通すと `[画像1]` が「画像1」になる。
    /// 印（翻訳で崩れた [Image 1] 等も含む）で文を区切り、間の文だけを加工して印はそのまま戻す。
    /// `active` が false（撮影ゼロ枚の録音）なら区切らずにそのまま加工する＝今の経路を変えない。
    static func preservingMarkers(in text: String, active: Bool, _ transform: (String) -> String) -> String {
        guard active else { return transform(text) }
        let matches = markerLikeRegex.matches(in: text, range: NSRange(text.startIndex..., in: text))
        guard !matches.isEmpty else { return transform(text) }
        var result = ""
        var cursor = text.startIndex
        for match in matches {
            guard let range = Range(match.range, in: text) else { continue }
            let segment = String(text[cursor..<range.lowerBound])
            if !segment.isEmpty { result += transform(segment) }
            result += text[range]
            cursor = range.upperBound
        }
        let tail = String(text[cursor...])
        if !tail.isEmpty { result += transform(tail) }
        return result
    }

    /// 貼るものが何も無いか（文が空白だけで、画像も 0 枚）
    ///
    /// 記号の自動削除で文が空になることがある（認識結果が「！」だけ等）。空文字の貼り付けは何もせずに
    /// 成功を返すので、ここで止めないと auto_enter の Enter だけが送られ、入力欄の別の内容を送信しかねない。
    static func isNothingToPaste(text: String, imageCount: Int) -> Bool {
        imageCount == 0 && text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    // MARK: - 内部

    /// こちらが入れた形そのままの印
    private static let exactMarkerRegex = try! NSRegularExpression(pattern: #"\[画像(\d+)\]"#)
    /// 整形・翻訳で崩れた印も含めて拾う（[Image 1]・[画像 #1]・【画像1】 など）
    private static let markerLikeRegex = try! NSRegularExpression(
        pattern: #"[\[［【]\s*(?:画像|image|img|图像|圖像|图片|이미지|imagen|imagem|bild|immagine)\s*#?\s*\d+\s*[\]］】]"#,
        options: [.caseInsensitive])

    private static func exactMarkerNumbers(in text: String) -> [Int] {
        exactMarkerRegex.matches(in: text, range: NSRange(text.startIndex..., in: text)).compactMap { match in
            Range(match.range(at: 1), in: text).flatMap { Int(text[$0]) }
        }
    }

    private static func firstExactMarker(in text: String) -> (range: Range<String.Index>, number: Int)? {
        guard let match = exactMarkerRegex.firstMatch(in: text, range: NSRange(text.startIndex..., in: text)),
              let whole = Range(match.range, in: text),
              let digits = Range(match.range(at: 1), in: text),
              let number = Int(text[digits]) else { return nil }
        return (whole, number)
    }

    private static func offset(forRatio ratio: Double, count: Int) -> Int {
        Int((min(1, max(0, ratio)) * Double(count)).rounded())
    }

    /// ライブ文字の末尾を確定文の中で探し、見つかった出現の直後の位置を返す
    private static func anchorOffset(chars: [Character], live: [Character], estimate: Int) -> Int? {
        var trimmed = live
        while let last = trimmed.last, last.isWhitespace { trimmed.removeLast() }
        for length in [8, 4] {
            let tail = Array(trimmed.suffix(length))
            // 2 文字未満の末尾はどこにでも見つかるので手掛かりにしない
            guard tail.count >= 2, tail.count <= chars.count else { continue }
            var best: Int?
            for start in 0...(chars.count - tail.count) where Array(chars[start..<(start + tail.count)]) == tail {
                let end = start + tail.count
                if let current = best, abs(current - estimate) <= abs(end - estimate) { continue }
                best = end
            }
            if let best { return best }
        }
        return nil
    }

    /// 近くの句読点・空白の直後へ寄せる（同じ距離なら前の方。見つからなければそのまま）
    private static func snap(chars: [Character], offset: Int) -> Int {
        let count = chars.count
        let offset = min(max(0, offset), count)
        func isBoundary(_ i: Int) -> Bool {
            if i == 0 || i == count { return true }
            let before = chars[i - 1]
            return before.isWhitespace || breakCharacters.contains(before)
        }
        if isBoundary(offset) { return offset }
        for distance in 1...snapRadius {
            if offset - distance >= 0, isBoundary(offset - distance) { return offset - distance }
            if offset + distance <= count, isBoundary(offset + distance) { return offset + distance }
        }
        return offset
    }

    private static func isASCIIAlphanumeric(_ c: Character) -> Bool {
        c.isASCII && (c.isLetter || c.isNumber)
    }
}

/// 1 回の録音で撮ったスクリーンショットの記録。撮影キーを初めて押したときに作り、RecordContext が持つ
/// （撮影ゼロ枚の録音では作らない＝今の経路を一切変えない）
@MainActor
final class ScreenshotSession {

    /// 撮った画像（PNG と、PNG を読めないアプリ向けの TIFF）
    struct Capture {
        let png: Data
        let tiff: Data?
    }

    enum State {
        /// 撮影キーを押している（離したら範囲選択を始める）
        case armed
        /// 範囲選択中
        case capturing
        case done(Capture)
        /// 取消（Esc・権限なし・撮影キーと一緒に別のキーを押した）。番号を詰める
        case cancelled
    }

    final class Shot {
        let mark: ScreenshotAttach.ShotMark
        fileprivate(set) var state: State = .armed
        init(mark: ScreenshotAttach.ShotMark) { self.mark = mark }
    }

    private(set) var shots: [Shot] = []
    /// 録音を止めた時点のライブ文字（文字数の割合の分母）
    var finalLiveText = ""
    /// 録音の長さ（経過秒の割合の分母）
    var recordingSeconds: TimeInterval = 0
    /// 文に入れた印の相対位置（整形・翻訳で消えたときの置き直しに使う）
    private var markerRatios: [Double] = []
    private var waiters: [CheckedContinuation<Void, Never>] = []

    /// 押したまま（まだ撮影を始めていない）の 1 枚
    var armedShot: Shot? {
        shots.last { if case .armed = $0.state { return true } else { return false } }
    }

    /// 撮影キーを押したまま、または範囲選択中か（撮影中の再押下は無視する・確定時は終わるまで待つ）
    var isBusy: Bool {
        shots.contains {
            switch $0.state {
            case .armed, .capturing: return true
            case .done, .cancelled: return false
            }
        }
    }

    /// ピルのバッジに出す枚数（取消を除く。押した瞬間から数えて手応えを出す）
    var visibleCount: Int {
        shots.filter { if case .cancelled = $0.state { return false } else { return true } }.count
    }

    /// 撮れた画像（押した順）と、その押下時点の記録
    var completed: [(mark: ScreenshotAttach.ShotMark, capture: Capture)] {
        shots.compactMap { shot in
            if case .done(let capture) = shot.state { return (shot.mark, capture) }
            return nil
        }
    }

    @discardableResult
    func arm(_ mark: ScreenshotAttach.ShotMark) -> Shot {
        let shot = Shot(mark: mark)
        shots.append(shot)
        return shot
    }

    func markCapturing(_ shot: Shot) {
        shot.state = .capturing
    }

    /// 撮影の終わり（nil は取消）
    func finish(_ shot: Shot, capture: Capture?) {
        shot.state = capture.map { .done($0) } ?? .cancelled
        resumeWaitersIfSettled()
    }

    /// 押したままの 1 枚を取り消す（録音が先に終わった・別のキーと一緒に押した）
    func cancelArmed() {
        for shot in shots {
            if case .armed = shot.state { shot.state = .cancelled }
        }
        resumeWaitersIfSettled()
    }

    /// 撮影が全部終わるまで待つ
    func waitUntilSettled() async {
        guard isBusy else { return }
        await withCheckedContinuation { waiters.append($0) }
    }

    /// 文に印を入れる（整形・翻訳の前に呼ぶ）
    func insertMarkers(into text: String) -> String {
        let marks = completed.map(\.mark)
        guard !marks.isEmpty, !text.isEmpty else { return text }
        let offsets = ScreenshotAttach.markerOffsets(
            text: text, shots: marks, finalLiveText: finalLiveText, recordingSeconds: recordingSeconds)
        markerRatios = ScreenshotAttach.ratios(offsets: offsets, textCount: text.count)
        return ScreenshotAttach.insertMarkers(into: text, offsets: offsets)
    }

    /// 整形・翻訳の後の文で印が崩れていたら置き直す
    func ensureMarkers(in text: String) -> String {
        guard !text.isEmpty else { return text }
        return ScreenshotAttach.ensureMarkers(in: text, count: completed.count, ratios: markerRatios)
    }

    /// 前面アプリに合わせた貼り付けの並び
    func pasteItems(text: String, terminal: Bool) -> [Paster.SequenceItem] {
        let captures = completed.map(\.capture)
        return ScreenshotAttach.pastePlan(text: text, imageCount: captures.count, terminal: terminal).map { step in
            switch step {
            case .text(let t):
                return Paster.SequenceItem(content: .text(t), controlV: false)
            case .image(let i):
                // ターミナルは ⌃V（Claude Code が画像を受けるキー）。それ以外は普通の ⌘V
                return Paster.SequenceItem(content: .image(png: captures[i].png, tiff: captures[i].tiff),
                                           controlV: terminal)
            }
        }
    }

    private func resumeWaitersIfSettled() {
        guard !isBusy else { return }
        let pending = waiters
        waiters.removeAll()
        for waiter in pending { waiter.resume() }
    }

    // MARK: - 撮影

    /// 標準の範囲選択でスクリーンショットを撮る（Space で窓選択、Esc で取消）。取消・失敗は nil
    ///
    /// `-i` 範囲選択 / `-x` 撮影音なし / `-o` 窓撮影で影を付けない。撮ったファイルは読んだらすぐ消す。
    nonisolated static func captureInteractively() async -> Capture? {
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent("voicekey-screenshot-\(UUID().uuidString).png")
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/sbin/screencapture")
        process.arguments = ["-i", "-x", "-o", url.path]
        let launched: Bool = await withCheckedContinuation { continuation in
            process.terminationHandler = { _ in continuation.resume(returning: true) }
            do {
                try process.run()
            } catch {
                process.terminationHandler = nil
                continuation.resume(returning: false)
            }
        }
        defer { try? FileManager.default.removeItem(at: url) }
        guard launched, let png = try? Data(contentsOf: url), !png.isEmpty else { return nil }
        return Capture(png: png, tiff: NSImage(data: png)?.tiffRepresentation)
    }
}
