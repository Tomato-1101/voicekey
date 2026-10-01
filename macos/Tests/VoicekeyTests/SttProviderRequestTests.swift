//
//  SttProviderRequestTests.swift
//  2026-10-01 に追加・変更した文字起こしエンジン（Mac のみ）の送受信形式の単体テスト
//
//  - Soniox: 応答トークンの組み立て・接続時の設定 JSON・Float→Int16 変換・失敗時の文言
//  - OpenAI gpt-transcribe: multipart の形（json 応答・languages[]）と応答解析
//  - Microsoft MAI-Transcribe-2: URL の正規化・definition・応答解析・認証ヘッダ
//  - ElevenLabs scribe_v2: tag_audio_events=false を明示していること
//
//  すべてオフライン（リクエストを組み立てて中身を見るだけ・通信しない）。Keychain にも触れない。
//

import XCTest
@testable import voicekey

final class SttProviderRequestTests: XCTestCase {

    // MARK: - 共通ヘルパー

    /// multipart の本文（音声バイトは UTF-8 として読めないことがあるのでバイト列のまま扱う）
    private func body(_ request: URLRequest) -> Data {
        request.httpBody ?? Data()
    }

    /// 本文に文字列（UTF-8）がそのまま含まれるか
    private func has(_ body: Data, _ string: String) -> Bool {
        body.range(of: Data(string.utf8)) != nil
    }

    /// multipart の 1 フィールド（name と値）の並び
    private func field(_ name: String, _ value: String) -> String {
        "name=\"\(name)\"\r\n\r\n\(value)\r\n"
    }

    private let dummyAudio = Transcriber.EncodedAudio(
        data: Data([0x52, 0x49, 0x46, 0x46]), filename: "audio.wav", contentType: "audio/wav"
    )

    // MARK: - Soniox: トークンの組み立て

    private func apply(_ transcript: inout SonioxTranscript, _ json: String) -> SonioxTranscript.Outcome {
        transcript.apply(Data(json.utf8))
    }

    /// 確定トークンは到着順に足し、非確定トークンは応答ごとに置き換える
    func testSonioxFinalTokensAccumulateAndInterimReplaces() {
        var t = SonioxTranscript()
        XCTAssertEqual(apply(&t, #"{"tokens":[{"text":"こん","is_final":true},{"text":"にち","is_final":false}]}"#), .updated)
        XCTAssertEqual(t.finalText, "こん")
        XCTAssertEqual(t.interimText, "にち")

        // 次の応答: 前の非確定は捨てて、新しい確定・非確定で置き換える
        XCTAssertEqual(apply(&t, #"{"tokens":[{"text":"にちは","is_final":true},{"text":"世","is_final":false}]}"#), .updated)
        XCTAssertEqual(t.finalText, "こんにちは")
        XCTAssertEqual(t.interimText, "世")
        XCTAssertEqual(t.fullText, "こんにちは世")

        // 非確定が無い応答では途中文が空に戻る
        XCTAssertEqual(apply(&t, #"{"tokens":[{"text":"世界","is_final":true}]}"#), .updated)
        XCTAssertEqual(t.fullText, "こんにちは世界")
    }

    /// <fin> / <end> は制御用の印なので結果に含めない
    func testSonioxControlTokensExcluded() {
        var t = SonioxTranscript()
        _ = apply(&t, #"{"tokens":[{"text":"Hello","is_final":true},{"text":"<end>","is_final":true},{"text":" world","is_final":true},{"text":"<fin>","is_final":true}]}"#)
        XCTAssertEqual(t.finalText, "Hello world")
        XCTAssertEqual(t.interimText, "")
    }

    /// "finished": true で完了。同じ応答に載ったトークンも取り込んでから完了扱いにする
    func testSonioxFinishedAfterApplyingTokens() {
        var t = SonioxTranscript()
        _ = apply(&t, #"{"tokens":[{"text":"テスト","is_final":false}]}"#)
        XCTAssertEqual(apply(&t, #"{"tokens":[{"text":"テスト","is_final":true}],"finished":true}"#), .finished)
        XCTAssertEqual(t.fullText, "テスト")
    }

    /// エラー応答はコードと種別だけを返す（コードが数値でも文字列でも扱える）
    func testSonioxErrorOutcome() {
        var t = SonioxTranscript()
        XCTAssertEqual(
            apply(&t, #"{"error_code":401,"error_type":"unauthenticated","error_message":"Invalid API key"}"#),
            .error(code: "401", type: "unauthenticated")
        )
        var t2 = SonioxTranscript()
        XCTAssertEqual(apply(&t2, #"{"error_code":"bad_request"}"#), .error(code: "bad_request", type: nil))
    }

    /// 通常の応答に error_code: null / error_type: null が載ってもエラー扱いにしない
    /// （null を「コード無しのエラー」と読むと、正常な応答で接続を失敗扱いにしてしまう）
    func testSonioxNullErrorFieldsIgnored() {
        var t = SonioxTranscript()
        XCTAssertEqual(
            apply(&t, #"{"error_code":null,"error_type":null,"tokens":[{"text":"はい","is_final":true}]}"#),
            .updated
        )
        XCTAssertEqual(t.fullText, "はい")
        // コードだけ null・種別だけ有効のときは種別だけ持つ
        var t2 = SonioxTranscript()
        XCTAssertEqual(apply(&t2, #"{"error_code":null,"error_type":"internal"}"#), .error(code: nil, type: "internal"))
    }

    /// finalize への応答の <fin> は「ここまでの確定が出そろった」印。finished より先に来るので区別して返す
    func testSonioxFinTokenGivesFinalized() {
        var t = SonioxTranscript()
        XCTAssertEqual(
            apply(&t, #"{"tokens":[{"text":"送信","is_final":true},{"text":"<fin>","is_final":true}]}"#),
            .finalized
        )
        XCTAssertEqual(t.fullText, "送信")
        // <fin> 単独でも finalized（確定は増えていない）
        var t2 = SonioxTranscript()
        XCTAssertEqual(apply(&t2, #"{"tokens":[{"text":"<fin>","is_final":true}]}"#), .finalized)
        XCTAssertEqual(t2.fullText, "")
        // finished が同じ応答に載れば finished を優先する
        var t3 = SonioxTranscript()
        XCTAssertEqual(apply(&t3, #"{"tokens":[{"text":"<fin>","is_final":true}],"finished":true}"#), .finished)
    }

    /// 表示に影響しない応答・壊れた応答は無視する
    func testSonioxIgnoredOutcomes() {
        var t = SonioxTranscript()
        XCTAssertEqual(apply(&t, #"{"tokens":[]}"#), .ignored)
        XCTAssertEqual(apply(&t, "not json"), .ignored)
        XCTAssertEqual(t.fullText, "")
    }

    // MARK: - Soniox: 設定 JSON

    private func configObject(_ json: String) throws -> [String: Any] {
        try XCTUnwrap(JSONSerialization.jsonObject(with: Data(json.utf8)) as? [String: Any])
    }

    func testSonioxConfigJSON() throws {
        let obj = try configObject(SonioxLiveTranscriber.configJSON(
            apiKey: "test-key", model: "stt-rt-v5", language: "en", prompt: "  voicekey, Soniox  "
        ))
        XCTAssertEqual(obj["api_key"] as? String, "test-key")
        XCTAssertEqual(obj["model"] as? String, "stt-rt-v5")
        XCTAssertEqual(obj["audio_format"] as? String, "pcm_s16le")
        XCTAssertEqual(obj["sample_rate"] as? Int, 16000)
        XCTAssertEqual(obj["num_channels"] as? Int, 1)
        XCTAssertEqual(obj["language_hints"] as? [String], ["en"])
        XCTAssertEqual((obj["context"] as? [String: Any])?["text"] as? String, "voicekey, Soniox")
        // ヒント外の言語（英単語の混在など）も拾わせるため strict は送らない
        XCTAssertNil(obj["language_hints_strict"])
    }

    /// 言語が空（自動判定）なら language_hints 自体を付けない、プロンプトが空なら context も付けない
    func testSonioxConfigJSONDefaults() throws {
        let obj = try configObject(SonioxLiveTranscriber.configJSON(
            apiKey: "test-key", model: "stt-rt-v5", language: "", prompt: "   "
        ))
        XCTAssertNil(obj["language_hints"])
        XCTAssertNil(obj["context"])
    }

    /// 録音全体の再送で finish を待つ上限: 3 秒＋音声長の半分、最大 15 秒
    func testSonioxReplayFinishTimeout() {
        XCTAssertEqual(SonioxLiveTranscriber.replayFinishTimeout(audioSeconds: 0), 3, accuracy: 0.001)
        XCTAssertEqual(SonioxLiveTranscriber.replayFinishTimeout(audioSeconds: 10), 8, accuracy: 0.001)
        XCTAssertEqual(SonioxLiveTranscriber.replayFinishTimeout(audioSeconds: 600), 15, accuracy: 0.001)
    }

    // MARK: - Soniox: 失敗時の文言

    func testSonioxFailureMessages() {
        let invalid = Transcriber.sonioxFailureMessage(.error(code: "401", type: "unauthenticated"))
        XCTAssertTrue(invalid.contains("API キーが無効"), invalid)
        XCTAssertTrue(invalid.contains("設定 › API キー"), invalid)
        XCTAssertEqual(Transcriber.sonioxFailureMessage(.error(code: "403", type: nil)), invalid)
        XCTAssertTrue(Transcriber.sonioxFailureMessage(.error(code: "402", type: nil)).contains("残高"))
        XCTAssertTrue(Transcriber.sonioxFailureMessage(.error(code: "429", type: nil)).contains("上限"))
        let network = "Soniox に接続できませんでした（ネットワークを確認してください）"
        XCTAssertEqual(Transcriber.sonioxFailureMessage(.error(code: "500", type: nil)), network)
        XCTAssertEqual(Transcriber.sonioxFailureMessage(.error(code: nil, type: "internal")), network)
        XCTAssertEqual(Transcriber.sonioxFailureMessage(.disconnect), network)
        XCTAssertEqual(Transcriber.sonioxFailureMessage(.timeout), network)
    }

    /// キー未設定の案内は、どのエンジンのキーかと入力先（設定 › API キー）を示す
    func testMissingKeyMessagePointsToApiKeySettings() {
        for backend in [Backend.soniox, .azureMAI, .openaiLive, .openai, .elevenlabs, .groq] {
            let message = Transcriber.missingKeyMessage(for: backend)
            XCTAssertTrue(message.contains(backend.label), message)
            XCTAssertTrue(message.contains("設定 › API キー"), message)
        }
    }

    /// 設定 › API キー へ案内するのは Soniox のキー無効（401/403）だけ。残高・上限・通信断は案内しない
    func testSonioxInvalidKeyFailureDetection() {
        XCTAssertTrue(Transcriber.isInvalidKeyFailure(.error(code: "401", type: "unauthenticated")))
        XCTAssertTrue(Transcriber.isInvalidKeyFailure(.error(code: "403", type: nil)))
        XCTAssertFalse(Transcriber.isInvalidKeyFailure(.error(code: "402", type: nil)))
        XCTAssertFalse(Transcriber.isInvalidKeyFailure(.error(code: "429", type: nil)))
        XCTAssertFalse(Transcriber.isInvalidKeyFailure(.error(code: nil, type: "internal")))
        XCTAssertFalse(Transcriber.isInvalidKeyFailure(.disconnect))
        XCTAssertFalse(Transcriber.isInvalidKeyFailure(.timeout))
    }

    // MARK: - Soniox: Float → Int16 LE

    func testSonioxPCM16() {
        let data = SonioxLiveTranscriber.pcm16([0, 1.0, -1.0, 2.0, -2.0, 0.5])
        XCTAssertEqual(data.count, 12)
        let values: [Int16] = stride(from: 0, to: data.count, by: 2).map { i in
            Int16(littleEndian: Int16(bitPattern: UInt16(data[i]) | UInt16(data[i + 1]) << 8))
        }
        // 範囲外はクリップする（ラップアラウンドで轟音にしない）
        XCTAssertEqual(values, [0, 32767, -32767, 32767, -32767, 16383])
        // リトルエンディアン（下位バイトが先）
        XCTAssertEqual(Array(data[2...3]), [0xFF, 0x7F])
    }

    // MARK: - OpenAI gpt-transcribe

    func testGptTranscribeMultipart() {
        let t = Transcriber(backend: .openai, model: "gpt-transcribe", language: "ja", prompt: "")
        let request = t.openAIRequest(audio: dummyAudio, apiKey: "test-key", gptTranscribeFormat: t.usesGptTranscribeFormat)
        let text = body(request)
        XCTAssertEqual(request.url?.absoluteString, "https://api.openai.com/v1/audio/transcriptions")
        XCTAssertEqual(request.httpMethod, "POST")
        XCTAssertTrue(has(text, field("model", "gpt-transcribe")))
        XCTAssertTrue(has(text, field("response_format", "json")))
        XCTAssertTrue(has(text, field("languages[]", "ja")))
        // 単数の language と両方は送らない。temperature も送らない
        XCTAssertFalse(has(text, "name=\"language\""))
        XCTAssertFalse(has(text, "name=\"temperature\""))
        // Whisper 用の数字の例文は送らない（gpt-transcribe では例文が出力に漏れうる）。プロンプト空なら prompt ごと送らない
        XCTAssertFalse(has(text, "name=\"prompt\""))
        XCTAssertFalse(has(text, Transcriber.numeralStyleHint))
        XCTAssertTrue(has(text, "name=\"file\"; filename=\"audio.wav\""))
    }

    /// ユーザーのプロンプトがあれば、それだけを prompt として送る
    func testGptTranscribeSendsOnlyUserPrompt() {
        let t = Transcriber(backend: .openai, model: "gpt-transcribe", language: "ja", prompt: "  voicekey, Soniox \n")
        let text = body(t.openAIRequest(audio: dummyAudio, apiKey: "test-key", gptTranscribeFormat: t.usesGptTranscribeFormat))
        XCTAssertTrue(has(text, field("prompt", "voicekey, Soniox")))
        XCTAssertFalse(has(text, Transcriber.numeralStyleHint))
    }

    /// 言語が空なら languages[] を送らない（自動判定）
    func testGptTranscribeWithoutLanguage() {
        let t = Transcriber(backend: .openai, model: "gpt-transcribe", language: "", prompt: "")
        let text = body(t.openAIRequest(audio: dummyAudio, apiKey: "test-key", gptTranscribeFormat: t.usesGptTranscribeFormat))
        XCTAssertFalse(has(text, "languages[]"))
        XCTAssertFalse(has(text, "name=\"language\""))
    }

    /// OpenAI ライブの REST フォールバックも gpt-transcribe の形で送る
    func testOpenAILiveFallbackUsesGptTranscribeFormat() {
        let t = Transcriber(backend: .openaiLive, model: "gpt-live-transcribe", language: "ja", prompt: "")
        let text = body(t.openAIRequest(audio: dummyAudio, apiKey: "test-key", gptTranscribeFormat: t.usesGptTranscribeFormat))
        XCTAssertTrue(has(text, field("model", "gpt-transcribe")))
        XCTAssertTrue(has(text, field("response_format", "json")))
        XCTAssertTrue(has(text, field("languages[]", "ja")))
    }

    /// Groq（Whisper）は従来どおり text 応答・単数 language
    func testGroqKeepsWhisperFormat() throws {
        let t = Transcriber(backend: .groq, model: "whisper-large-v3-turbo", language: "ja", prompt: "")
        let text = body(t.openAIRequest(audio: dummyAudio, apiKey: "test-key", gptTranscribeFormat: t.usesGptTranscribeFormat))
        XCTAssertTrue(has(text, field("response_format", "text")))
        XCTAssertTrue(has(text, field("language", "ja")))
        XCTAssertTrue(has(text, field("temperature", "0")))
        XCTAssertFalse(has(text, "languages[]"))
        XCTAssertEqual(try t.parseResponse(Data("こんにちは\n".utf8), gptTranscribeFormat: t.usesGptTranscribeFormat), "こんにちは\n")
    }

    func testGptTranscribeParsesJSON() throws {
        let t = Transcriber(backend: .openai, model: "gpt-transcribe", language: "ja", prompt: "")
        XCTAssertEqual(try t.parseResponse(Data(#"{"text":"こんにちは"}"#.utf8), gptTranscribeFormat: t.usesGptTranscribeFormat), "こんにちは")
        XCTAssertThrowsError(try t.parseResponse(Data("plain".utf8), gptTranscribeFormat: t.usesGptTranscribeFormat))
    }

    // MARK: - Microsoft MAI-Transcribe-2

    func testAzureTranscribeURLNormalizesEndpoint() {
        let expected = "https://example-res.cognitiveservices.azure.com"
            + "/speechtotext/transcriptions:transcribe?api-version=2025-10-15"
        for endpoint in [
            "https://example-res.cognitiveservices.azure.com",
            "https://example-res.cognitiveservices.azure.com/",
            "  https://example-res.cognitiveservices.azure.com//  \n",
        ] {
            XCTAssertEqual(Transcriber.azureTranscribeURL(endpoint: endpoint)?.absoluteString, expected, endpoint)
        }
        // ポータルからパス・クエリごとコピーした値、scheme の無い値も同じ URL になる
        for endpoint in [
            "https://example-res.cognitiveservices.azure.com/speechtotext/transcriptions:transcribe?api-version=2024-11-15",
            "https://example-res.cognitiveservices.azure.com/?foo=bar#frag",
            "example-res.cognitiveservices.azure.com",
            "example-res.cognitiveservices.azure.com/some/path",
        ] {
            XCTAssertEqual(Transcriber.azureTranscribeURL(endpoint: endpoint)?.absoluteString, expected, endpoint)
        }
        // port は残す
        XCTAssertEqual(
            Transcriber.azureTranscribeURL(endpoint: "http://localhost:8080/x?y=1")?.absoluteString,
            "http://localhost:8080/speechtotext/transcriptions:transcribe?api-version=2025-10-15"
        )
        XCTAssertNil(Transcriber.azureTranscribeURL(endpoint: nil))
        XCTAssertNil(Transcriber.azureTranscribeURL(endpoint: "https://"))
        XCTAssertNil(Transcriber.azureTranscribeURL(endpoint: "   "))
        XCTAssertNil(Transcriber.azureTranscribeURL(endpoint: "not a url"))
    }

    func testAzureDefinitionJSON() throws {
        let withLang = try configObject(Transcriber.azureDefinitionJSON(model: "MAI-Transcribe-2", language: "ja"))
        let enhanced = try XCTUnwrap(withLang["enhancedMode"] as? [String: Any])
        XCTAssertEqual(enhanced["enabled"] as? Bool, true)
        XCTAssertEqual(enhanced["model"] as? String, "MAI-Transcribe-2")
        XCTAssertEqual(withLang["locales"] as? [String], ["ja"])
        XCTAssertNil(withLang["modelOptions"])

        // 言語が空なら locales を付けない（自動判定）
        let noLang = try configObject(Transcriber.azureDefinitionJSON(model: "MAI-Transcribe-2", language: ""))
        XCTAssertNil(noLang["locales"])
    }

    func testAzureRequest() throws {
        let t = Transcriber(backend: .azureMAI, model: "MAI-Transcribe-2", language: "ja", prompt: "")
        let url = try XCTUnwrap(Transcriber.azureTranscribeURL(endpoint: "https://example-res.cognitiveservices.azure.com/"))
        let request = t.azureRequest(url: url, wav: Data([0x52, 0x49, 0x46, 0x46]), apiKey: "test-key")
        XCTAssertEqual(request.httpMethod, "POST")
        XCTAssertEqual(request.url, url)
        XCTAssertEqual(request.value(forHTTPHeaderField: "Ocp-Apim-Subscription-Key"), "test-key")
        XCTAssertNil(request.value(forHTTPHeaderField: "Authorization"))
        XCTAssertTrue(request.value(forHTTPHeaderField: "Content-Type")?.hasPrefix("multipart/form-data; boundary=") == true)
        let text = body(request)
        XCTAssertTrue(has(text, "name=\"audio\"; filename=\"audio.wav\"\r\nContent-Type: audio/wav"))
        XCTAssertTrue(has(text, field("definition", Transcriber.azureDefinitionJSON(model: "MAI-Transcribe-2", language: "ja"))))
    }

    func testAzureParseResponse() throws {
        // combinedPhrases[0].text を優先
        XCTAssertEqual(
            Transcriber.parseAzureResponse(Data(#"{"combinedPhrases":[{"text":"こんにちは世界"}],"phrases":[{"text":"こんにちは"},{"text":"世界"}]}"#.utf8)),
            "こんにちは世界"
        )
        // combinedPhrases が無ければ phrases を連結
        XCTAssertEqual(
            Transcriber.parseAzureResponse(Data(#"{"phrases":[{"text":"Hello"},{"text":"world"}]}"#.utf8)),
            "Hello world"
        )
        // 無音（どちらも空）は空文字
        XCTAssertEqual(Transcriber.parseAzureResponse(Data(#"{"combinedPhrases":[],"phrases":[]}"#.utf8)), "")
        XCTAssertEqual(Transcriber.parseAzureResponse(Data(#"{"combinedPhrases":[]}"#.utf8)), "")
        // 形が違えば解析失敗
        XCTAssertNil(Transcriber.parseAzureResponse(Data(#"{"error":{"code":"x"}}"#.utf8)))
        XCTAssertNil(Transcriber.parseAzureResponse(Data("oops".utf8)))

        // Transcriber 経由でも同じ解析を使い、失敗は例外になる
        let t = Transcriber(backend: .azureMAI, model: "MAI-Transcribe-2", language: "ja", prompt: "")
        XCTAssertEqual(try t.parseResponse(Data(#"{"combinedPhrases":[{"text":"テスト"}]}"#.utf8), gptTranscribeFormat: t.usesGptTranscribeFormat), "テスト")
        XCTAssertThrowsError(try t.parseResponse(Data("oops".utf8), gptTranscribeFormat: t.usesGptTranscribeFormat))
    }

    // MARK: - ElevenLabs scribe_v2

    /// scribe_v2 は音声イベントタグを既定で付けるので、false を明示して送る
    func testScribeV2DisablesAudioEventTags() {
        let t = Transcriber(backend: .elevenlabs, model: "scribe_v2", language: "ja", prompt: "")
        let request = t.elevenLabsRequest(audio: dummyAudio, apiKey: "test-key")
        let text = body(request)
        XCTAssertEqual(request.url?.absoluteString, "https://api.elevenlabs.io/v1/speech-to-text")
        XCTAssertEqual(request.value(forHTTPHeaderField: "xi-api-key"), "test-key")
        XCTAssertTrue(has(text, field("model_id", "scribe_v2")))
        XCTAssertTrue(has(text, field("tag_audio_events", "false")))
        XCTAssertTrue(has(text, field("language_code", "ja")))
    }
}
