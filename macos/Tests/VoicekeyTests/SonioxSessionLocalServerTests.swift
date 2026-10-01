//
//  SonioxSessionLocalServerTests.swift
//  SonioxLiveTranscriber の実接続回帰（相手はローカルの WebSocket サーバー・外部通信なし）
//
//  文字列の組み立てを見る単体テストだけでは、「送る順番」「<fin> で早く返るか」「エラー・切断を
//  どう終わったことにするか」といった、実際に WebSocket で会話して初めて分かる配線ミスを捕まえられない。
//  そこで Network.framework の NWListener で 127.0.0.1 の空きポートに WebSocket サーバーを立て、
//  接続先だけ差し替えた本物のセッションと会話させる。Soniox の課金 API には一切つながない。
//  キーはダミー（start(apiKey:)）で、実 Keychain にも触れない。
//

import Network
import XCTest
@testable import voicekey

/// テスト用のローカル WebSocket サーバー（127.0.0.1 の空きポートだけで待ち受ける）
private final class LocalWebSocketServer: @unchecked Sendable {

    enum Frame: Equatable {
        case text(String)
        case binary(Data)
    }

    /// フレームを受け取るたびに呼ぶ（サーバー側の応答を決める）
    typealias Responder = (_ frame: Frame, _ server: LocalWebSocketServer, _ connection: NWConnection) -> Void

    private let listener: NWListener
    private let queue = DispatchQueue(label: "voicekey.test.local-ws")
    private let lock = NSLock()
    private var frames: [Frame] = []
    private var connections: [NWConnection] = []
    private let closeOnConnect: Bool
    private let responder: Responder

    /// - Parameters:
    ///   - closeOnConnect: 接続（ハンドシェイク完了）直後にサーバーから切る
    ///   - responder: 受信フレームごとの応答
    init(closeOnConnect: Bool = false, responder: @escaping Responder = { _, _, _ in }) throws {
        let params = NWParameters.tcp
        let ws = NWProtocolWebSocket.Options()
        ws.autoReplyPing = true
        params.defaultProtocolStack.applicationProtocols.insert(ws, at: 0)
        // ループバックだけで待ち受ける（外から届かない・ファイアウォールの許可も要らない）
        params.requiredLocalEndpoint = .hostPort(host: .ipv4(.loopback), port: .any)
        listener = try NWListener(using: params)
        self.closeOnConnect = closeOnConnect
        self.responder = responder
    }

    /// 待ち受けを始め、接続先 URL（ws://127.0.0.1:<port>）を返す
    func start() async throws -> URL {
        listener.newConnectionHandler = { [weak self] connection in
            self?.accept(connection)
        }
        let port: UInt16 = try await withCheckedThrowingContinuation { cont in
            let once = OnceFlag()
            listener.stateUpdateHandler = { [weak self] state in
                switch state {
                case .ready:
                    if once.take() { cont.resume(returning: self?.listener.port?.rawValue ?? 0) }
                case .failed(let error):
                    if once.take() { cont.resume(throwing: error) }
                default:
                    break
                }
            }
            listener.start(queue: queue)
        }
        return URL(string: "ws://127.0.0.1:\(port)")!
    }

    func stop() {
        listener.cancel()
        lock.lock()
        let all = connections
        connections = []
        lock.unlock()
        all.forEach { $0.cancel() }
    }

    func receivedFrames() -> [Frame] {
        lock.lock(); defer { lock.unlock() }
        return frames
    }

    func sendText(_ text: String, on connection: NWConnection, then: (() -> Void)? = nil) {
        let metadata = NWProtocolWebSocket.Metadata(opcode: .text)
        let context = NWConnection.ContentContext(identifier: "text", metadata: [metadata])
        connection.send(content: Data(text.utf8), contentContext: context, isComplete: true,
                        completion: .contentProcessed { _ in then?() })
    }

    private func accept(_ connection: NWConnection) {
        lock.lock()
        connections.append(connection)
        lock.unlock()
        connection.stateUpdateHandler = { [weak self] state in
            guard let self, case .ready = state else { return }
            if self.closeOnConnect {
                connection.cancel()
            } else {
                self.receive(on: connection)
            }
        }
        connection.start(queue: queue)
    }

    private func receive(on connection: NWConnection) {
        connection.receiveMessage { [weak self] content, context, _, error in
            guard let self, error == nil,
                  let metadata = context?.protocolMetadata(definition: NWProtocolWebSocket.definition)
                    as? NWProtocolWebSocket.Metadata else { return }
            let frame: Frame
            switch metadata.opcode {
            case .text:
                frame = .text(String(decoding: content ?? Data(), as: UTF8.self))
            case .binary:
                // 長さ 0 のフレームは content が nil で届くことがある
                frame = .binary(content ?? Data())
            case .close:
                return
            default:
                self.receive(on: connection)
                return
            }
            self.lock.lock()
            self.frames.append(frame)
            self.lock.unlock()
            self.responder(frame, self, connection)
            self.receive(on: connection)
        }
    }
}

/// continuation を一度だけ再開するための印
private final class OnceFlag: @unchecked Sendable {
    private let lock = NSLock()
    private var taken = false
    func take() -> Bool {
        lock.lock(); defer { lock.unlock() }
        if taken { return false }
        taken = true
        return true
    }
}

final class SonioxSessionLocalServerTests: XCTestCase {

    private static let finalizeJSON = #"{"type":"finalize"}"#
    /// 100ms 分の音声（値に意味は無い。PCM の中身が崩れずに届くかを見る）
    private let chunkA: [Float] = (0..<1600).map { Float($0 % 200) / 400 - 0.25 }
    private let chunkB: [Float] = (0..<1600).map { _ in 0.1 }

    private func makeSession(_ url: URL, language: String = "ja") -> SonioxLiveTranscriber {
        SonioxLiveTranscriber(model: "stt-rt-v5", language: language, prompt: "", endpoint: url)
    }

    // (a) 送る順番: 設定 JSON（text）→ PCM（binary）→ 200ms の無音 → finalize（text）→ 空の binary。
    //     接続前に届いた PCM も、設定より後・接続後の PCM より前に並ぶ
    func testFrameOrder() async throws {
        let server = try LocalWebSocketServer { frame, server, connection in
            // 音声の終わり（空フレーム）まで届いたら完了を返す＝全フレームを記録し終えてから finish が返る
            if frame == .binary(Data()) {
                server.sendText(#"{"tokens":[{"text":"<fin>","is_final":true}],"finished":true}"#, on: connection)
            }
        }
        let url = try await server.start()
        defer { server.stop() }

        let session = makeSession(url)
        session.send(chunkA)                     // 接続前＝退避に積まれる
        XCTAssertTrue(session.start(apiKey: "test-key"))
        session.send(chunkB)                     // 接続後＝そのまま送る
        let text = await session.finish()

        XCTAssertEqual(text, "")
        XCTAssertEqual(session.ending, .completed)
        let frames = server.receivedFrames()
        XCTAssertEqual(frames.count, 6, "\(frames)")
        guard frames.count == 6 else { return }

        guard case .text(let config) = frames[0] else { return XCTFail("最初は設定 JSON（text）: \(frames[0])") }
        let obj = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(config.utf8)) as? [String: Any])
        XCTAssertEqual(obj["api_key"] as? String, "test-key")
        XCTAssertEqual(obj["model"] as? String, "stt-rt-v5")
        XCTAssertEqual(obj["language_hints"] as? [String], ["ja"])

        XCTAssertEqual(frames[1], .binary(SonioxLiveTranscriber.pcm16(chunkA)))
        XCTAssertEqual(frames[2], .binary(SonioxLiveTranscriber.pcm16(chunkB)))
        XCTAssertEqual(frames[3], .binary(Data(count: 6_400)), "finalize の前に 200ms の無音（6,400 バイト）")
        XCTAssertEqual(frames[4], .text(Self.finalizeJSON))
        XCTAssertEqual(frames[5], .binary(Data()))
    }

    // (b) finalize への <fin> が来たら、"finished" を待たずに finish が返り、終わり方は completed
    func testFinTokenResolvesFinishEarly() async throws {
        let server = try LocalWebSocketServer { frame, server, connection in
            switch frame {
            case .binary(let pcm) where !pcm.isEmpty && pcm != Data(count: 6_400):
                server.sendText(#"{"tokens":[{"text":"こんにち","is_final":false}]}"#, on: connection)
            case .text(Self.finalizeJSON):
                // <fin> だけ返し、finished も切断もしない（<fin> で返らなければタイムアウトまで待つ）
                server.sendText(
                    #"{"tokens":[{"text":"こんにちは","is_final":true},{"text":"<fin>","is_final":true}]}"#,
                    on: connection
                )
            default:
                break
            }
        }
        let url = try await server.start()
        defer { server.stop() }

        let session = makeSession(url)
        XCTAssertTrue(session.start(apiKey: "test-key"))
        session.send(chunkA)
        let t0 = Date()
        let text = await session.finish(timeout: 5)
        let elapsed = Date().timeIntervalSince(t0)

        XCTAssertEqual(text, "こんにちは")
        XCTAssertEqual(session.ending, .completed)
        XCTAssertLessThan(elapsed, 2.5, "<fin> で返るはずが、タイムアウトまで待った")
    }

    // (c) error_code 401 の応答 → 終わり方は failed で、コードと種別を保持する
    func testErrorResponseKeepsCode() async throws {
        let server = try LocalWebSocketServer { frame, server, connection in
            if case .text(let text) = frame, text.contains("api_key") {
                server.sendText(
                    #"{"error_code":401,"error_type":"unauthenticated","error_message":"Invalid API key"}"#,
                    on: connection
                ) { connection.cancel() }
            }
        }
        let url = try await server.start()
        defer { server.stop() }

        let session = makeSession(url)
        XCTAssertTrue(session.start(apiKey: "test-key"))
        session.send(chunkA)
        let text = await session.finish(timeout: 5)

        XCTAssertEqual(text, "")
        XCTAssertEqual(session.ending, .failed(.error(code: "401", type: "unauthenticated")))
        guard case .failed(let failure) = session.ending else { return XCTFail("failed のはず") }
        XCTAssertTrue(Transcriber.sonioxFailureMessage(failure).contains("API キーが無効"))
    }

    // (d) 正常に finished で終わってトークンが空 → completed かつ空文字（本当に無言）
    func testFinishedWithEmptyTokensIsCompletedEmpty() async throws {
        let server = try LocalWebSocketServer { frame, server, connection in
            if frame == .binary(Data()) {
                server.sendText(#"{"tokens":[],"finished":true}"#, on: connection)
            }
        }
        let url = try await server.start()
        defer { server.stop() }

        let session = makeSession(url, language: "")
        XCTAssertTrue(session.start(apiKey: "test-key"))
        session.send(chunkB)
        let text = await session.finish(timeout: 5)

        XCTAssertEqual(text, "")
        XCTAssertEqual(session.ending, .completed)
        // 言語が空（自動判定）なら language_hints を送らない
        guard case .text(let config)? = server.receivedFrames().first else { return XCTFail("設定 JSON が届いていない") }
        let obj = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(config.utf8)) as? [String: Any])
        XCTAssertNil(obj["language_hints"])
    }

    // (e) サーバーが接続直後に切る → 終わり方は failed（切断）。完了扱いにして録音を捨てない
    func testImmediateServerDisconnectIsFailed() async throws {
        let server = try LocalWebSocketServer(closeOnConnect: true)
        let url = try await server.start()
        defer { server.stop() }

        let session = makeSession(url)
        XCTAssertTrue(session.start(apiKey: "test-key"))
        session.send(chunkA)
        let text = await session.finish(timeout: 5)

        XCTAssertEqual(text, "")
        XCTAssertEqual(session.ending, .failed(.disconnect))
    }
}
