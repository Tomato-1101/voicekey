//
//  SonioxRegion.swift
//  Soniox の接続リージョン（米国 / 日本）を API キーから自動判定する
//
//  Soniox はリージョン別のプロジェクトで、API キーもリージョン専用
//  （米国のキーは日本の窓口で 401、日本のキーは米国の窓口で通らない）。
//  このアプリは GitHub で一般配布し、利用者が自分のキーを入れる（大半は米国のキー）ため、
//  接続先を固定せず「どちらの窓口でキーが通るか」を無料の REST（モデル一覧）で確かめて決める。
//  判定結果はキーの指紋ごとに UserDefaults へ覚えるので、問い合わせはキー 1 本につき初回だけ。
//

import Foundation
import CryptoKit

enum SonioxRegion: String {
    case us
    case jp

    /// リアルタイム文字起こしの WebSocket 窓口
    var websocketURL: URL {
        switch self {
        case .us: return URL(string: "wss://stt-rt.soniox.com/transcribe-websocket")!
        case .jp: return URL(string: "wss://stt-rt.jp.soniox.com/transcribe-websocket")!
        }
    }

    /// 判定に使う REST（モデル一覧）。課金が発生しない読み取り専用の API
    var modelsURL: URL {
        switch self {
        case .us: return URL(string: "https://api.soniox.com/v1/models")!
        case .jp: return URL(string: "https://api.jp.soniox.com/v1/models")!
        }
    }

    /// 判定の問い合わせ 1 本あたりの待ち上限（秒）。初回の録音開始を長く止めないため短くする
    static let probeTimeout: TimeInterval = 2

    /// 2 窓口の HTTP ステータスからリージョンを決める（純関数・nil は通信失敗）。
    /// jp を先に見るのは、日本のキーをわざわざ取った人は日本の窓口を使いたいはずだから。
    /// どちらも 200 でなければ nil ＝判定不能（呼び出し側はキャッシュせず米国へつなぐ）
    static func decide(jpStatus: Int?, usStatus: Int?) -> SonioxRegion? {
        if jpStatus == 200 { return .jp }
        if usStatus == 200 { return .us }
        return nil
    }

    /// キーの指紋（SHA256 の先頭 8 バイトの hex）。キー本体を保存・ログに出さずに
    /// 「同じキーか」だけを見分けるために使う。キーが変われば指紋も変わり再判定される
    static func fingerprint(of apiKey: String) -> String {
        let digest = SHA256.hash(data: Data(apiKey.utf8))
        return digest.prefix(8).map { String(format: "%02x", $0) }.joined()
    }

    private static func cacheKey(for apiKey: String) -> String {
        "sonioxRegion.\(fingerprint(of: apiKey))"
    }

    /// 覚えている判定結果（無ければ nil）
    static func cached(for apiKey: String, defaults: UserDefaults = .standard) -> SonioxRegion? {
        defaults.string(forKey: cacheKey(for: apiKey)).flatMap(SonioxRegion.init(rawValue:))
    }

    /// 2 窓口へ並列に問い合わせて判定し、判定できたときだけ覚える。
    /// 判定できなかったとき（両方 401・ネットワーク不通・タイムアウト）は覚えずに nil を返す
    /// （一時的な不通で誤ったリージョンを固定しないため。次の録音で再判定される）
    static func detect(apiKey: String, defaults: UserDefaults = .standard) async -> SonioxRegion? {
        async let jp = probe(.jp, apiKey: apiKey)
        async let us = probe(.us, apiKey: apiKey)
        guard let region = decide(jpStatus: await jp, usStatus: await us) else { return nil }
        defaults.set(region.rawValue, forKey: cacheKey(for: apiKey))
        return region
    }

    /// 判定専用のセッション（Cookie・キャッシュを残さない）
    private static let probeSession: URLSession = {
        let cfg = URLSessionConfiguration.ephemeral
        cfg.timeoutIntervalForRequest = probeTimeout
        cfg.timeoutIntervalForResource = probeTimeout
        return URLSession(configuration: cfg)
    }()

    /// 1 窓口へモデル一覧を問い合わせ、HTTP ステータスを返す（通信失敗は nil）
    private static func probe(_ region: SonioxRegion, apiKey: String) async -> Int? {
        var request = URLRequest(url: region.modelsURL, timeoutInterval: probeTimeout)
        request.setValue("Bearer \(apiKey)", forHTTPHeaderField: "Authorization")
        guard let (_, response) = try? await probeSession.data(for: request) else { return nil }
        return (response as? HTTPURLResponse)?.statusCode
    }
}
