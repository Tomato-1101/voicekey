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

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                statusSection
                Divider()
                modeSection
                Divider()
                transcriptSection
                Divider()
                engineSection
                Divider()
                displaySection
                Divider()
                keySection
            }
            .padding(20)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .onAppear {
            // タブを開いたときにだけ字幕サービスへ繋ぐ（起動時の遅延生成を壊さない）
            if let controller { model.attach(controller.caption) }
            keyStatuses = APIProvider.allCases.map { ($0, Self.keyStatus($0)) }
        }
    }

    // MARK: - 状態

    private var statusSection: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("状態").font(.headline)
            HStack(spacing: 12) {
                Label(model.stateTitle, systemImage: model.isActive ? "waveform" : "pause.circle")
                    .foregroundStyle(model.isActive ? Color.accentColor : .secondary)
                Spacer()
                Button(model.isActive ? "字幕を停止" : "字幕を開始") { model.toggle() }
            }
            Text("ショートカット ⌥⌘S でも開始・停止できます。")
                .font(.caption).foregroundStyle(.secondary)

            Toggle("起動時に字幕を自動開始", isOn: $config.captionAutoStart)
            Text("一度でも字幕を開始したあとから効きます（初回起動でいきなり許可を求めないため）。")
                .font(.caption).foregroundStyle(.secondary)
        }
    }

    // MARK: - モードと言語

    private var modeSection: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("モード").font(.headline)
            Picker("字幕の動作", selection: modeBinding) {
                ForEach(CaptionMode.allCases, id: \.self) { mode in
                    Text(mode.displayName).tag(mode)
                }
            }
            .pickerStyle(.radioGroup)

            if config.captionMode == .transcribe {
                Picker("認識する言語", selection: languageBinding) {
                    ForEach(CaptionLanguage.allCases, id: \.self) { language in
                        Text(language.displayName).tag(language)
                    }
                }
                .frame(maxWidth: 260)
                Text("端末内（Apple の音声認識）で文字起こしします。翻訳もクラウド送信も行いません。")
                    .font(.caption).foregroundStyle(.secondary)
            } else {
                Text("英語を聞いて日本語に訳します（認識は英語で固定）。")
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
    }

    // MARK: - 議事録

    private var transcriptSection: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("議事録（文字起こしの保存）").font(.headline)
            Toggle("文字起こしを自動で保存する", isOn: saveTranscriptBinding)
            Text("字幕が動いている間、確定した文字起こしを Markdown に追記します"
                 + "（訳文は保存しません）。5 分以上あくと別のファイルになります。")
                .font(.caption).foregroundStyle(.secondary)
            HStack {
                Text(CaptionService.transcriptDirectory.path)
                    .font(.caption).foregroundStyle(.secondary)
                    .lineLimit(1).truncationMode(.middle)
                Spacer()
                Button("保存先を開く") { openTranscriptDirectory() }
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

    private var engineSection: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("翻訳").font(.headline)
            Picker("翻訳エンジン", selection: engineBinding) {
                ForEach(TranslationEngine.allCases, id: \.self) { engine in
                    Text(engine.displayName).tag(engine)
                }
            }
            Text("Apple はキー不要・端末内で完結。Gemini / Groq は確定した文だけを送り、"
                 + "混雑や失敗のときは自動で Apple 翻訳に戻ります。")
                .font(.caption).foregroundStyle(.secondary)

            if config.captionEngine == .gemini {
                modelField(
                    title: "Gemini のモデル ID",
                    text: $config.captionGeminiModel,
                    placeholder: CaptionSettings.defaultGeminiModelID
                )
            }
            if config.captionEngine == .groq {
                modelField(
                    title: "Groq のモデル ID",
                    text: $config.captionGroqModel,
                    placeholder: CaptionSettings.defaultGroqModelID
                )
            }
        }
    }

    /// モデル ID の入力欄（空欄にすると既定へ戻る）
    private func modelField(title: String, text: Binding<String>, placeholder: String) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            TextField(title, text: text, prompt: Text(placeholder))
                .textFieldStyle(.roundedBorder)
            Text("空欄にすると既定（\(placeholder)）に戻ります。")
                .font(.caption).foregroundStyle(.secondary)
        }
    }

    // MARK: - 表示

    private var displaySection: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("表示と対象").font(.headline)
            Toggle("最前面のアプリだけを翻訳", isOn: frontmostBinding)
            Text("裏で流している音楽などを字幕に混ぜないための既定です。")
                .font(.caption).foregroundStyle(.secondary)
            Toggle("英語の原文も表示", isOn: showSourceBinding)
            Toggle("訳文を読み上げる", isOn: speakBinding)

            HStack {
                Text("字幕の位置は録音ピルの真上に固定です（大きさだけ変えられます）。")
                    .font(.caption).foregroundStyle(.secondary)
                Spacer()
                Button("字幕の大きさをリセット") { controller?.caption.resetHUDSize() }
            }
        }
    }

    // MARK: - API キー

    private var keySection: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("API キー").font(.headline)
            ForEach(keyStatuses, id: \.provider) { entry in
                HStack {
                    Text(entry.provider.displayName)
                    Spacer()
                    Text(entry.status).foregroundStyle(.secondary)
                }
                .font(.callout)
            }
            Text("キーの正本は共有 Keychain（と環境変数）です。ここでは状態だけを表示し、"
                 + "voicekey からは読み取りだけ行います。")
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
    /// 動作中に変えたときは、認識をきれいに作り直すために字幕を張り替える
    /// （メニューバーから変えたときと同じ挙動）。
    private var frontmostBinding: Binding<Bool> {
        Binding(
            get: { config.captionFrontmostOnly },
            set: { newValue in
                config.captionFrontmostOnly = newValue
                guard let service = controller?.caption else { return }
                let wasActive = service.state.isActive
                service.capturesFrontmostOnly = newValue
                guard wasActive else { return }
                service.stop()
                service.start()
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
