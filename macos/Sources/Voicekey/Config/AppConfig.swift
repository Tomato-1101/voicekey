//
//  AppConfig.swift
//  設定モデルと永続化（UserDefaults）
//
//  Python 版は settings.yaml + ホットリロードだったが、Mac ネイティブ版は
//  UserDefaults に永続化し、設定 UI から直接 ConfigStore を更新する
//  （ファイル監視そのものが不要になる）。
//

import Combine
import Foundation

/// ホットキーの動作モード
enum HotkeyMode: String, Codable, CaseIterable, Identifiable {
    /// 押している間だけ録音
    case hold
    /// 1 回押して開始、もう 1 回押して停止
    case toggle

    var id: String { rawValue }

    var label: String {
        switch self {
        case .hold: return "押している間だけ"
        case .toggle: return "押すたびに開始・停止"
        }
    }
}

/// 文字起こしバックエンド
enum Backend: String, Codable, CaseIterable, Identifiable {
    case openai
    case groq
    case elevenlabs
    case deepgram

    var id: String { rawValue }

    /// 設定 UI に出すバックエンド名（製品版＝release ブランチ）。
    /// ユーザーが選べる文字起こしモードは 2 択:
    /// - リアルタイム＝Deepgram（nova-3・短命 JWT 直叩き＋ストリーミング。話しながらライブ表示）
    /// - スタンダード＝Groq（whisper-large-v3-turbo・プロキシ経由。録音後にきれいに整形。既定）
    /// ElevenLabs（scribe_v1）は選択肢から外し、スタンダードのハンズフリー録音時に内部でのみ
    /// 使う（長時間録音の精度対策）。openai は開発用のみ。
    var label: String {
        switch self {
        case .deepgram: return "リアルタイム"
        case .groq: return "スタンダード"
        // elevenlabs は選択肢外。スタンダード(groq)のハンズフリー録音時に内部でのみ使う名前で、
        // 計測ログ・エラーメッセージに出る（UI のピッカーには出さない）。
        case .elevenlabs: return "スタンダード（ハンズフリー）"
        // openai は選択肢外（開発用のみ）。elevenlabs との表示重複バグを解消する。
        case .openai: return "OpenAI（開発用）"
        }
    }

    /// 製品版で文字起こしバックエンドとして選べる 2 つ（表示順）。
    /// リアルタイム=Deepgram（ストリーミング）/ スタンダード=Groq（既定・普通入力）。
    /// enum の case（elevenlabs/openai）は保存値の decode 互換と EL の内部利用のため残す
    /// （「選べる集合」だけを縮める設計）。
    static var selectableCases: [Backend] { [.deepgram, .groq] }

    /// 提供元名（API キー欄でどのキーかを示すためだけに使う。配布版では
    /// API キータブ自体を隠すので、開発時にしか表示されない）
    var providerName: String {
        switch self {
        case .openai: return "OpenAI"
        case .groq: return "Groq"
        case .elevenlabs: return "ElevenLabs"
        case .deepgram: return "Deepgram"
        }
    }

    /// 既知のモデル一覧（設定 UI の候補。自由入力も可）。
    /// 先頭が既定＝推奨（ベンチ実測 2026-06-10 に基づく。Windows 版と順序を一致させる）。
    var knownModels: [String] {
        switch self {
        // mini が高速で短文精度は同等（CER 2.7%）
        case .openai: return ["gpt-4o-mini-transcribe", "gpt-4o-transcribe"]
        // turbo が REST 最速（330ms〜）で精度も良好
        case .groq: return ["whisper-large-v3-turbo", "whisper-large-v3"]
        // scribe_v1 が日本語最高精度（scribe_v2 は最新だが日本語長文で後退）
        case .elevenlabs: return ["scribe_v1", "scribe_v2", "scribe_v1_experimental"]
        // nova-3 がベンチで速度・精度とも最良（ストリーミング既定）。ja は多言語モードで対応
        case .deepgram: return ["nova-3", "nova-2"]
        }
    }

    /// 既定モデル（＝設定 UI で「（推奨）」表記するモデル）
    var defaultModel: String { knownModels[0] }

    /// モード別のテキスト整形の既定 ON/OFF。
    /// リアルタイム(deepgram)は速度全振りのため既定 OFF（トグルで ON は可能）、
    /// スタンダード(groq)ほかは録音後にきれいに整形するため既定 ON。
    /// 設定 UI でモードを切り替えたときに整形トグルをこの既定へ追従させる。
    var defaultFormatEnabled: Bool {
        switch self {
        case .deepgram: return false
        case .groq, .elevenlabs, .openai: return true
        }
    }
}

/// ホットキースロット 1 つ分の設定
struct SlotConfig: Codable, Equatable {
    /// ホットキーを構成するキートークン（例: ["cmd_r"], ["ctrl_l", "space"]）
    var hotkey: [String]
    var mode: HotkeyMode
    var backend: Backend
    var model: String
    var prompt: String
    /// 貼り付け前に LLM でテキスト整形するか（製品版は既定オン＝裏で整形）
    var formatEnabled: Bool = true

    /// 人間が読める表記（例: "右⌘"、"⌃+Space"）
    var hotkeyLabel: String {
        hotkey.isEmpty ? "未設定" : hotkey.map { KeyToken.displayName($0) }.joined(separator: "+")
    }
}

extension SlotConfig {
    /// 既存ユーザーの保存済み JSON には整形フィールドが無いため、
    /// 全フィールドを decodeIfPresent + 既定値で読む（無いと decode 失敗で
    /// ホットキー設定がリセットされる）。本体宣言内に書くと memberwise init が
    /// 消えるため、必ず extension に置く。Encodable と CodingKeys は合成に任せる。
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        hotkey = try c.decodeIfPresent([String].self, forKey: .hotkey) ?? []
        mode = try c.decodeIfPresent(HotkeyMode.self, forKey: .mode) ?? .hold
        // 製品版で選べるのは deepgram（リアルタイム）/ groq（スタンダード）の 2 択。
        // 選択肢に無い保存値（旧「高精度」= elevenlabs・openai）は groq（スタンダード・既定）へ移行する。
        // deepgram は選択肢に残るため、保存済み deepgram はそのまま維持される。
        // enum の case は decode 互換と EL の内部利用のため削らない（「選べる集合」だけを絞る）。
        let decoded = try c.decodeIfPresent(Backend.self, forKey: .backend) ?? .groq
        let migrated = Backend.selectableCases.contains(decoded) ? decoded : .groq
        backend = migrated
        let decodedModel = try c.decodeIfPresent(String.self, forKey: .model) ?? ""
        // バックエンドを移行した（または保存モデルが当該バックエンドのものでない）場合は
        // そのバックエンドの推奨モデルに揃える（製品版はモデル非選択で固定なので実害はないが整合のため）
        model = migrated.knownModels.contains(decodedModel) ? decodedModel : migrated.defaultModel
        prompt = try c.decodeIfPresent(String.self, forKey: .prompt) ?? ""
        // 製品版は整形を既定オン（裏で整形）。保存値が無ければ true
        formatEnabled = try c.decodeIfPresent(Bool.self, forKey: .formatEnabled) ?? true
    }
}

/// ユーザー辞書の確定置換ルール 1 件。
/// 文字起こし・整形が終わった最終テキストに対し、貼り付け直前で
/// `from` を `to` に機械的に置換する（API を通さないので遅延ゼロ）。
struct ReplacementRule: Codable, Equatable, Identifiable {
    /// SwiftUI の List/ForEach 用の安定 ID（永続化もする）
    var id: UUID = UUID()
    /// 置換元（誤変換されがちな表記）
    var from: String = ""
    /// 置換先（正しい表記）
    var to: String = ""
    /// この行を適用するか（一時的に無効化できる）
    var enabled: Bool = true
}

extension ReplacementRule {
    /// 既存データに id が無くても decode できるようにする（無いと配列ごと読み込み失敗する）
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decodeIfPresent(UUID.self, forKey: .id) ?? UUID()
        from = try c.decodeIfPresent(String.self, forKey: .from) ?? ""
        to = try c.decodeIfPresent(String.self, forKey: .to) ?? ""
        enabled = try c.decodeIfPresent(Bool.self, forKey: .enabled) ?? true
    }
}

/// アプリ設定の永続化ストア。
/// すべての設定変更は @Published プロパティ経由で行い、変更時に自動保存される。
@MainActor
final class ConfigStore: ObservableObject {

    /// スロット 1＝普通入力（既定: 右⌘ 押している間 → Groq「スタンダード」whisper-large-v3-turbo）
    @Published var slot1: SlotConfig
    /// スロット 2＝ハンズフリー（既定: 右⌥ トグル → Groq「スタンダード」。toggle 録音では内部で EL scribe_v1 に自動切替）
    @Published var slot2: SlotConfig
    /// 言語コード（"ja" など。空なら API 側の自動判定）
    @Published var language: String
    /// VAD（発話検出）による無音スキップ・トリミングを行うか
    @Published var vadEnabled: Bool
    /// 録音 HUD を表示するか
    @Published var hudEnabled: Bool
    /// Deepgram でリアルタイムストリーミング（ライブ字幕）を使うか
    @Published var streamingEnabled: Bool
    /// ダブルタップ自動 Enter: テキスト挿入から Enter 送信までの待機（ミリ秒）
    @Published var autoEnterDelayMs: Int
    /// 録音に使う入力デバイスの UID（空ならシステム既定マイク）
    @Published var inputDeviceUID: String
    /// テキスト整形に使う Groq のモデル名（全スロット共通）
    @Published var formatModel: String
    /// 整形で LLM に渡すプロンプト本文（全スロット共通・編集可）
    @Published var autoFormatPrompt: String
    /// ハンズフリー切替キー（このキー＋スロットキーでトグル録音。空＝無効）
    @Published var handsfreeKey: [String]
    /// 長文を無音区間で分割し API へ並列送信する（既定オン。Deepgram ストリーミングは対象外）
    @Published var splitParallelEnabled: Bool
    /// ユーザー辞書の確定置換ルール（貼り付け直前に from→to を機械置換する）
    @Published var replacements: [ReplacementRule]

    private var cancellables: Set<AnyCancellable> = []
    private let defaults: UserDefaults

    private enum Keys {
        static let slot1 = "slot1"
        static let slot2 = "slot2"
        static let language = "language"
        static let vadEnabled = "vadEnabled"
        static let hudEnabled = "hudEnabled"
        static let streamingEnabled = "streamingEnabled"
        static let autoEnterDelayMs = "autoEnterDelayMs"
        static let inputDeviceUID = "inputDeviceUID"
        static let formatModel = "formatModel"
        static let autoFormatPrompt = "autoFormatPrompt"
        static let handsfreeKey = "handsfreeKey"
        static let splitParallelEnabled = "splitParallelEnabled"
        static let replacements = "replacements"
        /// モード別整形既定の一回限りマイグレーション済みフラグ（v1.8）
        static let didMigrateModeDefaultsV18 = "didMigrateModeDefaultsV18"
    }

    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults

        // 製品版の既定: スロット1=普通入力(右⌘・押している間・Groq「スタンダード」whisper-large-v3-turbo)
        //             スロット2=ハンズフリー(右⌥・トグル・同じく Groq「スタンダード」)
        //             ハンズフリー(toggle)録音では groq を内部で ElevenLabs(scribe_v1) に自動切替する。
        //             リアルタイム(Deepgram)は選択肢として選べる（ストリーミング・既定では未使用）
        slot1 = Self.loadSlot(defaults, key: Keys.slot1) ?? SlotConfig(
            hotkey: ["cmd_r"], mode: .hold, backend: .groq,
            model: Backend.groq.defaultModel, prompt: ""
        )
        slot2 = Self.loadSlot(defaults, key: Keys.slot2) ?? SlotConfig(
            hotkey: ["alt_r"], mode: .toggle, backend: .groq,
            model: Backend.groq.defaultModel, prompt: ""
        )
        language = defaults.string(forKey: Keys.language) ?? "ja"
        // VAD・HUD・ストリーミング・長文分割は常時 ON に固定（設定 UI から撤去）。
        // 保存済みの値は無視し true 固定にする（ユーザーが OFF にする手段は持たせない）
        vadEnabled = true
        hudEnabled = true
        streamingEnabled = true
        autoEnterDelayMs = defaults.object(forKey: Keys.autoEnterDelayMs) as? Int ?? 50
        inputDeviceUID = defaults.string(forKey: Keys.inputDeviceUID) ?? ""
        formatModel = defaults.string(forKey: Keys.formatModel) ?? "llama-3.1-8b-instant"
        // 既定値はそのまま編集できるよう実テキストを初期表示する（空欄なら実行時に既定へフォールバック）。
        // 保存されるのはユーザーが編集した場合のみ（下の保存処理参照）なので、
        // 未編集なら常に最新の既定文が使われる
        let storedAutoPrompt = defaults.string(forKey: Keys.autoFormatPrompt) ?? ""
        autoFormatPrompt = storedAutoPrompt.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            ? TextFormatter.defaultPrompt
            : storedAutoPrompt
        handsfreeKey = (defaults.array(forKey: Keys.handsfreeKey) as? [String]) ?? []
        splitParallelEnabled = true
        replacements = Self.loadReplacements(defaults) ?? []

        // 一回限りのマイグレーション（モード別整形既定の導入・v1.8）。
        // 既存ユーザーは formatEnabled=true が明示保存されているため、リアルタイム(deepgram)
        // スロットの整形を初回だけ既定 OFF（速度全振り）へ矯正する。以後は二度と触らないので、
        // ユーザーが deepgram で整形 ON にしたらそれを尊重する。decode 内ではやらない
        // （decode は保存値をそのまま復元する役目で、一回限りの副作用を持たせない）。
        if !defaults.bool(forKey: Keys.didMigrateModeDefaultsV18) {
            if slot1.backend == .deepgram { slot1.formatEnabled = false }
            if slot2.backend == .deepgram { slot2.formatEnabled = false }
            saveSlot(slot1, key: Keys.slot1)
            saveSlot(slot2, key: Keys.slot2)
            defaults.set(true, forKey: Keys.didMigrateModeDefaultsV18)
        }

        // 変更を自動保存（起動直後の初期代入は上で完了しているため安全）
        $slot1.dropFirst().sink { [weak self] in self?.saveSlot($0, key: Keys.slot1) }.store(in: &cancellables)
        $slot2.dropFirst().sink { [weak self] in self?.saveSlot($0, key: Keys.slot2) }.store(in: &cancellables)
        $language.dropFirst().sink { [weak self] in self?.defaults.set($0, forKey: Keys.language) }.store(in: &cancellables)
        $vadEnabled.dropFirst().sink { [weak self] in self?.defaults.set($0, forKey: Keys.vadEnabled) }.store(in: &cancellables)
        $hudEnabled.dropFirst().sink { [weak self] in self?.defaults.set($0, forKey: Keys.hudEnabled) }.store(in: &cancellables)
        $streamingEnabled.dropFirst().sink { [weak self] in self?.defaults.set($0, forKey: Keys.streamingEnabled) }.store(in: &cancellables)
        $autoEnterDelayMs.dropFirst().sink { [weak self] in self?.defaults.set($0, forKey: Keys.autoEnterDelayMs) }.store(in: &cancellables)
        $inputDeviceUID.dropFirst().sink { [weak self] in self?.defaults.set($0, forKey: Keys.inputDeviceUID) }.store(in: &cancellables)
        $formatModel.dropFirst().sink { [weak self] in self?.defaults.set($0, forKey: Keys.formatModel) }.store(in: &cancellables)
        // 既定文と同じ内容は保存しない（Windows 版と同じ方式）。
        // 既定文をそのまま永続化すると、アプリ更新で既定文を改善しても古い文が使われ続けるため
        $autoFormatPrompt.dropFirst().sink { [weak self] in
            let isDefault = $0.trimmingCharacters(in: .whitespacesAndNewlines)
                == TextFormatter.defaultPrompt.trimmingCharacters(in: .whitespacesAndNewlines)
            self?.defaults.set(isDefault ? "" : $0, forKey: Keys.autoFormatPrompt)
        }.store(in: &cancellables)
        $handsfreeKey.dropFirst().sink { [weak self] in self?.defaults.set($0, forKey: Keys.handsfreeKey) }.store(in: &cancellables)
        $splitParallelEnabled.dropFirst().sink { [weak self] in self?.defaults.set($0, forKey: Keys.splitParallelEnabled) }.store(in: &cancellables)
        $replacements.dropFirst().sink { [weak self] in self?.saveReplacements($0) }.store(in: &cancellables)
    }

    /// ユーザー辞書の確定置換を最終テキストに適用する。
    /// 有効・置換元が空でない行を登録順に単純置換する（部分一致。語境界は見ない）。
    func applyReplacements(_ text: String) -> String {
        var result = text
        for rule in replacements where rule.enabled && !rule.from.isEmpty {
            result = result.replacingOccurrences(of: rule.from, with: rule.to)
        }
        return result
    }

    /// スロット設定を ID で取得する
    func slot(_ id: Int) -> SlotConfig {
        id == 1 ? slot1 : slot2
    }

    private static func loadSlot(_ defaults: UserDefaults, key: String) -> SlotConfig? {
        guard let data = defaults.data(forKey: key) else { return nil }
        return try? JSONDecoder().decode(SlotConfig.self, from: data)
    }

    private func saveSlot(_ slot: SlotConfig, key: String) {
        if let data = try? JSONEncoder().encode(slot) {
            defaults.set(data, forKey: key)
        }
    }

    private static func loadReplacements(_ defaults: UserDefaults) -> [ReplacementRule]? {
        guard let data = defaults.data(forKey: Keys.replacements) else { return nil }
        return try? JSONDecoder().decode([ReplacementRule].self, from: data)
    }

    private func saveReplacements(_ rules: [ReplacementRule]) {
        if let data = try? JSONEncoder().encode(rules) {
            defaults.set(data, forKey: Keys.replacements)
        }
    }
}
