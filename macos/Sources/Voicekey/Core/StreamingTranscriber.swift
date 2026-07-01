//
//  StreamingTranscriber.swift
//  Deepgram リアルタイム文字起こし（WebSocket ストリーミング）
//
//  録音中の 16kHz PCM チャンクを WebSocket で逐次送り、暫定（interim）と
//  確定（is_final）のテキストをリアルタイムに受け取る。HUD のライブ字幕に使う。
//  ベンチマークで Deepgram nova-3 が「離した瞬間にほぼ確定（60〜170ms）」かつ
//  最高精度だったため、ストリーミングの既定バックエンドとして採用している。
//
//  外部依存を増やさないため URLSessionWebSocketTask（標準 API）を使う。
//  送信は audio スレッド、受信は URLSession のキュー、開始/終了はメインから
//  呼ばれるため、共有状態は NSLock で保護する。
//

import Foundation
import os.log

private let log = Logger(subsystem: "com.voicekey.app", category: "stream")

/// Deepgram WebSocket による逐次文字起こしセッション（1 録音 = 1 インスタンス）
final class StreamingTranscriber: @unchecked Sendable {

    /// 現在の全文（確定 + 暫定）の更新通知。HUD 用にメインへホップして使う想定
    var onInterim: ((String) -> Void)?

    private let model: String
    private let language: String
    private let session: URLSession
    private var task: URLSessionWebSocketTask?

    private let lock = NSLock()
    /// 確定済みセグメント（is_final=true の順次追記）
    private var finals: [String] = []
    /// 直近の暫定セグメント（次の確定で置き換わる）
    private var interim: String = ""
    /// 接続確立前に届いた PCM の退避（接続時に順序を保ってフラッシュする）。
    /// 製品版（ログイン）は短命 JWT 取得が非同期なので、接続前に send() が来うる。
    private var pending: [Data] = []
    /// finish/cancel が接続確立前に呼ばれた（接続完了後に即破棄させる）
    private var cancelled = false
    /// finish() を解決する継続。二重 resume を防ぐため lock 下で nil 化する
    private var finishContinuation: CheckedContinuation<Void, Never>?
    /// 接続が閉じた / 確定し終えたか
    private var done = false
    /// 録音開始遅延の計測用（生成時刻 ≒ 録音開始）。トークン往復 / 最初の文字までの累計を
    /// ログし、「始まりが遅い」の主因（サーバー往復か WS ハンドシェイクか）を裏取りする
    private let createdAt = Date()
    private var firstResultLogged = false
    /// 無料体験の保留 ID（段階1 hold 経路のみ非null）。文字起こし成功後に消費を確定するのに使う。
    private var ephemeralJti: String?
    /// 段階3: 再利用トークン（free）＝録音成功ごとに jti なし confirm で 1 消費すべきか。
    private var ephemeralMeter = false

    init(model: String, language: String) {
        self.model = model
        self.language = language
        let cfg = URLSessionConfiguration.ephemeral
        cfg.timeoutIntervalForRequest = 30
        self.session = URLSession(configuration: cfg)
    }

    /// Deepgram の言語パラメータ（REST 側 Transcriber と同じ規則）。
    /// nova-3 は日本語の単言語指定に未対応のため多言語モードを使う。
    private var streamLanguage: String {
        if model.hasPrefix("nova-3") { return "multi" }
        return language.isEmpty ? "ja" : language
    }

    /// WebSocket を開いて受信ループを開始する。
    ///
    /// 製品版（ログイン済み）はサーバーから短命 JWT を取得して `Bearer` で接続する。
    /// この取得は非同期なので接続確立前に send() が来うる（→ pending に退避）。
    /// 未ログインは従来どおり埋め込み/設定キーを `Token` で使う（並存ガード）。
    /// - Returns: キー未設定かつ未ログインなど開始できない場合 false（呼び出し側は REST にフォールバック）
    func start() -> Bool {
        let loggedIn = BackendClient.isLoggedIn
        // 配布版（製品版ビルド）はログイン必須＝未ログインでは埋め込みキーを使わない。
        // その場合 false を返し、ゲート済みの REST 経路に委ねてログイン要求エラーを表示させる。
        let key = EmbeddedKeys.isDist ? nil : Keychain.apiKey(for: .deepgram)
        guard loggedIn || key != nil else {
            log.error("Deepgram: 未ログイン（配布版はログイン必須）またはキー未設定のためストリーミングを開始できません")
            return false
        }

        if loggedIn {
            // 製品版: サーバーから短命 JWT を取得してから Bearer で接続する
            Task { [weak self] in
                guard let self else { return }
                do {
                    let t0 = Date()
                    let tok = try await BackendClient.fetchEphemeralToken()
                    // 録音成功後に消費を確定するための情報を控える。段階1=保留 ID(jti)、
                    // 段階3=再利用トークンの回数計測フラグ(meter)。どちらも録音成立時のみ送る。
                    self.lock.lock(); self.ephemeralJti = tok.jti; self.ephemeralMeter = tok.meter; self.lock.unlock()
                    // 「始まりが遅い」の主因切り分け用ログ（トークン往復 ms）
                    log.info("Deepgram 短命トークン取得 \(Int(Date().timeIntervalSince(t0) * 1000), privacy: .public)ms")
                    self.connect(auth: "Bearer \(tok.token)")
                } catch {
                    log.error("Deepgram 短命トークンの取得に失敗: \(error.localizedDescription)")
                    self.resolveFinish()  // finish() を解決し REST フォールバックに委ねる
                }
            }
        } else {
            // 未ログイン: 従来の Token 直叩き（key は guard で非 nil 確定）
            connect(auth: "Token \(key!)")
        }
        return true
    }

    /// 指定の Authorization ヘッダで WebSocket を開き、退避 PCM をフラッシュして受信を開始する。
    /// 接続確立前に finish/cancel されていたら（cancelled/done）即破棄する。
    private func connect(auth: String) {
        var comps = URLComponents(string: "wss://api.deepgram.com/v1/listen")!
        comps.queryItems = [
            URLQueryItem(name: "model", value: model),
            URLQueryItem(name: "language", value: streamLanguage),
            URLQueryItem(name: "encoding", value: "linear16"),
            URLQueryItem(name: "sample_rate", value: "16000"),
            URLQueryItem(name: "channels", value: "1"),
            URLQueryItem(name: "interim_results", value: "true"),
            URLQueryItem(name: "punctuate", value: "true"),
            URLQueryItem(name: "smart_format", value: "true"),
        ]
        var request = URLRequest(url: comps.url!)
        request.setValue(auth, forHTTPHeaderField: "Authorization")

        let task = session.webSocketTask(with: request)

        // 退避済み PCM を順序保証のためロック下で取り出してから task を公開する
        lock.lock()
        if cancelled || done {
            lock.unlock()
            task.cancel(with: .normalClosure, reason: nil)
            resolveFinish()
            return
        }
        self.task = task
        let buffered = pending
        pending = []
        lock.unlock()

        task.resume()
        for chunk in buffered {
            task.send(.data(chunk)) { error in
                if let error { log.debug("退避 PCM 送信エラー: \(error.localizedDescription)") }
            }
        }
        receiveLoop()
        log.info("Deepgram ストリーミング開始 (model=\(self.model, privacy: .public), lang=\(self.streamLanguage, privacy: .public))")
    }

    /// 16kHz モノラル Float32 チャンクを送信する（audio スレッドから呼ばれる）。
    /// 接続確立前のチャンクは pending に退避し、connect() がフラッシュする。
    func send(_ samples: [Float]) {
        guard !samples.isEmpty else { return }
        // Float32[-1,1] → Int16 LE 生バイト（範囲外はクリップして轟音化を防ぐ）
        var pcm = Data(capacity: samples.count * 2)
        for s in samples {
            let clipped = max(-1.0, min(1.0, s))
            var v = Int16(clipped * 32767).littleEndian
            withUnsafeBytes(of: &v) { pcm.append(contentsOf: $0) }
        }
        lock.lock()
        if cancelled || done {
            lock.unlock()
            return
        }
        guard let task else {
            // まだ接続前 → 退避（接続確立時に connect がフラッシュする）
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
    /// CloseStream を送ると Deepgram は残りを確定して Metadata を返し接続を閉じる。
    /// 取りこぼし対策で最大 3 秒だけ待つ。
    func finish() async -> String {
        // CloseStream で残りバッファの確定を促す（接続前なら task は nil で no-op）
        currentTask()?.send(.string("{\"type\":\"CloseStream\"}")) { _ in }

        await withCheckedContinuation { (cont: CheckedContinuation<Void, Never>) in
            lock.lock()
            if done {
                lock.unlock()
                cont.resume()
                return
            }
            finishContinuation = cont
            lock.unlock()
            // 確定が来ない場合の保険（接続ハング対策）
            DispatchQueue.global().asyncAfter(deadline: .now() + 3) { [weak self] in
                self?.resolveFinish()
            }
        }
        // 接続が JWT 取得中などで未確立でも、後から connect が走らないよう cancelled を立てる
        markCancelled()?.cancel(with: .normalClosure, reason: nil)
        // 録音のたびに生成するセッションは明示的に破棄する（放置すると漸増リーク）
        session.finishTasksAndInvalidate()
        let text = TextNormalize.stripCJKSpaces(currentText())
        // 文字起こしが成立したら無料体験の消費を確定する（ベストエフォート・非ブロッキング）。
        // 空文字（無音/接続失敗で REST フォールバック）のときは確定しない＝保留は TTL で戻る。
        if !text.isEmpty {
            lock.lock(); let jti = ephemeralJti; let meter = ephemeralMeter; ephemeralJti = nil; ephemeralMeter = false; lock.unlock()
            if let jti {
                Task { await BackendClient.confirmUsage(jti: jti) }  // 段階1: 保留を確定
            } else if meter {
                Task { await BackendClient.confirmUsageCount() }     // 段階3: 再利用トークンの回数を +1
            }
        }
        return text
    }

    /// lock 下で現在の task を読む（async コンテキストから直接 lock しないため）
    private func currentTask() -> URLSessionWebSocketTask? {
        lock.lock(); defer { lock.unlock() }
        return task
    }

    /// finish/cancel 確定: cancelled を立て、その時点の task を返す（async から呼ぶ用）
    @discardableResult
    private func markCancelled() -> URLSessionWebSocketTask? {
        lock.lock(); defer { lock.unlock() }
        cancelled = true
        return task
    }

    /// 結果を使わずに接続を破棄する（録音破棄時など）
    func cancel() {
        lock.lock()
        cancelled = true
        let t = task
        lock.unlock()
        t?.cancel(with: .normalClosure, reason: nil)
        session.finishTasksAndInvalidate()
        resolveFinish()
    }

    /// 確定 + 暫定を結合した現在の全文（生・スペース除去前）
    private func currentText() -> String {
        lock.lock(); defer { lock.unlock() }
        return (finals + (interim.isEmpty ? [] : [interim])).joined()
    }

    // MARK: - 受信

    private func receiveLoop() {
        task?.receive { [weak self] result in
            guard let self else { return }
            switch result {
            case .failure:
                // 切断・エラーは finish() を解決して REST 側の判断に委ねる
                self.resolveFinish()
            case .success(let message):
                switch message {
                case .string(let text): self.handle(text)
                case .data(let data):
                    if let text = String(data: data, encoding: .utf8) { self.handle(text) }
                @unknown default: break
                }
                self.receiveLoop()
            }
        }
    }

    /// Deepgram のストリーミング応答（必要なフィールドのみ）
    private struct DGMessage: Decodable {
        let type: String?
        let is_final: Bool?
        let channel: Channel?
        struct Channel: Decodable { let alternatives: [Alternative] }
        struct Alternative: Decodable { let transcript: String }
    }

    private func handle(_ json: String) {
        guard let data = json.data(using: .utf8),
              let msg = try? JSONDecoder().decode(DGMessage.self, from: data) else { return }

        // Metadata は CloseStream 後の終端通知 → 確定完了とみなす
        if msg.type == "Metadata" {
            resolveFinish()
            return
        }

        guard let transcript = msg.channel?.alternatives.first?.transcript else { return }
        let trimmed = transcript.trimmingCharacters(in: .whitespacesAndNewlines)

        // 録音開始から「最初の文字が出るまで」の体感遅延を 1 度だけログ（内訳の合計確認用）
        if !trimmed.isEmpty {
            lock.lock()
            let alreadyLogged = firstResultLogged
            firstResultLogged = true
            lock.unlock()
            if !alreadyLogged {
                log.info("Deepgram 最初の文字まで \(Int(Date().timeIntervalSince(self.createdAt) * 1000), privacy: .public)ms（録音開始から）")
            }
        }

        lock.lock()
        if msg.is_final == true {
            if !trimmed.isEmpty { finals.append(trimmed) }
            interim = ""
        } else {
            interim = trimmed
        }
        let snapshot = (finals + (interim.isEmpty ? [] : [interim])).joined()
        lock.unlock()

        // HUD へは日本語スペースを除去した見た目で渡す
        onInterim?(TextNormalize.stripCJKSpaces(snapshot))
    }

    /// finish() の継続を一度だけ解決する（接続終了 / Metadata / タイムアウトのいずれか）
    private func resolveFinish() {
        lock.lock()
        let cont = finishContinuation
        finishContinuation = nil
        done = true
        lock.unlock()
        cont?.resume()
    }

}
