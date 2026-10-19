/// 設定ウィンドウの「ライブ字幕」タブ
///
/// 「字幕の設定が voicekey の設定画面からできないので、できるようにして。設定の置き場所は
/// 設定 UI に集約」（2026-08-10 ユーザー指示）。これまではメニューバーのサブメニューだけが
/// 入口だったが、他の設定と同じ場所で触れるようにする（メニューバー側は残す）。
///
/// 値の正本は `CaptionSettings`（UserDefaults の `caption*` キー）。ここは他タブと同じく
/// `ConfigStore` の `@Published` ミラーを bind して、書き込みは ConfigStore の sink 経由にする。
/// ただし**動作中の字幕へ即時反映**が要るもの（読み上げ・原文表示・対象・エンジン）は
/// `CaptionService` にも配る（メニューバーから変えたときと同じ経路）。
import AppKit
import SwiftUI

/// 字幕翻訳の「エンジン＋モデル」の選択値（Apple・Soniox はモデルを選ばないので空）
struct CaptionEngineChoice: Hashable {
    let engine: TranslationEngine
    let model: String
}

/// 字幕の状態をを SwiftUI へ流すための小さな観測モデル
///
/// `CaptionService.onStateChange` は AppKit のコールバック 1 本しか持たないため、
/// ここで受けて `@Published` に変換する（メニューバーは開くたびに作り直すので購読しない）。
@available(macOS 26.0, *)
@MainActor
final class CaptionSettingsModel: ObservableObject {

    /// いまの状態を 1 行で（「停止中」「キャプチャ中」など）
    @Published var stateTitle: String = "停止中"
    /// 動作中か（ボタンの文言に使う）
    @Published var isActive: Bool = false

    private weak var service: CaptionService?

    /// 字幕サービスに繋いで状態を受け取り始める
    ///
    /// **@Published の更新は必ずビューの更新サイクルの外で行う**。`onAppear` や
    /// 状態コールバックの中で同期的に触ると、SwiftUI が「更新中の状態変更」を検出して
    /// AttributeGraph の precondition で abort する（実際にこれで落ちた）。
    ///
    /// - Parameter service: 監視する字幕サービス
    func attach(_ service: CaptionService) {
        guard self.service !== service else { return }
        self.service = service
        service.onStateChange = { [weak self] state in
            Task { @MainActor in self?.apply(state) }
        }
        let current = service.state
        Task { @MainActor [weak self] in self?.apply(current) }
    }

    /// 状態を反映する（変化が無ければ何もしない）
    private func apply(_ state: CaptionRunState) {
        let title = state.menuTitle
        let active = state.isActive
        guard title != stateTitle || active != isActive else { return }
        stateTitle = title
        isActive = active
    }

    /// 字幕の開始／停止
    func toggle() {
        guard let service else { return }
        if service.state.isActive {
            service.stop()
        } else {
            service.start()
        }
    }
}

/// ライブ字幕タブ
@available(macOS 26.0, *)
struct CaptionSettingsTab: View {

    @ObservedObject var config: ConfigStore
    /// 開始/停止と即時反映のために使う（観測しない plain 参照。他タブの作法に合わせる）
    var controller: AppController?

    @StateObject private var model = CaptionSettingsModel()

    /// API キーの状態表示（表示のたびに引き直さないよう、開いたときに一度だけ調べる）
    ///
    /// `APIKeyStore.load` は `/usr/bin/security` を子プロセスで起動するため、
    /// body から毎回呼ぶとスクロールのたびにプロセスが 3 つ立ち上がる。
    @State private var keyStatuses: [(provider: APIProvider, status: String)] = []
    /// 字幕の大きさ倍率（スライダーの表示用。正本は CaptionSettings.hudScale）
    @State private var hudScale = Double(CaptionSettings.hudScale)
    /// Groq / Gemini の実在一覧（未取得なら nil＝静的一覧だけで出す）
    @State private var groqAvailable: [String]?
    @State private var geminiAvailable: [String]?

    // 他の設定タブと同じ grouped Form に揃える（見出しと説明文の置き方がこのタブだけ違い、押せる場所も狭かった）。
    var body: some View {
        Form {
            statusSection
            modeSection
            transcriptSection
            engineSection
            displaySection
            keySection
        }
        .scrollContentBackground(.hidden)  // grouped Form の不透明背景を消してすりガラス下地を透かす
        .glassFormRows()                   // 行フィルを半透明化して島の中で「ガラスの棚」に見せる
        .formStyle(.grouped)
        .padding(.vertical, 8)
        .onAppear {
            // タブを開いたときにだけ字幕サービスへ繋ぐ（起動時の遅延生成を壊さない）
            if let controller { model.attach(controller.caption) }
            keyStatuses = APIProvider.allCases.map { ($0, Self.keyStatus($0)) }
        }
    }

    // MARK: - 状態

    private var statusSection: some View {
        Section("状態") {
            LabeledContent {
                Button(model.isActive ? "字幕を停止" : "字幕を開始") { model.toggle() }
            } label: {
                VStack(alignment: .leading, spacing: 3) {
                    Label(model.stateTitle, systemImage: model.isActive ? "waveform" : "pause.circle")
                        .foregroundStyle(model.isActive ? Brand.signal : .primary)
                    Text("ショートカット ⌥⌘S でも開始・停止できます。")
                        .font(.caption).foregroundStyle(.secondary)
                }
            }
            Toggle(isOn: $config.captionAutoStart) {
                SettingRowLabel(
                    title: "起動時に字幕を自動開始",
                    detail: "一度でも字幕を開始したあとから効きます（初回起動でいきなり許可を求めないため）。"
                )
            }
        }
    }

    // MARK: - モードと言語

    private var modeSection: some View {
        Section("モード") {
            Picker(selection: modeBinding) {
                ForEach(CaptionMode.allCases, id: \.self) { mode in
                    Text(mode.displayName).tag(mode)
                }
            } label: {
                SettingRowLabel(
                    title: "字幕の動作",
                    detail: config.captionMode == .transcribe
                        ? "端末内（Apple の音声認識）で文字起こしします。翻訳もクラウド送信も行いません。"
                        : "英語を聞いて日本語に訳します（認識は英語で固定）。"
                )
            }
            .pickerStyle(.segmented)

            if config.captionMode == .transcribe {
                Picker("認識する言語", selection: languageBinding) {
                    ForEach(CaptionLanguage.allCases, id: \.self) { language in
                        Text(language.displayName).tag(language)
                    }
                }
            }
        }
    }

    // MARK: - 議事録

    private var transcriptSection: some View {
        Section("議事録（文字起こしの保存）") {
            Toggle(isOn: saveTranscriptBinding) {
                SettingRowLabel(
                    title: "文字起こしを自動で保存する",
                    detail: "字幕が動いている間、確定した文字起こしを Markdown に追記します"
                        + "（訳文は保存しません）。5 分以上あくと別のファイルになります。"
                )
            }
            LabeledContent {
                Button("保存先を開く") { openTranscriptDirectory() }
            } label: {
                VStack(alignment: .leading, spacing: 3) {
                    Text("保存先")
                    Text(CaptionService.transcriptDirectory.path)
                        .font(.caption).foregroundStyle(.secondary)
                        .lineLimit(1).truncationMode(.middle)
                }
            }
        }
    }

    /// 議事録フォルダを Finder で開く（無ければ作ってから開く）
    private func openTranscriptDirectory() {
        let directory = CaptionService.transcriptDirectory
        try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        NSWorkspace.shared.open(directory)
    }

    // MARK: - 翻訳エンジン

    /// エンジンとモデルを 1 つのポップアップで決める（録音キーの文字起こしエンジンと同じ形）。
    /// 以前は「翻訳エンジン」を選んでから、その下に出る「Gemini / Groq のモデル」をもう一度選ぶ 2 段だった
    /// （2026-10-19 ユーザー指示「1 個のポップアップで、プロバイダーを選んですぐにモデルを選べるように」）
    private var engineSection: some View {
        Section("翻訳") {
            LabeledContent {
                GroupedChoiceMenu(
                    groups: engineChoiceGroups,
                    selection: engineChoiceBinding,
                    currentTitle: engineChoiceTitle
                )
            } label: {
                SettingRowLabel(title: "翻訳エンジン", detail: engineDetail)
            }

            if config.captionEngine == .soniox {
                // 課金があるので、選んだときに何が起きるかをはっきり出す
                Text("Soniox は音声を送り続けて認識と翻訳を同時に行います（最速）。字幕を動かしている間ずっと"
                     + "課金されます（約 $0.12/時）。キーは「設定 › API キー」の Soniox を使い、"
                     + "未設定や接続できないときは Apple で表示します。")
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
        .task {
            // 一覧外の提供終了モデルを選ばせないよう、開いたときに実在一覧を取り直す（一覧 GET は無料）。
            // キャッシュで先に描き、取り直せたら差し替える（LLMModelPicker と同じ取り方）
            groqAvailable = LLMModelCatalog.cachedAvailable(.groq)
            geminiAvailable = LLMModelCatalog.cachedAvailable(.gemini)
            await LLMModelCatalog.refreshAvailable(.groq, force: true)
            await LLMModelCatalog.refreshAvailable(.gemini, force: true)
            groqAvailable = LLMModelCatalog.cachedAvailable(.groq)
            geminiAvailable = LLMModelCatalog.cachedAvailable(.gemini)
        }
    }

    /// いまの (エンジン, モデル) の組（Apple・Soniox はモデルを選ばないので空）
    private var engineChoice: CaptionEngineChoice {
        switch config.captionEngine {
        case .gemini: return CaptionEngineChoice(engine: .gemini, model: config.captionGeminiModel)
        case .groq: return CaptionEngineChoice(engine: .groq, model: config.captionGroqModel)
        case .apple, .soniox: return CaptionEngineChoice(engine: config.captionEngine, model: "")
        }
    }

    /// ポップアップの選択を書き戻す。モデルを先に書いてからエンジンを切り替える
    /// （エンジンの切り替えは動作中の字幕へ即反映されるので、その時点で新しいモデルを読ませるため）
    private var engineChoiceBinding: Binding<CaptionEngineChoice> {
        Binding(
            get: { engineChoice },
            set: { newValue in
                switch newValue.engine {
                case .gemini: config.captionGeminiModel = newValue.model
                case .groq: config.captionGroqModel = newValue.model
                case .apple, .soniox: break
                }
                if newValue.engine != config.captionEngine {
                    engineBinding.wrappedValue = newValue.engine
                }
            }
        )
    }

    /// ポップアップの中身（Apple・Soniox は 1 項目、Gemini・Groq は見出しの下にモデル。並びは allCases の順）
    private var engineChoiceGroups: [ChoiceMenuGroup<CaptionEngineChoice>] {
        TranslationEngine.allCases.map { engine in
            switch engine {
            case .apple:
                return ChoiceMenuGroup(title: nil, options: [
                    ChoiceMenuOption(id: CaptionEngineChoice(engine: .apple, model: ""), label: "Apple 翻訳（端末内・無料）"),
                ])
            case .soniox:
                return ChoiceMenuGroup(title: nil, options: [
                    ChoiceMenuOption(id: CaptionEngineChoice(engine: .soniox, model: ""),
                                     label: "Soniox（リアルタイム・有料 約 $0.12/時）"),
                ])
            case .gemini:
                let options = LLMModelCatalog.options(.gemini, current: config.captionGeminiModel, available: geminiAvailable)
                return ChoiceMenuGroup(title: "Gemini", options: options.map {
                    ChoiceMenuOption(id: CaptionEngineChoice(engine: .gemini, model: $0.id), label: $0.label)
                })
            case .groq:
                let options = LLMModelCatalog.options(.groq, current: config.captionGroqModel, available: groqAvailable)
                return ChoiceMenuGroup(title: "Groq", options: options.map {
                    ChoiceMenuOption(id: CaptionEngineChoice(engine: .groq, model: $0.id), label: $0.label)
                })
            }
        }
    }

    /// 閉じたポップアップの表示（提供元 · モデル）
    private var engineChoiceTitle: String {
        switch config.captionEngine {
        case .apple: return "Apple 翻訳（端末内）"
        case .soniox: return "Soniox（リアルタイム）"
        case .gemini: return "Gemini · \(LLMModelCatalog.displayName(.gemini, id: config.captionGeminiModel))"
        case .groq: return "Groq · \(LLMModelCatalog.displayName(.groq, id: config.captionGroqModel))"
        }
    }

    /// 選択中のエンジン（とモデル）の説明
    private var engineDetail: String {
        let common = "Apple はキー不要・端末内で完結。Gemini / Groq は確定した文だけを送り、"
            + "Apple の訳を先に出してから差し替えます（混雑や失敗のときは Apple のまま）。"
        switch config.captionEngine {
        case .gemini:
            return common + "\n" + LLMModelCatalog.detail(.gemini, id: config.captionGeminiModel, available: geminiAvailable)
        case .groq:
            return common + "\n" + LLMModelCatalog.detail(.groq, id: config.captionGroqModel, available: groqAvailable)
        case .apple, .soniox:
            return common
        }
    }

    // MARK: - 表示

    private var displaySection: some View {
        Section("表示と対象") {
            Toggle(isOn: frontmostBinding) {
                SettingRowLabel(
                    title: "最前面のアプリだけを翻訳",
                    detail: "裏で流している音楽などを字幕に混ぜないための既定です。"
                )
            }
            Toggle("英語の原文も表示", isOn: showSourceBinding)
            Toggle("訳文を読み上げる", isOn: speakBinding)

            // 字幕の上でのドラッグ操作は撤去した（常にクリック透過で、下のアプリの操作を邪魔しない）。
            // 大きさはここで変える。
            LabeledContent {
                Slider(value: $hudScale, in: 0.7...2.0, step: 0.1)
                    .frame(width: 180)
                    // ドラッグ中も字幕に即反映する（見ながら合わせられるように）
                    .onChange(of: hudScale) { _, newValue in
                        controller?.caption.hudScale = CGFloat(newValue)
                    }
            } label: {
                SettingRowLabel(
                    title: "字幕の大きさ（\(Int((hudScale * 100).rounded()))%）",
                    detail: "位置は録音ピルの真上に固定です（大きさだけ変えられます）。"
                )
            }
        }
    }

    // MARK: - API キー

    private var keySection: some View {
        Section {
            ForEach(keyStatuses, id: \.provider) { entry in
                LabeledContent(entry.provider.displayName) {
                    Text(entry.status).foregroundStyle(.secondary)
                }
            }
        } header: {
            Text("API キー")
        } footer: {
            Text("キーは「設定 › API キー」で入力できます。ここでは状態だけを表示します"
                 + "（アプリに保存した値 → 環境変数 → 共有 Keychain の順に読みます）。")
                .font(.caption).foregroundStyle(.secondary)
        }
    }

    /// 1 プロバイダー分のキー状態（値そのものは絶対に出さない）
    private static func keyStatus(_ provider: APIProvider) -> String {
        guard let found = APIKeyStore.load(provider) else { return "未設定" }
        return "設定済み（\(found.source.displayName)・\(APIKeyStore.masked(found.key))）"
    }

    // MARK: - 動作中の字幕へも配る binding

    /// 字幕モード（動作中なら CaptionService 側が認識セッションを張り直す）
    private var modeBinding: Binding<CaptionMode> {
        Binding(
            get: { config.captionMode },
            set: { newValue in
                config.captionMode = newValue
                controller?.caption.mode = newValue
            }
        )
    }

    /// 文字起こしの認識言語（同上）
    private var languageBinding: Binding<CaptionLanguage> {
        Binding(
            get: { config.captionLanguage },
            set: { newValue in
                config.captionLanguage = newValue
                controller?.caption.language = newValue
            }
        )
    }

    /// 議事録の自動保存
    private var saveTranscriptBinding: Binding<Bool> {
        Binding(
            get: { config.captionSaveTranscript },
            set: { newValue in
                config.captionSaveTranscript = newValue
                controller?.caption.savesTranscript = newValue
            }
        )
    }

    /// 翻訳エンジン（動作中のサービスにも即反映する）
    private var engineBinding: Binding<TranslationEngine> {
        Binding(
            get: { config.captionEngine },
            set: { newValue in
                config.captionEngine = newValue
                controller?.caption.translationEngine = newValue
            }
        )
    }

    /// 最前面のアプリだけを対象にするか
    ///
    /// 動作中に変えたときの張り直しは CaptionService 側が受け持つ（メニューバーから変えたときと同じ）。
    /// ここで stop → start すると、張り直しの待ち時間を置かずにタップを作り直してしまう。
    private var frontmostBinding: Binding<Bool> {
        Binding(
            get: { config.captionFrontmostOnly },
            set: { newValue in
                config.captionFrontmostOnly = newValue
                controller?.caption.capturesFrontmostOnly = newValue
            }
        )
    }

    /// 訳文の読み上げ
    private var speakBinding: Binding<Bool> {
        Binding(
            get: { config.captionSpeak },
            set: { newValue in
                config.captionSpeak = newValue
                controller?.caption.speaksTranslation = newValue
            }
        )
    }

    /// 英語の原文併記
    private var showSourceBinding: Binding<Bool> {
        Binding(
            get: { config.captionShowSource },
            set: { newValue in
                config.captionShowSource = newValue
                controller?.caption.showsSourceText = newValue
            }
        )
    }
}
