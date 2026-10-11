//
//  SonioxCaptionTests.swift
//  ライブ字幕の Soniox エンジン（2026-10-12 追加）の単体テスト
//
//  実 API には接続しない。Soniox の応答 JSON（公式ドキュメントの形）を手で組んで
//  組み立て・解析・再接続の方針・設定 JSON だけを確かめる。
//

import XCTest

@testable import voicekey

final class SonioxCaptionTests: XCTestCase {

    // MARK: - 解析

    /// トークン・終了・エラーを読める
    func testParseResponse() throws {
        let json = """
        {"tokens":[
          {"text":"Hello","is_final":true,"translation_status":"original"},
          {"text":"こんにちは","is_final":false,"translation_status":"translation"},
          {"text":"<end>","is_final":true}
        ],"final_audio_proc_ms":100,"total_audio_proc_ms":200}
        """
        let response = try XCTUnwrap(SonioxCaptionAssembler.parse(Data(json.utf8)))
        XCTAssertEqual(response.tokens.count, 3)
        XCTAssertEqual(response.tokens[0], .init(text: "Hello", isFinal: true, isTranslation: false, awaitsTranslation: true))
        XCTAssertEqual(response.tokens[1], .init(text: "こんにちは", isFinal: false, isTranslation: true))
        XCTAssertEqual(response.tokens[2].text, SonioxCaptionAssembler.endToken)
        XCTAssertFalse(response.finished)
        XCTAssertNil(response.errorCode)

        let error = try XCTUnwrap(SonioxCaptionAssembler.parse(Data(#"{"error_code":401,"error_message":"Invalid API key"}"#.utf8)))
        XCTAssertEqual(error.errorCode, 401)
        XCTAssertEqual(error.errorMessage, "Invalid API key")

        let finished = try XCTUnwrap(SonioxCaptionAssembler.parse(Data(#"{"tokens":[],"finished":true}"#.utf8)))
        XCTAssertTrue(finished.finished)

        XCTAssertNil(SonioxCaptionAssembler.parse(Data("not json".utf8)))
    }

    // MARK: - 翻訳モード

    /// 未確定はライブ行、訳が文末まで揃ったら原文と組にして 1 行に確定する
    func testTranslateModeConfirmsWhenTranslationCompletes() {
        var assembler = SonioxCaptionAssembler(translates: true)

        // 認識途中（ライブ行だけ）
        var events = assembler.apply(response([src("Good", final: false), src(" morning", final: false)]))
        XCTAssertEqual(events, [.live(source: "Good morning", translation: "")])

        // 原文は確定したが訳はまだ
        events = assembler.apply(response([src("Good morning.", final: true), tr("おはよう", final: false)]))
        XCTAssertEqual(events, [.live(source: "Good morning.", translation: "おはよう")])

        // 訳が文末まで確定 → 1 行確定
        events = assembler.apply(response([tr("おはようございます。", final: true)]))
        XCTAssertEqual(events.first, .confirmed(source: "Good morning.", translation: "おはようございます。"))
        XCTAssertEqual(events.last, .live(source: "", translation: ""))
    }

    /// 原文の確定分に次の文の頭が入っていたら、文末までを行にして残りは持ち越す
    func testTranslateModeCarriesOverNextSentence() {
        var assembler = SonioxCaptionAssembler(translates: true)
        _ = assembler.apply(response([src("Hello there. How", final: true)]))
        let events = assembler.apply(response([tr("やあ。", final: true), src(" are", final: false)]))
        XCTAssertEqual(events.first, .confirmed(source: "Hello there.", translation: "やあ。"))
        XCTAssertEqual(events.last, .live(source: "How are", translation: ""))
        // 持ち越した分も後で確定する（1 文字も落とさない）
        XCTAssertEqual(assembler.flush(), .confirmed(source: "How are", translation: ""))
    }

    /// 発話の切れ目（<end>）が来たら、文末記号が無くても訳が揃いしだい確定する
    func testTranslateModeConfirmsAtEndToken() {
        var assembler = SonioxCaptionAssembler(translates: true)
        var events = assembler.apply(response([src("So yeah", final: true), endToken()]))
        XCTAssertEqual(events, [.live(source: "So yeah", translation: "")])
        XCTAssertTrue(assembler.isWaitingForTranslation, "訳待ちの目印が立っていない（時間切れの確定が効かない）")

        events = assembler.apply(response([tr("そうだね", final: true)]))
        XCTAssertEqual(events.first, .confirmed(source: "So yeah", translation: "そうだね"))
        XCTAssertFalse(assembler.isWaitingForTranslation)
    }

    /// 翻訳先と同じ言語（訳されない）だけの発話は、訳を待たずに原文だけで確定する
    func testTranslateModeUntranslatedSpeech() {
        var assembler = SonioxCaptionAssembler(translates: true)
        let events = assembler.apply(response([
            .init(text: "はい", isFinal: true, isTranslation: false, awaitsTranslation: false),
            endToken(),
        ]))
        XCTAssertEqual(events.first, .confirmed(source: "はい", translation: ""))
    }

    // MARK: - 文字起こしモード

    /// 文末・<end>・長さのどれかで確定する（訳は付かない）
    func testTranscribeModeConfirms() {
        var assembler = SonioxCaptionAssembler(translates: false)
        var events = assembler.apply(response([src("今日は", final: true), src("晴れ", final: false)]))
        XCTAssertEqual(events, [.live(source: "今日は晴れ", translation: "")])

        events = assembler.apply(response([src("晴れです。", final: true)]))
        XCTAssertEqual(events.first, .confirmed(source: "今日は晴れです。", translation: ""))

        events = assembler.apply(response([src("えっと", final: true), endToken()]))
        XCTAssertEqual(events.first, .confirmed(source: "えっと", translation: ""))

        let long = String(repeating: "あ", count: SonioxCaptionAssembler.maxConfirmedSourceCharacters)
        events = assembler.apply(response([src(long, final: true)]))
        XCTAssertEqual(events.first, .confirmed(source: long, translation: ""), "文末が来ないまま長くなっても確定しない")
    }

    /// 無音の切れ目だけが来ても空行を出さない
    func testEndTokenWithoutSpeechEmitsNothing() {
        var assembler = SonioxCaptionAssembler(translates: false)
        let events = assembler.apply(response([endToken()]))
        XCTAssertEqual(events, [.live(source: "", translation: "")])
        XCTAssertNil(assembler.flush())
    }

    /// 停止時の flush は未確定分も含めて 1 行にする（最後の文を議事録から落とさない）
    func testFlushIncludesPendingTokens() {
        var assembler = SonioxCaptionAssembler(translates: true)
        _ = assembler.apply(response([src("Last words", final: true), src(" before stop", final: false),
                                      tr("止める前の", final: false)]))
        XCTAssertEqual(assembler.flush(), .confirmed(source: "Last words before stop", translation: "止める前の"))
        XCTAssertNil(assembler.flush(), "flush 済みの分をもう一度出した")
    }

    // MARK: - 再接続の方針

    /// 0.5 秒から倍々・上限 8 秒・5 回で諦める。安定したら数え直す
    func testReconnectPolicyBackoff() {
        var policy = SonioxReconnectPolicy()
        XCTAssertEqual(policy.nextDelay(isFatal: false), 0.5)
        XCTAssertEqual(policy.nextDelay(isFatal: false), 1)
        XCTAssertEqual(policy.nextDelay(isFatal: false), 2)
        XCTAssertEqual(policy.nextDelay(isFatal: false), 4)
        XCTAssertEqual(policy.nextDelay(isFatal: false), 8)
        XCTAssertNil(policy.nextDelay(isFatal: false), "上限を超えても張り直し続ける")

        policy.noteHealthy()
        XCTAssertEqual(policy.nextDelay(isFatal: false), 0.5, "安定した後も失敗回数が戻らない")
    }

    /// 認証・残高のエラーは張り直さない
    func testReconnectPolicyFatalErrors() {
        var policy = SonioxReconnectPolicy()
        XCTAssertNil(policy.nextDelay(isFatal: true))
        XCTAssertTrue(SonioxReconnectPolicy.isFatal(errorCode: 401))
        XCTAssertTrue(SonioxReconnectPolicy.isFatal(errorCode: 402))
        XCTAssertFalse(SonioxReconnectPolicy.isFatal(errorCode: 503))
        XCTAssertFalse(SonioxReconnectPolicy.isFatal(errorCode: nil))
    }

    // MARK: - 設定 JSON

    /// 翻訳モードだけ翻訳を付け、速さ重視のエンドポイント設定で送る
    func testConfigJSON() throws {
        guard #available(macOS 26.0, *) else { throw XCTSkip("ライブ字幕は macOS 26 以降") }
        let translate = try object(SonioxCaptionStream.configJSON(apiKey: "test-key", translates: true, language: "en"))
        XCTAssertEqual(translate["model"] as? String, "stt-rt-v5")
        XCTAssertEqual(translate["audio_format"] as? String, "pcm_s16le")
        XCTAssertEqual(translate["sample_rate"] as? Int, 16000)
        XCTAssertEqual(translate["num_channels"] as? Int, 1)
        XCTAssertEqual(translate["language_hints"] as? [String], ["en"])
        XCTAssertEqual(translate["enable_endpoint_detection"] as? Bool, true)
        XCTAssertEqual(translate["max_endpoint_delay_ms"] as? Int, 500)
        let translation = try XCTUnwrap(translate["translation"] as? [String: String])
        XCTAssertEqual(translation, ["type": "one_way", "target_language": "ja"])

        let transcribe = try object(SonioxCaptionStream.configJSON(apiKey: "test-key", translates: false, language: "ja"))
        XCTAssertNil(transcribe["translation"], "文字起こしモードで翻訳を頼んでいる（余計な処理）")
        XCTAssertEqual(transcribe["language_hints"] as? [String], ["ja"])
    }

    // MARK: - 補助

    private func response(_ tokens: [SonioxCaptionAssembler.Token]) -> SonioxCaptionAssembler.Response {
        .init(tokens: tokens, finished: false, errorCode: nil, errorMessage: nil)
    }

    private func src(_ text: String, final: Bool) -> SonioxCaptionAssembler.Token {
        .init(text: text, isFinal: final, isTranslation: false, awaitsTranslation: true)
    }

    private func tr(_ text: String, final: Bool) -> SonioxCaptionAssembler.Token {
        .init(text: text, isFinal: final, isTranslation: true)
    }

    private func endToken() -> SonioxCaptionAssembler.Token {
        .init(text: SonioxCaptionAssembler.endToken, isFinal: true, isTranslation: false)
    }

    private func object(_ json: String) throws -> [String: Any] {
        try XCTUnwrap(JSONSerialization.jsonObject(with: Data(json.utf8)) as? [String: Any])
    }
}
