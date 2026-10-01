//
//  SonioxLiveTranscriber.swift
//  Soniox のリアルタイム文字起こし（stt-rt-v5・WebSocket ストリーミング）
//
//  2026-10-01 に Deepgram の後継として追加した（Mac のみ）。公開ベンチで速さが最上位で、
//  日本語・英語・中国語の混在にも強いため、ライブ型の既定にする。
//  他のライブ型（OpenAILiveTranscriber）と同じ LiveTranscribing 契約で、録音中の 16kHz PCM を
//  逐次送り、離鍵時に確定テキストを返す。REST 経路は持たない。ライブ接続が失敗して文字が
//  取れなかったときは、Transcriber が録音全体を新しいセッションへ流し直して救う。
//
//  プロトコルの要点:
//  - 接続直後の最初のテキストフレームで設定 JSON（api_key・モデル・音声形式・言語ヒント）を送る
//  - 音声は Int16 LE の生バイナリフレームで送る（base64 も リサンプルも不要）
//  - 停止は 200ms の無音 → {"type":"finalize"} → 長さ 0 のバイナリフレーム。
//    <fin> トークンが来た時点で確定文が揃う（"finished":true を待たずに返す）
//  - 応答の tokens は is_final のものを到着順に足し、非確定のものは毎回置き換える
//

import Foundation
import os.log

private let log = Logger(subsystem: "com.voicekey.app", category: "stream")

/// Soniox の応答を確定文・途中文へ組み立てる純ロジック（通信と切り離してテストするため分けている）。
struct SonioxTranscript {

    /// 応答 1 件を取り込んだ結果
    enum Outcome: Equatable {
        /// 確定文か途中文が変わった
        case updated
        /// finalize の完了印（<fin>）が来た＝それまでに送った音声の確定文が揃った
        case finalized
        /// サーバーが全音声の処理を終えた（"finished": true）
        case finished
        /// エラー応答（メッセージ本文は持たない。コードと種別だけ。無い項目は nil）
        case error(code: String?, type: String?)
        /// 表示に影響しない応答
        case ignored
    }

    /// 確定済みトークンを到着順に連結したもの（以後変わらない）
    private(set) var finalText = ""
    /// 直近の応答の非確定トークン（応答ごとに丸ごと置き換わる）
    private(set) var interimText = ""

    /// いま見せるべき全文（確定 + 途中）
    var fullText: String { finalText + interimText }

    /// finalize 完了の印
    private static let finToken = "<fin>"
    /// 制御用の特殊トークン（finalize 完了・発話終端の印）。文字起こし結果には含めない
    private static let controlTokens: Set<String> = [finToken, "<end>"]

    /// 応答 JSON 1 件を取り込む
    mutating func apply(_ data: Data) -> Outcome {
        guard let obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else {
            return .ignored
        }
        // "error_code": null のようにキーだけあるものはエラー扱いにしない（NSNull は無いのと同じ）
        let code = Self.nonNull(obj["error_code"])
        let type = Self.nonNull(obj["error_type"])
        if code != nil || type != nil {
            return .error(code: code.map(Self.describe), type: type.map(Self.describe))
        }
        let before = (finalText, interimText)
        var sawFin = false
        if let tokens = obj["tokens"] as? [[String: Any]] {
            var interim = ""
            for token in tokens {
                guard let text = token["text"] as? String else { continue }
                if text == Self.finToken { sawFin = true }
                if Self.controlTokens.contains(text) { continue }
                if (token["is_final"] as? Bool) == true {
                    finalText += text
                } else {
                    interim += text
                }
            }
            interimText = interim
        }
        // 最後の応答にも tokens が載ることがあるので、取り込んでから終端を判定する
        if (obj["finished"] as? Bool) == true { return .finished }
        if sawFin { return .finalized }
        return (finalText, interimText) == before ? .ignored : .updated
    }

    private static func nonNull(_ value: Any?) -> Any? {
        guard let value, !(value is NSNull) else { return nil }
        return value
    }

    /// エラーコードは数値・文字列のどちらで来ても文字列にそろえる（401 も "401" も同じに扱うため）
    private static func describe(_ value: Any) -> String {
        "\(value)"
    }
}

/// Soniox WebSocket による逐次文字起こしセッション（1 録音 = 1 インスタンス）
final class SonioxLiveTranscriber: LiveTranscribing, @unchecked Sendable {

    var onInterim: ((String) -> Void)?

    /// 本番の接続先（テストはローカルの WebSocket サーバーへ差し替える）
    static let defaultEndpoint = URL(string: "wss://stt-rt.soniox.com/transcribe-websocket")!
    /// 録音側のサンプルレート（そのまま送る＝リサンプル不要）
    private static let sampleRate = 16000
    /// finalize の直前に送る 200ms の無音（16kHz・Int16 で 6,400 バイト）。
    /// Soniox の manual finalization の推奨。離鍵ぎりぎりで切れた語尾も確定されやすくなる
    static let trailingSilence = Data(count: sampleRate / 5 * 2)
    /// 離鍵後に完了を待つ上限（秒）。OpenAI ライブと同じ
    static let defaultFinishTimeout: TimeInterval = 3

    /// 録音全体を流し直したときに完了を待つ上限（秒）。
    /// 実時間より速く送った音声を Soniox がどれだけで処理し終えるかは公式に明記が無いので、
    /// 「3 秒 + 音声長の半分」を最大 15 秒で頭打ちにしておく（鍵発行後に実測で詰める）
    static func replayFinishTimeout(audioSeconds: Double) -> TimeInterval {
        min(15, 3 + max(0, audioSeconds) * 0.5)
    }

    private let model: String
    private let language: String
    private let prompt: String
    private let endpoint: URL
    /// 録音のたびに URLSession を作って invalidate すると CFNetwork 側のオブジェクトが
    /// 録音回数分残り続けるため、OpenAI ライブと同じく static 共有にして invalidate 自体をやめる。
    private static let sharedSession: URLSession = {
        let cfg = URLSessionConfiguration.ephemeral
        cfg.timeoutIntervalForRequest = 30
        return URLSession(configuration: cfg)
    }()
    private var task: URLSessionWebSocketTask?

    private let lock = NSLock()
    private var transcript = SonioxTranscript()
    /// 接続確立前に届いた PCM の退避（接続時に順序を保ってフラッシュする）
    private var pending: [Data] = []
    private var cancelled = false
    /// finish() が呼ばれた（＝終端を送った／接続確立後に connect() が送る）
    private var closeRequested = false
    private var finishContinuation: CheckedContinuation<Void, Never>?
    private var done = false
    /// finish がどう解決したか（最初の解決だけを記録する）
    private var endState: LiveEnding = .unknown
    private let createdAt = Date()
    private var firstResultLogged = false

    init(model: String, language: String, prompt: String, endpoint: URL = SonioxLiveTranscriber.defaultEndpoint) {
        self.model = model
        self.language = language
        self.prompt = prompt
        self.endpoint = endpoint
    }

    var ending: LiveEnding {
        lock.lock(); defer { lock.unlock() }
        return endState
    }

    /// 接続直後に送る設定 JSON（テストから組み立て結果を確かめるため static に切り出している）。
    /// 言語が空（設定の「自動判定」）なら language_hints 自体を送らない＝Soniox の自動判定に任せる。
    /// language_hints_strict は送らない＝ヒント外の言語（英単語の混在など）も認識させる。
    static func configJSON(apiKey: String, model: String, language: String, prompt: String) -> String {
        let lang = language.trimmingCharacters(in: .whitespaces)
        var payload: [String: Any] = [
            "api_key": apiKey,
            "model": model,
            "audio_format": "pcm_s16le",
            "sample_rate": sampleRate,
            "num_channels": 1,
        ]
        if !lang.isEmpty { payload["language_hints"] = [lang] }
        let trimmedPrompt = prompt.trimmingCharacters(in: .whitespacesAndNewlines)
        if !trimmedPrompt.isEmpty {
            // 固有名詞・専門用語のヒント（スロットのプロンプトをそのまま文脈として渡す）
            payload["context"] = ["text": trimmedPrompt]
        }
        guard let data = try? JSONSerialization.data(withJSONObject: payload, options: [.sortedKeys]),
              let json = String(data: data, encoding: .utf8) else { return "" }
        return json
    }

    /// Float32 [-1, 1] → Int16 LE（pcm_s16le）。範囲外はクリップする
    static func pcm16(_ samples: [Float]) -> Data {
        var pcm = Data(capacity: samples.count * 2)
        for s in samples {
            let clipped = max(-1.0, min(1.0, s))
            var v = Int16(clipped * 32767).littleEndian
            withUnsafeBytes(of: &v) { pcm.append(contentsOf: $0) }
        }
        return pcm
    }

    /// WebSocket を開いて受信ループを開始する（Keychain の Soniox キーで直結する）。
    /// - Returns: キー未設定・配布版など開始できない場合 false（呼び出し側は REST 経路へ回り、
    ///   Transcriber がキー未設定のエラーを出す）
    func start() -> Bool {
        // 配布版（製品版）には Soniox を提供しない（personal 限定の選択肢）
        guard !EmbeddedKeys.isDist else {
            log.error("Soniox: 配布版では利用できません")
            return false
        }
        guard let key = Keychain.apiKey(for: .soniox) else {
            log.error("Soniox: \(Transcriber.missingKeyMessage(for: .soniox), privacy: .public)")
            return false
        }
        return start(apiKey: key)
    }

    /// キーを明示して開始する（Transcriber の再送と、実 Keychain に触れない検証用の入口）
    @discardableResult
    func start(apiKey: String) -> Bool {
        guard !apiKey.isEmpty else { return false }
        connect(key: apiKey)
        return true
    }

    /// WebSocket を開き、設定 JSON → 退避 PCM のフラッシュ → 受信開始まで行う。
    private func connect(key: String) {
        let task = Self.sharedSession.webSocketTask(with: endpoint)

        lock.lock()
        if cancelled || done {
            lock.unlock()
            task.cancel(with: .normalClosure, reason: nil)
            resolveFinish(.unknown, reason: "cancelled")
            return
        }
        // 設定は最初のフレームでなければならない（音声より先に届く必要がある）。
        // self.task を公開すると send() が直接送り始めるので、公開する前に lock 内で
        // 設定・退避 PCM（・終端）を送信キューへ積み切る。積むだけなので lock はすぐ外れる
        task.resume()
        task.send(.string(Self.configJSON(apiKey: key, model: model, language: language, prompt: prompt))) { error in
            if let error { log.error("Soniox: 設定送信エラー: \(error.localizedDescription)") }
        }
        for chunk in pending {
            task.send(.data(chunk)) { error in
                if let error { log.debug("退避 PCM 送信エラー: \(error.localizedDescription)") }
            }
        }
        pending = []
        // finish-before-connect: 退避を送り切ってから終端を送る
        if closeRequested {
            sendEndOfStream(task)
        }
        self.task = task
        lock.unlock()

        receiveLoop()
        log.notice("Soniox 開始 (model=\(self.model, privacy: .public), lang=\(self.language.isEmpty ? "auto" : self.language, privacy: .public))")
    }

    /// 終端の送信: 200ms の無音 → finalize（未確定トークンを確定させる）→ 長さ 0 のフレーム（音声の終わり）
    private func sendEndOfStream(_ task: URLSessionWebSocketTask) {
        task.send(.data(Self.trailingSilence)) { error in
            if let error { log.debug("Soniox: 末尾無音の送信エラー: \(error.localizedDescription)") }
        }
        task.send(.string("{\"type\":\"finalize\"}")) { _ in }
        task.send(.data(Data())) { error in
            if let error { log.debug("Soniox: 終端フレーム送信エラー: \(error.localizedDescription)") }
        }
    }

    /// 16kHz モノラル Float32 チャンクを送信する（audio スレッドから呼ばれる）
    func send(_ samples: [Float]) {
        guard !samples.isEmpty else { return }
        let pcm = Self.pcm16(samples)

        lock.lock()
        if cancelled || done {
            lock.unlock()
            return
        }
        guard let task else {
            pending.append(pcm)
            lock.unlock()
            return
        }
        lock.unlock()
        task.send(.data(pcm)) { error in
            if let error { log.debug("送信エラー: \(error.localizedDescription)") }
        }
    }

    /// 送信を打ち切り、確定テキストを返す（ホットキーを離したときに呼ぶ）。
    func finish() async -> String {
        await finish(timeout: Self.defaultFinishTimeout)
    }

    /// 送信を打ち切り、確定テキストを返す。
    /// 接続済みなら終端を送って <fin> か "finished" を最大 timeout 秒待つ。未接続なら close 要求を立て、
    /// 接続確立後に connect() が pending フラッシュ後に終端を送る。
    /// 未接続かつ音声ゼロなら完了は永遠に来ないので即空解決する。
    /// - Parameter timeout: 完了を待つ上限（録音全体の再送では音声長に応じて延ばす）
    func finish(timeout: TimeInterval) async -> String {
        let (connectedTask, immediateEmpty) = requestClose()
        if let connectedTask { sendEndOfStream(connectedTask) }

        if immediateEmpty {
            // 接続していない＝サーバーが処理し終えた証拠は無い。完了扱いにすると呼び出し側が
            // 録音を捨ててしまうので失敗として返し、手元の録音での救済に回させる
            resolveFinish(.failed(.disconnect), reason: "empty-noconnect")
        } else {
            await withCheckedContinuation { (cont: CheckedContinuation<Void, Never>) in
                lock.lock()
                if done {
                    lock.unlock()
                    cont.resume()
                    return
                }
                finishContinuation = cont
                lock.unlock()
                // 完了が来ない場合の最終防衛（接続ハング・終端未達対策）
                DispatchQueue.global().asyncAfter(deadline: .now() + timeout) { [weak self] in
                    self?.resolveFinish(.failed(.timeout), reason: "timeout")
                }
            }
        }
        markCancelled()?.cancel(with: .normalClosure, reason: nil)
        // タイムアウトで確定が揃わなかった場合も、届いている途中文までは使う（取りこぼしを減らす）
        return TextNormalize.stripCJKSpaces(currentText()).trimmingCharacters(in: .whitespacesAndNewlines)
    }

    /// finish() の入口: close 要求を立て、(接続済み task, 即空解決すべきか) を lock 下で判定する。
    private func requestClose() -> (task: URLSessionWebSocketTask?, immediateEmpty: Bool) {
        lock.lock(); defer { lock.unlock() }
        closeRequested = true
        if let task { return (task, false) }
        return (nil, pending.isEmpty)
    }

    /// finish/cancel 確定: cancelled を立て、その時点の task を返す
    @discardableResult
    private func markCancelled() -> URLSessionWebSocketTask? {
        lock.lock(); defer { lock.unlock() }
        cancelled = true
        return task
    }

    func cancel() {
        lock.lock()
        cancelled = true
        let t = task
        lock.unlock()
        t?.cancel(with: .normalClosure, reason: nil)
        // 結果は使われないので終わり方は区別しない
        resolveFinish(.unknown, reason: "cancelled")
    }

    private func currentText() -> String {
        lock.lock(); defer { lock.unlock() }
        return transcript.fullText
    }

    // MARK: - 受信

    private func receiveLoop() {
        task?.receive { [weak self] result in
            guard let self else { return }
            switch result {
            case .failure:
                // 接続の確立失敗も途中切断もここに来る。正常終了（<fin> / finished）が先に届いていれば
                // resolveFinish は 2 回目以降を無視するので、後始末の切断で失敗扱いに上書きされない
                self.resolveFinish(.failed(.disconnect), reason: "disconnect")
            case .success(let message):
                let data: Data?
                switch message {
                case .string(let text): data = text.data(using: .utf8)
                case .data(let raw): data = raw
                @unknown default: data = nil
                }
                if let data { self.handle(data) }
                self.receiveLoop()
            }
        }
    }

    private func handle(_ data: Data) {
        lock.lock()
        let outcome = transcript.apply(data)
        let snapshot = transcript.fullText
        let finalizeSent = closeRequested
        lock.unlock()

        switch outcome {
        case .ignored:
            return
        case .error(let code, let type):
            // メッセージ本文は出さない（設定内容が混ざる可能性があるため。コードと種別で切り分ける）
            log.error("Soniox エラー code=\(code ?? "-", privacy: .public) type=\(type ?? "-", privacy: .public)")
            resolveFinish(.failed(.error(code: code, type: type)), reason: "error")
        case .updated:
            logFirstResultOnce()
            onInterim?(TextNormalize.stripCJKSpaces(snapshot))
        case .finalized:
            if !snapshot.isEmpty {
                logFirstResultOnce()
                onInterim?(TextNormalize.stripCJKSpaces(snapshot))
            }
            // finalize を送った後の <fin> ＝ 送った音声の確定文はすべて揃った。
            // "finished"（接続の後始末）まで待つと離鍵から貼り付けまでが延びるので、ここで返す
            if finalizeSent { resolveFinish(.completed, reason: "fin") }
        case .finished:
            if !snapshot.isEmpty { onInterim?(TextNormalize.stripCJKSpaces(snapshot)) }
            resolveFinish(.completed, reason: "finished")
        }
    }

    /// 録音開始から「最初の文字が出るまで」の体感遅延を 1 度だけログ（他のライブ型と対称）
    private func logFirstResultOnce() {
        lock.lock()
        let alreadyLogged = firstResultLogged
        firstResultLogged = true
        lock.unlock()
        if !alreadyLogged {
            log.notice("Soniox 最初の文字まで \(Int(Date().timeIntervalSince(self.createdAt) * 1000), privacy: .public)ms（録音開始から）")
        }
    }

    /// finish() の継続を一度だけ解決する（<fin> / finished / 切断 / エラー / タイムアウトのいずれか）。
    /// 終わり方は最初の解決だけを記録する（後から来た切断・タイムアウトで上書きしない）
    private func resolveFinish(_ result: LiveEnding, reason: String) {
        lock.lock()
        let cont = finishContinuation
        finishContinuation = nil
        let wasDone = done
        done = true
        if !wasDone { endState = result }
        lock.unlock()
        if !wasDone {
            log.notice("Soniox finish 解決: \(reason, privacy: .public)")
        }
        cont?.resume()
    }
}
