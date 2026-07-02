//
//  BackendClient.swift
//  自社バックエンド経由でプロバイダを使うためのクライアント（製品版・Phase 5 段階2）
//
//  製品版は長期 API キーをアプリに持たない。Supabase の access_token で自社サーバーに
//  認証し、サーバーがサブスク有効性を検証する。Deepgram は短命 JWT を受け取って
//  アプリが直叩き（低レイテンシ核心を維持）、ElevenLabs/Groq はサーバープロキシ経由で叩く。
//
//  このファイルは「クライアントの提供」だけを担う。既存の Transcriber /
//  StreamingTranscriber / TextFormatter への配線は段階3 で行う。
//  サーバー契約は voicekey-site の app/api/v1/{auth/ephemeral,transcribe/elevenlabs,format} と一致させる。
//

import Foundation

enum BackendClient {

    /// バックエンド呼び出しのエラー（ユーザー向け日本語メッセージ付き）
    enum BackendError: Error {
        case unauthenticated      // ローカルに認証セッションが無い（未ログイン）
        case unauthorized         // 401: トークン無効/期限切れ（要再ログイン・リフレッシュ）
        case freeQuotaExhausted   // 402: 無料体験を使い切った（要アクティベーション）
        case noSubscription       // 403: サブスク無効
        case deviceLimit          // 409: 同時利用デバイス上限
        case rateLimited          // 429: 発行/利用が多すぎる
        case providerUnavailable  // 503: サーバー側プロバイダ未設定
        case server(Int)          // その他の非 200
        case network(Error)       // 通信失敗
        case invalidResponse      // レスポンス解釈不能
        case message(String)      // サーバーが返した日本語メッセージをそのまま表示する

        var userMessage: String {
            switch self {
            case .unauthenticated: return "ログインが必要です"
            case .unauthorized: return "ログインの有効期限が切れました。再度ログインしてください"
            case .freeQuotaExhausted: return "無料体験を使い切りました。続けて使うにはアクティベーションキーの登録が必要です（設定 → アカウント）"
            case .noSubscription: return "利用するにはアクティベーションキーの登録が必要です（設定 → アカウント）"
            case .deviceLimit: return "利用できるデバイス数の上限に達しました"
            case .rateLimited: return "リクエストが多すぎます。少し待ってからお試しください"
            case .providerUnavailable: return "サーバー側の設定エラーです。時間をおいてお試しください"
            case .server(let code): return "サーバーエラー (HTTP \(code))"
            case .network(let e): return "通信に失敗しました: \(e.localizedDescription)"
            case .invalidResponse: return "サーバー応答を解釈できませんでした"
            case .message(let m): return m
            }
        }
    }

    /// Deepgram の短命トークンと失効時刻
    struct EphemeralToken {
        let token: String
        let expiresAt: Date
        /// 録音をまたいでキャッシュ再利用してよいか（サーバーが返す。利用権あり=true）。
        /// 段階3以降は無料体験も再利用トークン化で true になる（消費は confirm の非同期送信で数える）。
        let cacheable: Bool
        /// 無料体験の保留 ID（段階1の hold 経路のみ非null）。録音成功後に /usage/confirm へ送って確定する。
        /// 段階3の再利用トークンでは nil（消費は jti なしの confirmUsageCount で数える）。
        let jti: String?
        /// 段階3: 再利用トークン（free）で「録音成功ごとに /usage/confirm（jti なし）で +1」すべきか。
        /// paid は false（消費ゼロ）。無料体験の再利用モードのみ true。トークンをキャッシュ再利用しても
        /// この値は保持されるので、同一トークンでの N 録音 → N 回の confirm で正しく N 消費される。
        let meter: Bool
    }

    /// ログイン中アカウントの状態（メール・利用権の有無/期限・無料体験の残量）
    struct AccountStatus {
        let email: String?
        let active: Bool          // 有効な利用権（アクティベーションキー or サブスク）があるか
        let activeUntil: Date?    // 利用権の期限（active=true のとき）
        let freeUsed: Int         // 無料体験の消費済み回数
        let freeQuota: Int        // 無料体験の上限回数
        let freeRemaining: Int    // 無料体験の残り回数（active=false のとき意味を持つ）
    }

    /// 短い接続タイムアウトの専用セッション（録音直前に叩くため待たせない）
    private static let session: URLSession = {
        let c = URLSessionConfiguration.ephemeral
        c.timeoutIntervalForRequest = 15
        c.timeoutIntervalForResource = 60
        c.waitsForConnectivity = false
        return URLSession(configuration: c)
    }()

    // MARK: - 公開 API

    /// 製品版サーバー経由（短命トークン / プロキシ）を使うべきかを返す。
    ///
    /// ローカルに認証セッション（access_token）があれば true ＝ ログイン済み。
    /// 各文字起こし/整形プリミティブはこれが true のときだけサーバー経路に切り替え、
    /// false のときは従来の埋め込み/設定キーによる直叩きを使う（段階3 の並存ガード）。
    static var isLoggedIn: Bool {
        Keychain.authSession() != nil
    }

    /// 短命トークンのキャッシュと取得の集約（録音ごとの往復を省く）。
    /// キャッシュ再利用は、サーバーが cacheable:true（=利用権あり paid＝発行で無料枠を消費しない）
    /// と返したときだけ。無料体験（cacheable:false）・不明（旧サーバー）は録音ごとに取り直す＝
    /// 「1録音=1消費」を保証する（同じ JWT を使い回すと無料枠が 1 回しか減らない）。
    /// Deepgram は接続確立時にのみトークンを検証するため、TTL(60秒)内の再利用は接続に十分。
    /// 同時取得は 1 本の Task に集約し、二重発行（＝サーバーのレート/台数枠の浪費）を防ぐ。
    private static let tokenLock = NSLock()
    private static var cachedToken: EphemeralToken?
    private static var inFlightToken: Task<EphemeralToken, Error>?

    /// Deepgram「高速リアルタイム」用の短命 JWT を取得する。
    /// 録音直前に呼び、返ってきた JWT で Deepgram WebSocket を `Bearer` 認証で開く（段階3）。
    /// 利用権あり（paid）でキャッシュ有効なら往復ゼロで即返す（2回目以降の録音を高速化）。
    /// 無料体験は録音ごとに新トークンを発行する（1録音=1消費の保証・キャッシュしない）。
    static func fetchEphemeralToken() async throws -> EphemeralToken {
        // ロック操作は同期ヘルパに閉じ込める（async コンテキストで NSLock を直接触らない）。
        let (cached, task) = cachedOrInFlightToken()
        if let cached { return cached }  // 往復ゼロ
        return try await task!.value     // cached が nil なら task は必ず非 nil
    }

    /// warm ループが温めた有効な短命トークンを「同期・往復ゼロ・非同期化なし」で取り出す。
    /// ストリーミング開始（StreamingTranscriber.start）を main（未ログインの Token 直叩き）と
    /// 同じ「押した瞬間に WS を開く」挙動に揃えるための入口。キャッシュヒット時はこれで即 connect し、
    /// `Task { await fetchEphemeralToken() }` の非同期ホップぶんの始まり遅延を無くす（cold のときだけ非同期取得へ）。
    /// 判定・単発使い切りの規約は `cachedOrInFlightToken` のキャッシュ節（残 15 秒超・jti 付きは取り出しで除去）と一致させる。
    static func cachedEphemeralTokenIfValid() -> EphemeralToken? {
        tokenLock.lock(); defer { tokenLock.unlock() }
        guard let c = cachedToken, c.expiresAt.timeIntervalSinceNow > 15 else { return nil }
        // free の保留トークン(jti 付き)は「1保留=1録音=1confirm」を守るため 1 回使い切り。
        // paid / 再利用 free(jti なし)は TTL 内で使い回す（fetchEphemeralToken と同規約）。
        if c.jti != nil { cachedToken = nil }
        return c
    }

    /// キャッシュ確認と取得の集約を同期・ロック下で行う。
    /// 戻り値はちょうど一方が非 nil（有効キャッシュ or 取得 Task）。
    private static func cachedOrInFlightToken() -> (EphemeralToken?, Task<EphemeralToken, Error>?) {
        let gen = AuthClient.generation  // 取得開始時の認証世代（ロック取得前に控える＝ネスト回避）
        tokenLock.lock(); defer { tokenLock.unlock() }
        // 1) 残 15 秒超のキャッシュがあれば即返す
        if let c = cachedToken, c.expiresAt.timeIntervalSinceNow > 15 {
            // free の保留トークン(jti 付き)は「1保留=1録音=1confirm」を守るため 1 回使い切り
            // （取り出したらキャッシュから除去）。paid(jti なし)は TTL 内で使い回す。
            if c.jti != nil { cachedToken = nil }
            return (c, nil)
        }
        // 2) 進行中の取得があれば相乗りする（録音開始の二重呼び出しを 1 往復に集約）
        if let existing = inFlightToken {
            return (nil, existing)
        }
        let task = Task {
            defer { clearInFlightToken() }  // 完了時に自分でクリア
            let tok = try await performFetchEphemeralToken()
            // 取得中にログアウト/別アカウントのログインが割り込んでいたら、旧アカウントの
            // トークンをキャッシュ・返却しない（別アカウントでの再利用を防ぐ）。
            guard gen == AuthClient.generation else { throw BackendError.unauthenticated }
            // 録音をまたいだキャッシュ再利用は、サーバーが cacheable（=利用権あり paid＝
            // 発行で無料枠を消費しない）と返したときだけ許す。無料体験・不明時はキャッシュ
            // しない＝毎録音で取り直す（1録音=1消費を保証）。旧 paid キャッシュは消す。
            if tok.cacheable {
                storeToken(tok)  // クリアより前にキャッシュへ（取得直後の呼び出しを取りこぼさない）
            } else {
                clearCachedTokenValue()
            }
            return tok
        }
        inFlightToken = task
        return (nil, task)
    }

    /// 進行中フラグを下ろす（取得 Task の完了時に一度だけ呼ぶ）。
    private static func clearInFlightToken() {
        tokenLock.lock(); defer { tokenLock.unlock() }
        inFlightToken = nil
    }

    /// 取得したトークンをキャッシュへ保存する（同期・ロック下）。
    private static func storeToken(_ tok: EphemeralToken) {
        tokenLock.lock(); defer { tokenLock.unlock() }
        cachedToken = tok
    }

    /// キャッシュ値だけを破棄する（進行中の取得 Task は cancel しない＝取得中から呼ぶ用）。
    /// cacheable=false のトークンを取得したときに、残っている旧 paid キャッシュを消す。
    private static func clearCachedTokenValue() {
        tokenLock.lock(); defer { tokenLock.unlock() }
        cachedToken = nil
    }

    /// 実際のトークン取得（fetchEphemeralToken からのみ呼ぶ。集約はラッパー側で保証する）。
    private static func performFetchEphemeralToken() async throws -> EphemeralToken {
        try? await AuthClient.ensureValidSession()  // 失効間際なら先にリフレッシュ
        var req = try authorizedRequest(path: ServerConfig.ephemeralPath)
        req.httpMethod = "POST"
        // 段階3: 「録音成功後に /usage/confirm（jti なし）で回数を数えられる新クライアント」の宣言。
        // これによりサーバーは free でも即時消費せず、有料と同じ再利用可トークン（cacheable:true・meter:true）
        // を返す＝録音開始のサーバー往復を消す。無料枠の +1 はクライアントが録音後に非同期 confirm で行う。
        // （"1"=段階1 hold・未指定=段階0 即時消費。新旧サーバーとの互換のため値で分岐する。）
        req.setValue("2", forHTTPHeaderField: "x-vk-confirm")
        let data = try await send(req)
        struct Resp: Decodable { let token: String; let expires_in: Int; let cacheable: Bool?; let jti: String?; let meter: Bool? }
        guard let r = try? JSONDecoder().decode(Resp.self, from: data) else {
            throw BackendError.invalidResponse
        }
        return EphemeralToken(
            token: r.token,
            expiresAt: Date().addingTimeInterval(TimeInterval(r.expires_in)),
            cacheable: r.cacheable ?? false,  // 不明（旧サーバー）はキャッシュ不可＝毎録音で取り直す
            jti: r.jti,
            meter: r.meter ?? false  // 再利用トークン（free）のみ true。録音後に jti なし confirm で数える
        )
    }

    /// 短命トークンのキャッシュを破棄する（ログアウト時。別アカウントでの再利用を防ぐ）。
    static func clearTokenCache() {
        tokenLock.lock()
        cachedToken = nil
        inFlightToken?.cancel()  // 進行中の取得も無効化（旧アカウント token をキャッシュへ戻させない）
        inFlightToken = nil
        tokenLock.unlock()
    }

    /// 整形プロキシ（/api/v1/format）の接続を温める（録音開始時に呼ぶ）。
    /// 製品版の整形はこのプロキシ経由なので、TLS・接続・サーバー関数（lambda）を先に温めて
    /// おくと、録音後の整形の往復（特に最初の cold start）が短くなる。空テキストはサーバーで
    /// 400 になる＝Groq を呼ばず lambda・認証だけ温まる（無料枠の消費もない＝consume:false）。
    /// 失敗（400 含む）は無視。未ログインなら何もしない。
    static func warmFormatProxy() async {
        guard isLoggedIn else { return }
        try? await AuthClient.ensureValidSession()
        guard var req = try? authorizedRequest(path: ServerConfig.formatProxyPath) else { return }
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: ["text": ""])
        _ = try? await send(req)  // 空テキストは 400。throw は握る（暖機が目的）。
    }

    /// 無料体験の消費を確定する（録音成功後に保留 jti を送る・ベストエフォート）。
    /// 録音開始のクリティカルパスから消費を外すための後段。失敗してもユーザー操作は止めない
    /// （サーバー側で保留は TTL 経過後に整合する）。未ログインなら何もしない。
    static func confirmUsage(jti: String) async {
        guard isLoggedIn else { return }
        guard var req = try? authorizedRequest(path: ServerConfig.usageConfirmPath) else { return }
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: ["jti": jti])
        _ = try? await send(req)  // 結果は使わない（ベストエフォート）
    }

    /// 無料体験の消費を確定する（段階3・再利用トークン用＝jti なし）。録音成功ごとに送り、サーバーは
    /// increment_free_used で free_used を +1 する（paid は no-op）。録音開始のクリティカルパスから
    /// 消費計測を外すための後段＝「録音開始のサーバー往復ゼロ」を実現する要。失敗してもユーザー操作は
    /// 止めない（ベストエフォート・多少の数え落ちは許容）。未ログインなら何もしない。
    static func confirmUsageCount() async {
        guard isLoggedIn else { return }
        guard var req = try? authorizedRequest(path: ServerConfig.usageConfirmPath) else { return }
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        // jti なしの空 JSON。サーバーは jti 不在を「段階3の回数確定」と解釈して increment する。
        req.httpBody = try? JSONSerialization.data(withJSONObject: [String: String]())
        _ = try? await send(req)  // 結果は使わない（ベストエフォート）
    }

    /// ログイン中アカウントの状態（メール・利用権の有無/期限）を取得する。
    /// 設定画面の表示・ゲート判定の事前確認に使う（200 固定＝未契約でも 200 で active:false）。
    static func fetchAccountStatus() async throws -> AccountStatus {
        try? await AuthClient.ensureValidSession()
        let req = try authorizedRequest(path: ServerConfig.mePath)  // GET（httpMethod 未設定）
        let data = try await send(req)
        struct Resp: Decodable {
            let email: String?; let active: Bool?; let active_until: String?
            let free_used: Int?; let free_quota: Int?; let free_remaining: Int?
        }
        guard let r = try? JSONDecoder().decode(Resp.self, from: data) else {
            throw BackendError.invalidResponse
        }
        return AccountStatus(
            email: r.email,
            active: r.active ?? false,
            activeUntil: r.active_until.flatMap(Self.parseISO),
            freeUsed: r.free_used ?? 0,
            freeQuota: r.free_quota ?? 0,
            freeRemaining: r.free_remaining ?? 0
        )
    }

    /// アクティベーションキーを登録（消費）する。成功すると利用権がアカウントに紐付く。
    /// 戻り値は利用権の期限（サーバーが返した場合）。失敗はサーバーの日本語メッセージ付きで throw。
    @discardableResult
    static func redeemActivationKey(_ code: String) async throws -> Date? {
        try? await AuthClient.ensureValidSession()
        var req = try authorizedRequest(path: ServerConfig.redeemPath)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        let trimmed = code.trimmingCharacters(in: .whitespacesAndNewlines)
        req.httpBody = try? JSONSerialization.data(withJSONObject: ["code": trimmed])
        let (data, status) = try await sendStatus(req)
        struct Resp: Decodable { let ok: Bool?; let active_until: String?; let error: String? }
        let r = try? JSONDecoder().decode(Resp.self, from: data)
        if status == 200, r?.ok == true {
            return r?.active_until.flatMap(Self.parseISO)
        }
        // サーバーの日本語メッセージ（「キーが見つかりません」等）を優先表示する。
        if let msg = r?.error, !msg.isEmpty { throw BackendError.message(msg) }
        throw mapStatus(status)
    }

    // MARK: - 使用実績のアカウント連携（#10）

    /// 1 日分の実績（サーバーへ送る形＝この端末の絶対値）。録音時間はミリ秒で送る。
    struct StatsDayPayload: Encodable {
        let day: String          // yyyy-MM-dd
        let chars: Int
        let sessions: Int
        let duration_ms: Int
    }

    /// この端末の日次実績をサーバーへ送る（POST /api/v1/stats/sync）。
    /// サーバーは (user_id, device_id, day) で絶対値 upsert するので、同じ値を再送しても
    /// 二重計上されない（冪等）。最大 60 日。失敗は throw（呼び出し側はだいたい無視する）。
    static func syncStats(days: [StatsDayPayload]) async throws {
        guard !days.isEmpty else { return }
        try? await AuthClient.ensureValidSession()
        var req = try authorizedRequest(path: ServerConfig.statsSyncPath)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        struct Body: Encodable { let days: [StatsDayPayload] }
        req.httpBody = try? JSONEncoder().encode(Body(days: Array(days.prefix(60))))
        _ = try await send(req)
    }

    /// アカウント横断の使用実績スナップショット（日次＋累計）。
    struct AccountStats {
        /// day(yyyy-MM-dd) → 端末横断で合算した 1 日分
        let daily: [String: DayStat]
        /// 累計文字数（端末横断）
        let totalCharacters: Int
        /// 累計回数（端末横断）
        let totalSessions: Int
        /// 累計録音秒数（端末横断）
        let totalRecordingSeconds: Double
        /// #11: サーバーが持つ「この端末ぶん」の日次（server baseline）。
        /// 端末横断 daily からこの端末の寄与を引き、未同期のローカル差分だけを足すために使う。
        /// nil = サーバーが self_daily を返さない旧サーバー（従来の max 合成にフォールバック）。
        let selfDaily: [String: DayStat]?
    }

    /// アカウント横断の使用実績を取得する（GET /api/v1/stats）。
    /// サーバーが端末横断で日ごとに合算した daily と累計 totals を返す。ミリ秒は秒へ戻す。
    static func fetchStats() async throws -> AccountStats {
        try? await AuthClient.ensureValidSession()
        let req = try authorizedRequest(path: ServerConfig.statsPath)  // GET
        let data = try await send(req)
        struct Day: Decodable { let day: String; let chars: Int?; let sessions: Int?; let duration_ms: Int? }
        struct Totals: Decodable { let chars: Int?; let sessions: Int?; let duration_ms: Int? }
        struct Resp: Decodable { let daily: [Day]?; let totals: Totals?; let self_daily: [Day]? }
        guard let r = try? JSONDecoder().decode(Resp.self, from: data) else {
            throw BackendError.invalidResponse
        }
        func parseDaily(_ rows: [Day]) -> [String: DayStat] {
            var out: [String: DayStat] = [:]
            for d in rows {
                out[d.day] = DayStat(
                    characters: d.chars ?? 0,
                    recordingSeconds: Double(d.duration_ms ?? 0) / 1000.0,
                    sessions: d.sessions ?? 0)
            }
            return out
        }
        // self_daily はキーが在るときだけ（空配列でも [:]）。無い旧サーバーは nil。
        let selfDaily = r.self_daily != nil ? parseDaily(r.self_daily!) : nil
        return AccountStats(
            daily: parseDaily(r.daily ?? []),
            totalCharacters: r.totals?.chars ?? 0,
            totalSessions: r.totals?.sessions ?? 0,
            totalRecordingSeconds: Double(r.totals?.duration_ms ?? 0) / 1000.0,
            selfDaily: selfDaily)
    }

    /// ElevenLabs「正確性」プロキシで文字起こしする（FLAC/WAV を multipart 送信）。
    ///
    /// multipart は ElevenLabs がそのまま受け取れる形（file + model_id=scribe_v1 + language_code）で
    /// 組み、`x-vk-passthrough: 1` を付ける。サーバーはこのヘッダを見たらボディを read せず
    /// EL へストリーム透過する（formData の全量バッファ＋再構築をやめる＝中継の二度手間を削減）。
    /// audio は FLAC（可逆・約半分）を優先＝アップロード時間を短縮する。EL は FLAC を受理する
    /// （main の EL 直叩きが FLAC で実証済み）ため、passthrough でもそのまま通る。
    static func transcribeElevenLabs(
        audio: Data, filename: String, contentType: String, language: String
    ) async throws -> String {
        try? await AuthClient.ensureValidSession()  // 失効間際なら先にリフレッシュ
        var req = try authorizedRequest(path: ServerConfig.elevenLabsProxyPath)
        req.httpMethod = "POST"
        let boundary = "vk-\(UUID().uuidString)"
        req.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")
        req.setValue("1", forHTTPHeaderField: "x-vk-passthrough")  // EL 形式で送る＝サーバーは透過
        // 文字起こしは本質的に時間がかかる（cold start＋長文＋日米越境往復）。録音直前の token 取得用に
        // 短く設定したセッション既定(15s)のままだと長文で応答前に切れ「通信に失敗」になるため、
        // このリクエストだけ余裕を持たせる（セッション既定をリクエスト単位で上書き）。
        req.timeoutInterval = 90
        req.httpBody = multipartBody(
            boundary: boundary, audio: audio, filename: filename,
            contentType: contentType, language: language
        )
        let data = try await send(req)
        struct Resp: Decodable { let text: String? }
        return (try? JSONDecoder().decode(Resp.self, from: data))?.text ?? ""
    }

    /// ElevenLabs「正確性」プロキシ（/api/v1/transcribe/elevenlabs）の serverless 関数を温める。
    /// 「正確性」初回利用時の Vercel 関数 cold start（最大数秒）が遅延の主因なので、起動時・
    /// 数分間隔で消費なしの GET を叩いて同じ関数を温存し、録音後の文字起こし POST を warm path に乗せる。
    /// GET は無料枠を消費せず・EL も Supabase も叩かない（即 200）。認証ヘッダは付けない。失敗は無視。
    static func warmElevenLabs() async {
        guard isLoggedIn else { return }
        guard let url = ServerConfig.url(ServerConfig.elevenLabsProxyPath) else { return }
        let req = URLRequest(url: url)  // GET（既定メソッド）。認証不要・消費なし。
        _ = try? await session.data(for: req)
    }

    /// Groq「高速」プロキシで文字起こしする（普通入力・WAV を multipart 送信）。
    ///
    /// Groq は Deepgram のような client 直叩き用の短命トークンが無いので、ElevenLabs と同じく
    /// サーバープロキシ経由。サーバー(Edge・東京)は formData(file + language)を受けて Groq 形式
    /// (model=whisper-large-v3-turbo / response_format=text)へ組み直して中継する＝クライアントは
    /// file と language だけ送る（EL の passthrough とは違い透過フラグは付けない）。
    /// サーバーは consume:true で無料枠を同期消費するので、クライアント側の confirm は不要
    /// （Deepgram の再利用トークン方式とは別＝1 プロキシ呼び出し=1 消費）。
    /// audio は FLAC（可逆・約半分）を優先。サーバーはクライアントの filename をそのまま Groq へ
    /// 転送するので、拡張子で FLAC/WAV を Groq に伝えられる（Groq は OpenAI 互換で FLAC を受理）。
    ///
    /// - Parameter format: multipart に `format=1` を付けるか。true にするとサーバー内で
    ///   STT→整形まで行い、整形済みテキストを `text` で返す（STT と整形を 1 リクエストに統合＝
    ///   録音後のクライアント整形の往復を省く）。整形失敗時でも `text` に STT 原文が入って返るので、
    ///   呼び出し側は `text` をそのまま最終テキストとして使う（再整形はしない）。
    static func transcribeGroq(
        audio: Data, filename: String, contentType: String, language: String,
        format: Bool = false
    ) async throws -> String {
        try? await AuthClient.ensureValidSession()  // 失効間際なら先にリフレッシュ
        var req = try authorizedRequest(path: ServerConfig.groqTranscribeProxyPath)
        req.httpMethod = "POST"
        let boundary = "vk-\(UUID().uuidString)"
        req.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")
        // 普通入力は短尺（ホールド発話）なので token 取得用の短い既定(15s)を少しだけ延長する程度でよい
        // （長文の EL=90s とは別事情）。整形統合時はサーバーで LLM 整形も走るため少し余裕を持たせる。
        req.timeoutInterval = format ? 90 : 60
        req.httpBody = groqMultipartBody(
            boundary: boundary, audio: audio, filename: filename,
            contentType: contentType, language: language, format: format
        )
        let data = try await send(req)
        struct Resp: Decodable { let text: String? }
        return (try? JSONDecoder().decode(Resp.self, from: data))?.text ?? ""
    }

    /// Groq「高速」プロキシ（/api/v1/transcribe/groq）の serverless 関数を温める。
    /// Edge なので cold start はほぼ無いが、EL/format と同じ暖機導線で TLS・接続を先に温めておく。
    /// GET は無料枠を消費せず・Groq も Supabase も叩かない（即 200）。認証ヘッダは付けない。失敗は無視。
    static func warmGroqTranscribe() async {
        guard isLoggedIn else { return }
        guard let url = ServerConfig.url(ServerConfig.groqTranscribeProxyPath) else { return }
        let req = URLRequest(url: url)  // GET（既定メソッド）。認証不要・消費なし。
        _ = try? await session.data(for: req)
    }

    /// Groq テキスト整形プロキシ（モデル/プロンプトはサーバー固定。text のみ送る）。
    /// 失敗時は呼び出し側で原文フォールバックする想定なので throw で返す。
    static func formatText(_ text: String) async throws -> String {
        try? await AuthClient.ensureValidSession()  // 失効間際なら先にリフレッシュ
        var req = try authorizedRequest(path: ServerConfig.formatProxyPath)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        // 整形も長文だと時間がかかる。token 取得用の短いセッション既定(15s)を上書きして余裕を持たせる。
        req.timeoutInterval = 60
        req.httpBody = try? JSONSerialization.data(withJSONObject: ["text": text])
        let data = try await send(req)
        struct Resp: Decodable { let text: String? }
        return (try? JSONDecoder().decode(Resp.self, from: data))?.text ?? text
    }

    /// アプリ内フィードバックを自社サーバーへ送る（認証は任意＝未ログインでも送れる）。
    /// ログイン済みなら Bearer を付けて user_id に紐付ける。サブスク有効性は問わない。
    /// 失敗は throw で返す（呼び出し側がユーザーに表示する）。
    static func submitFeedback(_ message: String) async throws {
        guard let url = ServerConfig.url(ServerConfig.feedbackPath) else {
            throw BackendError.invalidResponse
        }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.setValue(Keychain.deviceId(), forHTTPHeaderField: "x-device-id")
        req.setValue("mac", forHTTPHeaderField: "x-platform")
        // ログイン済みなら Bearer を付ける（未ログインでも送れるよう必須にしない）
        if let auth = Keychain.authSession() {
            req.setValue("Bearer \(auth.accessToken)", forHTTPHeaderField: "Authorization")
        }
        let version = Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "?"
        req.httpBody = try? JSONSerialization.data(
            withJSONObject: ["message": message, "app_version": version]
        )
        _ = try await send(req)
    }

    // MARK: - 内部

    /// 認証ヘッダ（Bearer access_token + device_id + platform）付きリクエストを組み立てる
    private static func authorizedRequest(path: String) throws -> URLRequest {
        guard let auth = Keychain.authSession() else { throw BackendError.unauthenticated }
        guard let url = ServerConfig.url(path) else { throw BackendError.invalidResponse }
        var req = URLRequest(url: url)
        req.setValue("Bearer \(auth.accessToken)", forHTTPHeaderField: "Authorization")
        req.setValue(Keychain.deviceId(), forHTTPHeaderField: "x-device-id")
        req.setValue("mac", forHTTPHeaderField: "x-platform")
        return req
    }

    /// リクエストを投げ、200 ならボディを返す。非 200 はステータスをエラーへ写す。
    /// 401 かつ Authorization 付きなら、一度だけトークンをリフレッシュして再試行する
    /// （失効間際を ensureValidSession で先回りしきれなかった場合の保険）。
    private static func send(_ req: URLRequest, allowRefresh: Bool = true) async throws -> Data {
        let data: Data
        let resp: URLResponse
        do {
            (data, resp) = try await session.data(for: req)
        } catch {
            throw BackendError.network(error)
        }
        guard let http = resp as? HTTPURLResponse else { throw BackendError.invalidResponse }
        if http.statusCode == 200 { return data }
        // 認証付きリクエストが 401 のときだけリフレッシュ＋再試行（匿名のフィードバック等は対象外）
        if http.statusCode == 401,
           allowRefresh,
           req.value(forHTTPHeaderField: "Authorization") != nil,
           (try? await AuthClient.refresh()) != nil,
           let auth = Keychain.authSession() {
            var retry = req
            retry.setValue("Bearer \(auth.accessToken)", forHTTPHeaderField: "Authorization")
            return try await send(retry, allowRefresh: false)
        }
        throw mapStatus(http.statusCode)
    }

    /// 認証付きで送信し (body, statusCode) を返す。非 200 でも throw しない
    /// （呼び出し側がレスポンス本文の error メッセージを使えるようにするため）。
    /// 401 のときだけ一度リフレッシュして再試行する。
    private static func sendStatus(_ req: URLRequest, allowRefresh: Bool = true) async throws -> (Data, Int) {
        let data: Data
        let resp: URLResponse
        do {
            (data, resp) = try await session.data(for: req)
        } catch {
            throw BackendError.network(error)
        }
        guard let http = resp as? HTTPURLResponse else { throw BackendError.invalidResponse }
        if http.statusCode == 401,
           allowRefresh,
           req.value(forHTTPHeaderField: "Authorization") != nil,
           (try? await AuthClient.refresh()) != nil,
           let auth = Keychain.authSession() {
            var retry = req
            retry.setValue("Bearer \(auth.accessToken)", forHTTPHeaderField: "Authorization")
            return try await sendStatus(retry, allowRefresh: false)
        }
        return (data, http.statusCode)
    }

    private static func mapStatus(_ code: Int) -> BackendError {
        switch code {
        case 401: return .unauthorized
        case 402: return .freeQuotaExhausted
        case 403: return .noSubscription
        case 409: return .deviceLimit
        case 429: return .rateLimited
        case 503: return .providerUnavailable
        default: return .server(code)
        }
    }

    /// ISO8601（小数秒あり/なし両対応）を Date へ。解釈不能なら nil。
    private static func parseISO(_ s: String) -> Date? {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let d = f.date(from: s) { return d }
        f.formatOptions = [.withInternetDateTime]
        return f.date(from: s)
    }

    /// ElevenLabs がそのまま受け取れる multipart ボディ（透過用）。
    /// field: model_id=scribe_v1（モデル固定・製品版仕様）, language_code（任意）, file=audio.flac/wav。
    /// サーバーは x-vk-passthrough を見てこのボディを EL へ無加工で流すので、filename/Content-Type は
    /// クライアントが決めたものが EL に届く（FLAC を FLAC として伝えられる）。
    private static func multipartBody(
        boundary: String, audio: Data, filename: String, contentType: String, language: String
    ) -> Data {
        var body = Data()
        func append(_ s: String) { body.append(Data(s.utf8)) }
        // モデルは scribe_v1 固定（サーバーが透過するため、クライアント側で EL 形式に含める）
        append("--\(boundary)\r\n")
        append("Content-Disposition: form-data; name=\"model_id\"\r\n\r\n")
        append("scribe_v1\r\n")
        if !language.isEmpty {
            append("--\(boundary)\r\n")
            append("Content-Disposition: form-data; name=\"language_code\"\r\n\r\n")
            append("\(language)\r\n")
        }
        append("--\(boundary)\r\n")
        append("Content-Disposition: form-data; name=\"file\"; filename=\"\(filename)\"\r\n")
        append("Content-Type: \(contentType)\r\n\r\n")
        body.append(audio)
        append("\r\n--\(boundary)--\r\n")
        return body
    }

    /// Groq プロキシ用の multipart ボディ。サーバー(Edge)が formData で受けて Groq 形式
    /// （model/response_format はサーバー側で固定）へ組み直すため、クライアントは
    /// file(FLAC/WAV) と language(任意) だけ入れる。サーバーは filename を Groq へ引き継ぐ。
    /// format=true のときは `format=1` を付け、サーバーに STT→整形の統合実行を依頼する。
    private static func groqMultipartBody(
        boundary: String, audio: Data, filename: String, contentType: String, language: String,
        format: Bool = false
    ) -> Data {
        var body = Data()
        func append(_ s: String) { body.append(Data(s.utf8)) }
        if !language.isEmpty {
            append("--\(boundary)\r\n")
            append("Content-Disposition: form-data; name=\"language\"\r\n\r\n")
            append("\(language)\r\n")
        }
        // サーバー統合整形の依頼フラグ。サーバーはこれを見たら STT→Groq 整形まで行い整形済みを返す。
        if format {
            append("--\(boundary)\r\n")
            append("Content-Disposition: form-data; name=\"format\"\r\n\r\n")
            append("1\r\n")
        }
        append("--\(boundary)\r\n")
        append("Content-Disposition: form-data; name=\"file\"; filename=\"\(filename)\"\r\n")
        append("Content-Type: \(contentType)\r\n\r\n")
        body.append(audio)
        append("\r\n--\(boundary)--\r\n")
        return body
    }
}
