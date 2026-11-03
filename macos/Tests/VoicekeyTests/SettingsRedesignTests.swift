//
//  SettingsRedesignTests.swift
//  設定画面の作り直し（2026-10-19）の純ロジック部分の単体テスト
//
//  - 文字起こしエンジン＋モデルを 1 つのポップアップにした選択肢の並び・表示名
//    （一覧外の保存値が消えない／Apple ローカルはエンジン名だけ、の退行は見た目だけでは気付きにくい）
//  - 整形プロンプトのひな形（標準は既定文そのもの・ほかは共通ルールを保ったまま末尾だけ差し替え）
//  - 新しい設定タブの id が既存と重ならず、保存済みの最後のタブの復元で壊れないこと
//

import XCTest
@testable import voicekey

@MainActor
final class SettingsRedesignTests: XCTestCase {

    // MARK: - エンジン＋モデルのポップアップ

    /// 選べるエンジンのモデルはすべて、ちょうど 1 回ずつポップアップに出る
    func testEveryKnownModelAppearsOnce() {
        let groups = EngineModelCatalog.groups(current: EngineModelChoice(backend: .soniox, model: "stt-rt-v5"))
        let ids = groups.flatMap { $0.options.map(\.id) }
        for backend in Backend.selectableCases {
            for model in backend.knownModels {
                let choice = EngineModelChoice(backend: backend, model: model)
                XCTAssertEqual(ids.filter { $0 == choice }.count, 1, "\(backend.rawValue)/\(model) がちょうど 1 回出ること")
            }
        }
    }

    /// 同じ提供元のエンジン（OpenAI の通常とライブ）は 1 つの見出しにまとまる
    func testSameProviderIsGroupedUnderOneHeading() {
        let groups = EngineModelCatalog.groups(
            current: EngineModelChoice(backend: .openai, model: "gpt-transcribe"),
            selectable: [.soniox, .openai, .openaiLive, .groq]
        )
        XCTAssertEqual(groups.count, 3)
        let openAI = groups.first { $0.title == "OpenAI" }
        XCTAssertEqual(openAI?.options.map(\.id.backend), [.openai, .openaiLive])
        // 見出しの下はモデル名だけ（提供元は見出しにある）。ライブは見分けが付くように印を付ける
        XCTAssertEqual(openAI?.options.map(\.label), ["gpt-transcribe", "gpt-live-transcribe（ライブ）"])
    }

    /// 選択肢が 1 つの提供元は見出しを付けず「提供元 · モデル」の 1 項目にする
    func testSingleOptionProviderHasNoHeading() {
        let groups = EngineModelCatalog.groups(
            current: EngineModelChoice(backend: .soniox, model: "stt-rt-v5"),
            selectable: [.soniox]
        )
        XCTAssertEqual(groups.count, 1)
        XCTAssertNil(groups[0].title)
        XCTAssertEqual(groups[0].options.map(\.label), ["Soniox · stt-rt-v5"])
    }

    /// 複数モデルのエンジンは既定モデルに「（推奨）」が付く
    func testRecommendedModelIsMarked() {
        let groups = EngineModelCatalog.groups(
            current: EngineModelChoice(backend: .elevenlabs, model: "scribe_v2"),
            selectable: [.elevenlabs]
        )
        XCTAssertEqual(groups.first?.title, "ElevenLabs")
        XCTAssertEqual(groups.first?.options.map(\.label), ["scribe_v2（推奨）", "scribe_v1"])
    }

    /// 一覧に無い保存済みモデル（自由入力・旧モデル）も、選択状態のまま見えるように残る
    func testCustomSavedModelStaysVisible() {
        let custom = EngineModelChoice(backend: .soniox, model: "stt-rt-preview")
        let groups = EngineModelCatalog.groups(current: custom, selectable: [.soniox, .groq])
        let ids = groups.flatMap { $0.options.map(\.id) }
        XCTAssertTrue(ids.contains(custom))
        // 2 つになった Soniox は見出し付きになる
        XCTAssertEqual(groups.first?.title, "Soniox")
    }

    /// 閉じたポップアップの表示は「実プロバイダー名 · モデル名」。Apple ローカルはエンジン名だけ
    func testClosedTitle() {
        XCTAssertEqual(EngineModelCatalog.title(EngineModelChoice(backend: .soniox, model: "stt-rt-v5")),
                       "Soniox · stt-rt-v5")
        XCTAssertEqual(EngineModelCatalog.title(EngineModelChoice(backend: .elevenlabs, model: "scribe_v1")),
                       "ElevenLabs · scribe_v1")
        XCTAssertEqual(EngineModelCatalog.title(EngineModelChoice(backend: .appleLocal, model: "オンデバイス音声認識")),
                       Backend.appleLocal.label)
    }

    // MARK: - 整形プロンプトのひな形

    /// 標準のひな形は既定文そのもの（選んでも保存値は空＝最新の既定文を使い続ける）
    func testStandardTemplateIsDefaultPrompt() {
        XCTAssertEqual(FormatPromptTemplates.all.first?.id, "standard")
        XCTAssertEqual(FormatPromptTemplates.all.first?.prompt, TextFormatter.defaultPrompt)
        XCTAssertEqual(FormatPromptTemplates.matching(TextFormatter.defaultPrompt + "\n")?.id, "standard")
    }

    /// ほかのひな形も既定文の共通ルールを保ち、id はサーバーの preset_id と同じ 4 種
    func testTemplatesKeepCommonRules() {
        XCTAssertEqual(FormatPromptTemplates.all.map(\.id), ["standard", "punctuation", "clean", "bullets"])
        XCTAssertTrue(TextFormatter.defaultPrompt.contains(FormatPromptTemplates.presetMarker),
                      "既定文に差し替えの目印が無いと、ひな形が既定文の丸写しになってしまう")
        for template in FormatPromptTemplates.all {
            XCTAssertTrue(template.prompt.hasPrefix(FormatPromptTemplates.commonPart), template.id)
            XCTAssertEqual(FormatPromptTemplates.matching(template.prompt)?.id, template.id)
        }
        XCTAssertNil(FormatPromptTemplates.matching("自分で書いた指示"))
    }

    // MARK: - 設定タブ

    /// タブの id は重ならない（保存済みの最後のタブを数値で復元するため）
    func testSettingsTabIDsAreUnique() {
        let ids = MainWindowView.settingsTabIDs
        XCTAssertEqual(Set(ids).count, ids.count)
        XCTAssertTrue(ids.contains(MainWindowView.TabID.prompt))
        XCTAssertTrue(ids.contains(MainWindowView.TabID.inputBasics))
        XCTAssertTrue(ids.contains(MainWindowView.TabID.general))
    }

    /// プロンプトのタブは「音声入力」の中で、録音キー 2 のすぐ後ろ
    func testPromptTabFollowsSlot2() {
        let items = MainWindowView.settingsNavItems
        guard let slot2 = items.firstIndex(where: { $0.id == MainWindowView.TabID.slot2 }) else {
            return XCTFail("録音キー 2 のタブが無い")
        }
        XCTAssertEqual(items[slot2 + 1].id, MainWindowView.TabID.prompt)
        XCTAssertEqual(items[slot2 + 1].section, "音声入力")
    }

    /// 保存済みの最後のタブが新しいタブでも、そのまま復元できる
    func testNewTabsRestore() {
        let ids = MainWindowView.settingsTabIDs
        XCTAssertEqual(MainWindowScreen.sanitizedTab(MainWindowView.TabID.prompt, validTabs: ids), MainWindowView.TabID.prompt)
        XCTAssertEqual(MainWindowScreen.sanitizedTab(MainWindowView.TabID.inputBasics, validTabs: ids),
                       MainWindowView.TabID.inputBasics)
    }
}
