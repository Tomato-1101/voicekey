//
//  LocalSttAndTranslateTests.swift
//  ローカル（Apple）文字起こしと「翻訳して入力」の純ロジックテスト
//
//  外部通信・音声モデル・Keychain には一切触れない（設定の解決規則と、
//  他バックエンドを巻き込んでいないことだけを機械的に確かめる）。
//

import XCTest
@testable import voicekey

final class LocalSttAndTranslateTests: XCTestCase {

    // MARK: - ローカル（Apple）バックエンド

    /// 選択肢に「ローカル（Apple）」が入り、既存の選択肢を消していないこと。
    func testAppleLocalIsSelectableAlongsideExisting() {
        let cases = Backend.selectableCases
        XCTAssertTrue(cases.contains(.deepgram), "既存の即時入力(Deepgram)を消してはいけない")
        XCTAssertTrue(cases.contains(.groq), "既存のスタンダード(Groq)を消してはいけない")
        XCTAssertTrue(cases.contains(.openaiLive), "既存の OpenAI ライブを消してはいけない")
        if #available(macOS 26.0, *) {
            XCTAssertTrue(cases.contains(.appleLocal), "macOS 26 以降ではローカルを選べる")
        } else {
            XCTAssertFalse(cases.contains(.appleLocal), "macOS 26 未満ではローカルを出さない")
        }
    }

    /// ローカルは API キーを使わない＝Keychain を引かずに必ず nil を返す。
    /// （他バックエンドのキー解決に副作用が無いことも同時に確かめる）
    func testAppleLocalNeverResolvesAPIKey() {
        XCTAssertNil(Keychain.apiKey(for: .appleLocal))
    }

    /// ローカルは速度全振りなので整形の既定は OFF（Deepgram と同じ扱い）。
    func testAppleLocalDefaultsToNoFormatting() {
        XCTAssertFalse(Backend.appleLocal.defaultFormatEnabled)
        // 既存バックエンドの既定を変えていないこと
        XCTAssertFalse(Backend.deepgram.defaultFormatEnabled)
        XCTAssertTrue(Backend.groq.defaultFormatEnabled)
    }

    /// personal の表示名にモデル名まで出る（何が動いているか隠さない方針）。
    func testAppleLocalDeveloperLabel() {
        XCTAssertEqual(Backend.appleLocal.developerLabel, "Apple オンデバイス音声認識")
    }

    /// 言語コード → 認識ロケールの写し（空はシステムの言語へ倒す）。
    @available(macOS 26.0, *)
    func testRecognitionLocaleMapping() {
        XCTAssertEqual(LocalSpeechTranscriber.recognitionLocale(for: "ja").identifier, "ja")
        XCTAssertEqual(LocalSpeechTranscriber.recognitionLocale(for: " en ").identifier, "en")
        XCTAssertEqual(
            LocalSpeechTranscriber.recognitionLocale(for: "").identifier,
            Locale.current.identifier,
            "空（自動判定）はシステムの言語で認識する"
        )
    }

    // MARK: - 翻訳して入力

    /// 出力言語は既定 en。未知の保存値は en へ倒す（選べない値が残らない）。
    func testTargetLanguageFallsBackToEnglish() {
        let defaults = UserDefaults.standard
        let original = defaults.string(forKey: DictationTranslation.Key.target)
        defer {
            if let original { defaults.set(original, forKey: DictationTranslation.Key.target) }
            else { defaults.removeObject(forKey: DictationTranslation.Key.target) }
        }

        defaults.removeObject(forKey: DictationTranslation.Key.target)
        XCTAssertEqual(DictationTranslation.targetLanguage, "en", "未設定なら英語")

        defaults.set("kl", forKey: DictationTranslation.Key.target)
        XCTAssertEqual(DictationTranslation.targetLanguage, "en", "選択肢に無い値は英語へ倒す")

        defaults.set("ko", forKey: DictationTranslation.Key.target)
        XCTAssertEqual(DictationTranslation.targetLanguage, "ko")
    }

    /// エンジンの既定は Apple（オンデバイス・無料）。壊れた保存値も Apple へ倒す。
    func testEngineDefaultsToApple() {
        let defaults = UserDefaults.standard
        let original = defaults.string(forKey: DictationTranslation.Key.engine)
        defer {
            if let original { defaults.set(original, forKey: DictationTranslation.Key.engine) }
            else { defaults.removeObject(forKey: DictationTranslation.Key.engine) }
        }

        defaults.removeObject(forKey: DictationTranslation.Key.engine)
        XCTAssertEqual(DictationTranslation.engine, .apple)

        defaults.set("gemini", forKey: DictationTranslation.Key.engine)
        XCTAssertEqual(DictationTranslation.engine, .apple, "選べないエンジン名は Apple へ倒す")
    }

    /// 課金を勝手に発生させない恒久方針: Gemini は「翻訳して入力」の選択肢に出さない。
    func testGeminiIsNotOfferedForDictation() {
        let names = DictationTranslationEngine.allCases.map(\.rawValue)
        XCTAssertEqual(names.sorted(), ["apple", "groq"])
    }

    /// トグル OFF が既定（勝手に翻訳して入力しない）。
    func testTranslationIsOffByDefault() {
        let defaults = UserDefaults.standard
        let original = defaults.object(forKey: DictationTranslation.Key.enabled)
        defer {
            if let original { defaults.set(original, forKey: DictationTranslation.Key.enabled) }
            else { defaults.removeObject(forKey: DictationTranslation.Key.enabled) }
        }
        defaults.removeObject(forKey: DictationTranslation.Key.enabled)
        XCTAssertFalse(DictationTranslation.isEnabled)
    }

    /// 話す言語と出力言語が同じなら、失敗扱いにせず原文をそのまま返す。
    /// （実機で ja→ja が指定され「翻訳できなかった」通知が毎回出た回帰を防ぐ）
    @available(macOS 26.0, *)
    func testSameLanguagePairReturnsSourceWithoutFailing() async {
        let translator = DictationTranslator(engine: .apple, sourceLanguage: "ja", targetLanguage: "ja")
        let result = await translator.translate("今日は良い天気ですね。")
        XCTAssertEqual(result.text, "今日は良い天気ですね。", "原文がそのまま最終テキストになる")
        XCTAssertTrue(result.didTranslate, "失敗扱いにしない（HUD の通知を出さない）")
        XCTAssertNil(result.failureReason)
    }

    /// Groq のシステムプロンプトに出力言語が入り、訳文だけを返させる指示になっていること。
    @available(macOS 26.0, *)
    func testGroqSystemPromptNamesTargetLanguage() {
        let prompt = DictationTranslator.groqSystemPrompt(targetCode: "en")
        XCTAssertTrue(prompt.contains("英語"), "出力言語が指示に入っていること")
        XCTAssertTrue(prompt.contains("訳文だけ"), "余計な前置きを禁じていること")
    }
}
