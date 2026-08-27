/// オンデバイス音声認識（SpeechAnalyzer / SpeechTranscriber, macOS 26）
///
/// システム音声（英語）をストリーミング認識し、途中経過（ボラタイル）と確定結果を
/// AsyncStream で後段へ渡す。後段は Phase 3 で Claude API 翻訳になる。
import AVFoundation
import CoreMedia
import Foundation
import OSLog
import Speech

/// 音声認識器のエラー
@available(macOS 26.0, *)
enum SpeechRecognizerError: Error, CustomStringConvertible {
    /// この Mac / OS で SpeechTranscriber が使えない
    case unavailable
    /// 指定ロケールのモデルが提供されていない
    case unsupportedLocale(String)
    /// 解析に適したフォーマットが得られない
    case noCompatibleFormat

    var description: String {
        switch self {
        case .unavailable:
            return "SpeechTranscriber がこの環境で利用できません"
        case let .unsupportedLocale(identifier):
            return "ロケール \(identifier) の音声認識モデルが提供されていません"
        case .noCompatibleFormat:
            return "音声認識が受け付けるフォーマットを取得できませんでした"
        }
    }
}

/// ストリーミング音声認識のラッパ
///
/// 使い方: `start()` で返るフォーマットに音声を変換して `feed(_:)` へ流し、
/// 結果は `segments` を for-await で読む。
@available(macOS 26.0, *)
final class SpeechRecognizer: @unchecked Sendable {

    /// 認識結果 1 件
    struct Segment: Sendable {
        /// 認識テキスト
        let text: String
        /// 確定済みなら true（false は途中経過＝ボラタイル）
        let isFinal: Bool
        /// 音声上の時間範囲
        let range: CMTimeRange
    }

    private let logger = makeCaptionLogger("SpeechRecognizer")
    private let locale: Locale

    /// 後段へ結果を流すストリーム
    let segments: AsyncStream<Segment>
    private let segmentContinuation: AsyncStream<Segment>.Continuation

    /// 言語モデルのダウンロード進捗の通知（音声入力側が HUD に出す。字幕は設定しない）
    ///
    /// 初回だけ数百 MB のダウンロードが走るため、無言で固まって見えないよう外へ出せるようにする。
    var onAssetProgress: ((String) -> Void)?

    /// 進捗を出し始めるまでの猶予（秒）
    ///
    /// 導入済みのときアセット取得は数 ms で終わる。すぐ通知すると毎録音で HUD の
    /// 録音表示を上書きしてしまうので、これを超えて待たされたときだけ知らせる。
    private static let assetProgressGrace: TimeInterval = 0.8

    /// このプロセスで「導入済み」を確認できたロケール（BCP-47）
    ///
    /// `AssetInventory.status` は**導入済みでも `.supported` を返す**ため、キャッシュしないと
    /// 録音のたびにダウンロード要求の経路を通る（実測 3〜9ms。進捗通知が暴発する経路でもある）。
    /// 猶予（`assetProgressGrace`）は「たまたま遅かった 1 回」を防げないので、確認自体を
    /// 1 プロセス 1 回に減らして根を断つ。認識開始に失敗したら取り消して再確認へ戻す。
    private static let readyLock = NSLock()
    private static var readyLocales: Set<String> = []

    /// 指定ロケールのアセットが確認済みか（＝アセット確認を丸ごと省いてよいか）
    static func isAssetReady(_ identifier: String) -> Bool {
        readyLock.lock(); defer { readyLock.unlock() }
        return readyLocales.contains(identifier)
    }

    /// 確認済みとして記録する
    static func markAssetReady(_ identifier: String) {
        readyLock.lock(); defer { readyLock.unlock() }
        readyLocales.insert(identifier)
    }

    /// 確認済みを取り消す（他アプリの利用でモデルが外された場合に再取得へ戻すため）
    static func clearAssetReady(_ identifier: String) {
        readyLock.lock(); defer { readyLock.unlock() }
        readyLocales.remove(identifier)
    }

    private var transcriber: SpeechTranscriber?
    private var analyzer: SpeechAnalyzer?
    private var inputContinuation: AsyncStream<AnalyzerInput>.Continuation?
    private var resultsTask: Task<Void, Never>?

    /// - Parameter locale: 認識対象の言語（既定 en-US）
    init(locale: Locale = Locale(identifier: "en-US")) {
        self.locale = locale
        var continuation: AsyncStream<Segment>.Continuation!
        self.segments = AsyncStream<Segment> { continuation = $0 }
        self.segmentContinuation = continuation
    }

    /// 必要なら言語モデルアセットを取得し、解析を開始する
    ///
    /// - Returns: `feed(_:)` に渡すべき PCM フォーマット
    /// - Throws: アセット取得や解析開始に失敗した場合
    func start() async throws -> AVAudioFormat {
        guard SpeechTranscriber.isAvailable else { throw SpeechRecognizerError.unavailable }

        // 指定ロケールに一番近い提供済みロケールへ寄せる（en-US → en_US 等の差異を吸収）
        guard let resolvedLocale = await SpeechTranscriber.supportedLocale(equivalentTo: locale) else {
            throw SpeechRecognizerError.unsupportedLocale(locale.identifier)
        }
        let localeKey = resolvedLocale.identifier(.bcp47)
        logger.notice("音声認識ロケール: \(localeKey, privacy: .public)")

        // 開始に失敗したらアセットの「確認済み」を取り消し、次回はアセット確認からやり直す
        // （モデルが外された状態のままセッション中ずっと無音になるのを防ぐ）
        var startedSuccessfully = false
        defer { if !startedSuccessfully { Self.clearAssetReady(localeKey) } }

        // ボラタイル結果（途中経過）を有効にして、字幕が発話中から出るようにする。
        //
        // `.fastResults` は精度より速さを優先して結果を早めに出す指定。
        // これが無いと途中経過そのものが実測で **音声から約 3.0 秒遅れて**届き、
        // 後段をいくら速くしても字幕は追いつかない（遅延の主因は認識側だった）。
        let transcriber = SpeechTranscriber(
            locale: resolvedLocale,
            transcriptionOptions: [],
            reportingOptions: [.volatileResults, .fastResults],
            attributeOptions: [.audioTimeRange]
        )
        self.transcriber = transcriber

        try await ensureAssetsInstalled(for: transcriber, locale: resolvedLocale)

        guard let format = await SpeechAnalyzer.bestAvailableAudioFormat(compatibleWith: [transcriber]) else {
            throw SpeechRecognizerError.noCompatibleFormat
        }
        // commonFormat も記録する。Int16 が返る環境があり、Float32 前提で書いた処理が
        // 無言でゼロを返す事故（レベル計測が常に 0）を起こしたことがあるため。
        logger.notice(
            "解析入力フォーマット: \(format.sampleRate, privacy: .public)Hz \(format.channelCount, privacy: .public)ch commonFormat=\(format.commonFormat.rawValue, privacy: .public)"
        )

        let (inputStream, inputContinuation) = AsyncStream<AnalyzerInput>.makeStream()
        self.inputContinuation = inputContinuation

        let analyzer = SpeechAnalyzer(modules: [transcriber])
        self.analyzer = analyzer
        // モデルの初期化コストを先払いして、最初の発話を取りこぼさないようにする
        try await analyzer.prepareToAnalyze(in: format)
        try await analyzer.start(inputSequence: inputStream)

        resultsTask = Task { [weak self] in
            await self?.consumeResults(from: transcriber)
        }

        logger.notice("音声認識を開始しました")
        startedSuccessfully = true
        return format
    }

    /// PCM を解析器へ投入する
    ///
    /// - Parameter buffer: `start()` が返したフォーマットの PCM。
    ///   タップ由来の noCopy バッファを直接渡してはいけない（解析は非同期で、
    ///   IO ブロックを抜けた時点で元メモリが無効になるため）。変換後の所有バッファを渡すこと。
    func feed(_ buffer: AVAudioPCMBuffer) {
        inputContinuation?.yield(AnalyzerInput(buffer: buffer))
    }

    /// 入力を打ち切り、残りを確定させてから終了する
    func finish() async {
        inputContinuation?.finish()
        inputContinuation = nil
        if let analyzer {
            try? await analyzer.finalizeAndFinishThroughEndOfInput()
        }
        // 結果ストリームが閉じるのを待ってから後段のストリームを閉じる
        await resultsTask?.value
        resultsTask = nil
        analyzer = nil
        transcriber = nil
        segmentContinuation.finish()
        logger.notice("音声認識を終了しました")
    }

    // MARK: - 内部処理

    /// 言語モデルアセットが未導入ならダウンロードする
    ///
    /// - Parameters:
    ///   - transcriber: 対象モジュール
    ///   - locale: 予約するロケール
    private func ensureAssetsInstalled(for transcriber: SpeechTranscriber, locale: Locale) async throws {
        // このプロセスで一度確認できていれば丸ごと省く（録音のクリティカルパスから外す）
        let localeKey = locale.identifier(.bcp47)
        if Self.isAssetReady(localeKey) { return }

        let status = await AssetInventory.status(forModules: [transcriber])
        logger.notice("言語モデルアセットの状態: \(String(describing: status), privacy: .public)")

        if status != .installed {
            if let request = try await AssetInventory.assetInstallationRequest(supporting: [transcriber]) {
                let progress = request.progress
                // **導入済みでも status は `.supported` を返すことがあり、この経路は録音のたびに通る**
                // （実測: `downloadAndInstall()` が 5ms で完了する＝実ダウンロードは起きていない）。
                // 以前は即座に「ダウンロード中 0%」を流していたため、毎録音で HUD の通知が
                // 録音中の波形表示を上書きしていた。**本当に待たされているときだけ**知らせるよう、
                // 猶予を置いてまだ終わっていない場合の最初の tick から進捗を出す。
                let progressTask = Task {
                    try? await Task.sleep(for: .seconds(Self.assetProgressGrace))
                    while !Task.isCancelled {
                        let percent = Int(progress.fractionCompleted * 100)
                        self.logger.notice("言語モデルをダウンロード中 \(percent, privacy: .public)%")
                        // 文言は短く保つ（HUD のピル内に収める。長いと頭が切れて読めない）
                        self.onAssetProgress?("モデル準備中 \(percent)%")
                        try? await Task.sleep(for: .seconds(2))
                    }
                }
                defer { progressTask.cancel() }
                let startedAt = CFAbsoluteTimeGetCurrent()
                try await request.downloadAndInstall()
                let elapsed = CFAbsoluteTimeGetCurrent() - startedAt
                if elapsed >= Self.assetProgressGrace {
                    logger.notice("言語モデルのダウンロードが完了しました")
                } else {
                    // 実ダウンロードなし（導入済み）。毎録音で出るので 1 行だけに留める。
                    logger.notice("言語モデルは導入済み（即完了 \(Int(elapsed * 1000), privacy: .public)ms）")
                }
            } else {
                logger.notice("ダウンロード要求は不要でした（既に利用可能）")
            }
        }

        // ロケール枠を予約しておかないと、他アプリの利用でモデルが外されることがある
        do {
            let reserved = try await AssetInventory.reserve(locale: locale)
            logger.notice("ロケール予約: \(reserved, privacy: .public)")
        } catch {
            logger.notice("ロケール予約に失敗（動作は継続）: \(String(describing: error), privacy: .public)")
        }

        // ここまで来れば使える状態。次の録音からはこの関数ごと素通りする
        Self.markAssetReady(localeKey)
    }

    /// 認識結果を読み出して後段ストリームへ流す
    private func consumeResults(from transcriber: SpeechTranscriber) async {
        do {
            for try await result in transcriber.results {
                let text = String(result.text.characters)
                guard !text.isEmpty else { continue }
                let segment = Segment(text: text, isFinal: result.isFinal, range: result.range)
                if result.isFinal {
                    logger.notice("確定: \(text, privacy: .public)")
                }
                segmentContinuation.yield(segment)
            }
        } catch {
            logger.error("認識結果の受信が中断: \(String(describing: error), privacy: .public)")
        }
    }
}
