/// Google Meet のページ内で実行する JavaScript
///
/// **Meet の DOM に依存する箇所はこのファイルだけ**に閉じ込める。Meet の実装は予告なく
/// 変わるので、壊れたときにここだけ直せば済むようにしてある。
///
/// 壊れにくくするための方針:
/// - クラス名（難読化されていて変わりやすい）を**単独の頼りにしない**。まず `jsname` や
///   `aria-label`、ボタンの表示文字といった意味のある手掛かりで探し、駄目なら別の手掛かりへ落ちる。
/// - 日本語 UI / 英語 UI の両方の文言を見る。
/// - 見つからないときは例外を投げず、`null` / `false` を返す（呼び出し側が状態を判断する）。
import Foundation

@available(macOS 26.0, *)
extension MeetBotService {

    /// 共通のヘルパ（各スクリプトの先頭に差し込む）
    ///
    /// `visible` は「画面に出ているか」の判定。Meet は使わないボタンも DOM に残すので、
    /// 見えていないものを押さないようにする。
    private static let helpers = """
        const vis = (el) => !!el && !!el.offsetParent;
        const buttons = () => [...document.querySelectorAll('button,[role="button"]')].filter(vis);
        const label = (el) => ((el.getAttribute('aria-label') || '') + ' ' + (el.innerText || '')).trim();
        const clickByPattern = (patterns) => {
            for (const p of patterns) {
                const re = new RegExp(p, 'i');
                const hit = buttons().find((b) => re.test(label(b)));
                if (hit) { hit.click(); return label(hit).slice(0, 40); }
            }
            return null;
        };
        """

    /// Cookie 同意バナーを閉じる
    ///
    /// 未ログインのプロファイルで最初に Meet を開くと同意画面が挟まり、その裏では
    /// 参加ボタンにたどり着けない（実測。`--meetbot-test` で「同意する」だけが見えた）。
    /// 押すとページが遷移するので、参加操作の**前に**別呼び出しで実行する。
    static let consentScript = """
        (() => {
            \(helpers)
            return { clicked: clickByPattern(['^同意する$', 'すべて同意', 'Accept all', 'I agree', '^同意']) };
        })()
        """

    /// 会議コードのページから追い出されたか
    ///
    /// 未ログインのプロファイルで会議 URL を開くと、Meet は `meet.google.com/` → `/about` →
    /// `apps.google.com/meet` と紹介サイトへ飛ばす（2026-09-08 実測）。会議コードのパスに
    /// 留まっていれば false（ゲスト参加の名前欄が出ている状態も含む）。
    static let leftMeetingPageScript = """
        (() => {
            if (location.host !== 'meet.google.com') return true;
            const path = location.pathname;
            return path === '/' || /^\\/(about|landing)(\\/|$)/.test(path);
        })()
        """

    /// 会議に参加する
    ///
    /// 1. 「マイクとカメラを使わずに続行」のダイアログが出ていれば押す（Chrome 側で
    ///    カメラ・マイクを拒否しているので、Meet がこれを尋ねてくる）
    /// 2. マイクとカメラを切る（`data-is-muted="false"` は Meet が入れている状態属性）
    /// 3. ゲストのときだけ出る名前欄を埋める
    /// 4. 「今すぐ参加」か「参加をリクエスト」を押す
    ///
    /// - Parameter displayName: 会議に出すボットの名前
    static func joinScript(displayName: String) -> String {
        let escaped = displayName.replacingOccurrences(of: "'", with: "\\'")
        return """
            (() => {
                \(helpers)
                clickByPattern([
                    'マイクとカメラを使用せずに続行', 'マイクとカメラなしで続行',
                    'Continue without microphone and camera', 'without microphone'
                ]);
                // マイク・カメラを確実に切る（ボットは聞くだけ）
                document.querySelectorAll('[data-is-muted="false"]').forEach((b) => { try { b.click(); } catch (e) {} });

                // ゲスト参加のときだけ名前欄が出る
                const input = [...document.querySelectorAll('input[type="text"]')].find(vis);
                if (input && !input.value) {
                    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                    setter.call(input, '\(escaped)');
                    input.dispatchEvent(new Event('input', { bubbles: true }));
                    input.dispatchEvent(new Event('change', { bubbles: true }));
                }

                const clicked = clickByPattern([
                    '今すぐ参加', 'Join now',
                    '参加をリクエスト', 'Ask to join',
                    '参加$', '^Join$'
                ]);
                return { clicked };
            })()
            """
    }

    /// 会議に入れたか
    ///
    /// 通話中にだけ出る「通話から退出」ボタンの有無で見る（字幕が出ていなくても判定できる）。
    static let inMeetingScript = """
        (() => {
            \(helpers)
            const leave = buttons().find((b) => /通話から退出|Leave call|退出/i.test(label(b)));
            return !!leave;
        })()
        """

    /// 会議画面の状態を読む（入室できたか・承認待ちか・拒否・終了など）
    ///
    /// 文言の判定は Swift 側（`MeetBotRules.pageStatus`）で行う。ここは材料を集めるだけ。
    /// 会議中は字幕の本文が画面の文字に混ざるので、**会議中は文字を持ち帰らない**。
    static let pageStatusScript = """
        (() => {
            \(helpers)
            const inMeeting = !!buttons().find((b) => /通話から退出|Leave call|退出/i.test(label(b)));
            const text = inMeeting ? '' : (document.body ? document.body.innerText : '').slice(0, 3000);
            return { inMeeting, text };
        })()
        """

    /// 会議に入ったあと、マイクとカメラが切れていることを確かめる
    ///
    /// Chrome 側で拒否しているので通常は何も押さない。万一オンで入っていたら切る。
    static let ensureMutedScript = """
        (() => {
            let count = 0;
            document.querySelectorAll('[data-is-muted="false"]').forEach((b) => {
                if (b.offsetParent) { try { b.click(); count++; } catch (e) {} }
            });
            return count;
        })()
        """

    /// 字幕ボタンの状態を読む（押すかどうかは Swift 側の `MeetBotRules.captionToggle` が決める）
    ///
    /// 以前は「オンにする」ボタンが見つからないと無条件に `c` キーを送っていたため、
    /// 既に字幕がオンだとオフにしてしまうことがあった。まず状態を集めてから判断する。
    static let captionStateScript = """
        (() => {
            \(helpers)
            const related = buttons()
                .map((b) => ({ label: label(b).slice(0, 80), pressed: b.getAttribute('aria-pressed') }))
                .filter((b) => /字幕|caption|subtitle/i.test(b.label));
            const bodies = [...document.querySelectorAll('[jsname="tgaKEf"]')].some(vis);
            const region = [...document.querySelectorAll('[aria-label]')].some((el) =>
                /^(字幕|captions)$/i.test((el.getAttribute('aria-label') || '').trim()) && vis(el));
            return {
                buttons: related.map((b) => ({
                    label: b.label,
                    pressed: b.pressed === 'true' ? true : (b.pressed === 'false' ? false : null)
                })),
                regionVisible: bodies || region
            };
        })()
        """

    /// 字幕ボタンをラベルで押す
    ///
    /// - Parameter buttonLabel: `captionStateScript` が返したラベル（そのまま一致で探す）
    static func clickCaptionButtonScript(_ buttonLabel: String) -> String {
        """
        (() => {
            \(helpers)
            const want = \(jsLiteral(buttonLabel));
            const hit = buttons().find((b) => label(b).slice(0, 80) === want);
            if (!hit) return false;
            hit.click();
            return true;
        })()
        """
    }

    /// 字幕のキーボードショートカット（c）を送る
    ///
    /// ボタンがどれも見つからないときの最後の手段。オン・オフが切り替わるだけなので、
    /// 呼ぶのは**状態が分からず字幕も見えていない**ときに 1 回だけ（`MeetBotRules.captionToggle`）。
    static let captionShortcutScript = """
        (() => {
            document.body.dispatchEvent(new KeyboardEvent('keydown', { key: 'c', code: 'KeyC', bubbles: true }));
            return true;
        })()
        """

    /// 字幕の言語を選ぶ（取れなくても記録は続ける＝ベストエフォート）
    ///
    /// Meet は字幕をオンにすると、字幕欄か設定に「会議の言語」の選択欄を出す。
    /// 見つからなければ何もしない（Meet が自動で判定した言語のまま）。
    ///
    /// - Parameter language: 選びたい言語
    /// - Returns: 'already' / 'selected' / 'no-picker' / 'no-option'
    static func captionLanguageScript(_ language: CaptionLanguage) -> String {
        let names: [String]
        switch language {
        case .japanese: names = ["日本語", "Japanese"]
        case .english: names = ["英語", "English"]
        }
        return """
            (async () => {
                \(helpers)
                const clean = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                const names = \(jsLiteral(names));
                const matches = (text) => names.some((n) => clean(text).startsWith(n));
                const opener = [...document.querySelectorAll('[role="combobox"],button,[role="button"]')]
                    .filter(vis)
                    .find((el) => /会議の言語|字幕の言語|話されている言語|Meeting language|caption language|spoken language/i
                        .test(label(el)));
                if (!opener) return 'no-picker';
                if (names.some((n) => label(opener).includes(n))) return 'already';
                opener.click();
                await new Promise((r) => setTimeout(r, 700));
                const option = [...document.querySelectorAll('[role="option"],[role="menuitemradio"],li')]
                    .filter(vis)
                    .find((el) => matches(el.innerText));
                if (!option) {
                    document.body.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
                    return 'no-option';
                }
                option.click();
                return 'selected';
            })()
            """
    }

    /// Swift の値を JS のリテラルに変換する（文字列・配列を安全に埋め込むため）
    private static func jsLiteral(_ value: Any) -> String {
        // JSON は JS の式としてそのまま読める。トップレベルが文字列でも使えるよう配列で包んで外す
        guard let data = try? JSONSerialization.data(withJSONObject: [value]),
              let text = String(data: data, encoding: .utf8) else { return "null" }
        return String(text.dropFirst().dropLast())
    }

    /// いま画面に出ている字幕を話者名つきで取り出す
    ///
    /// Meet の字幕本文は `jsname="tgaKEf"` の要素に入る（複数の実装で長く使われている手掛かり）。
    /// 話者名は同じブロックの中にある短いテキストか、参加者アイコンの `alt`。
    /// どちらも取れないときは、字幕領域（aria-label に「字幕」/Captions）の生テキストで代替する。
    static let captionScript = """
        (() => {
            const clean = (s) => (s || '').replace(/\\s+/g, ' ').trim();
            const bodies = [...document.querySelectorAll('[jsname="tgaKEf"]')];
            if (bodies.length) {
                const entries = bodies.map((body) => {
                    let block = body.parentElement;
                    for (let i = 0; i < 3 && block; i++) {
                        if (block.querySelector('img[alt]') || block.children.length > 1) break;
                        block = block.parentElement;
                    }
                    let speaker = '';
                    if (block) {
                        const image = block.querySelector('img[alt]');
                        if (image) speaker = clean(image.getAttribute('alt'));
                        if (!speaker) {
                            const candidate = [...block.children]
                                .map((el) => clean(el.innerText))
                                .find((t) => t && t.length <= 40 && t !== clean(body.innerText));
                            speaker = candidate || '';
                        }
                    }
                    return { speaker, text: clean(body.innerText) };
                }).filter((e) => e.text);
                return { found: true, source: 'jsname', entries };
            }

            const region = [...document.querySelectorAll('[aria-label],[jsname]')].find((el) => {
                const text = el.getAttribute('aria-label') || '';
                return /字幕|captions/i.test(text) && el.innerText && el.offsetParent;
            });
            if (region) {
                return { found: true, source: 'region', entries: [{ speaker: '', text: clean(region.innerText) }] };
            }
            return { found: false, entries: [] };
        })()
        """

    /// いま話している参加者の名前を返す（取れなければ null）
    ///
    /// **文字起こしには使わない**。文字起こしはこの Mac の中で（Apple のオンデバイス認識で）
    /// 行うので、ここで読むのは議事録の行頭に添える名前だけ。
    ///
    /// Meet は発話状態の出し方がバージョンで変わるため、手掛かりを 3 つ順に試す:
    /// 1. `data-is-speaking` のような明示的な属性（あれば一番確実）
    /// 2. 「〇〇が話しています」/「… is speaking」の aria-label
    /// 3. 参加者タイルの中で見えている発話インジケーター（クラス名は変わりやすいので最後）
    ///
    /// どれも取れなければ null を返し、議事録は話者名なしの行になる（記録は止めない）。
    static let activeSpeakerScript = """
        (() => {
            const clean = (s) => (s || '').replace(/\\s+/g, ' ').trim();
            const nameOfTile = (tile) => {
                if (!tile) return '';
                const explicit = tile.getAttribute('data-self-name') || tile.getAttribute('data-participant-name');
                if (explicit) return clean(explicit);
                const image = tile.querySelector('img[alt]');
                if (image) return clean(image.getAttribute('alt'));
                const texts = [...tile.querySelectorAll('div,span')]
                    .map((el) => clean(el.innerText))
                    .filter((t) => t && t.length <= 40);
                return texts[0] || '';
            };

            // 1) 明示的な属性
            const flagged = document.querySelector('[data-is-speaking="true"],[data-speaking="true"]');
            if (flagged) {
                const name = nameOfTile(flagged.closest('[data-participant-id]') || flagged);
                if (name) return name;
            }

            // 2) aria-label の文言
            const speakingLabel = [...document.querySelectorAll('[aria-label]')]
                .map((el) => clean(el.getAttribute('aria-label')))
                .find((text) => /が話しています|話し中|is speaking/i.test(text));
            if (speakingLabel) {
                const name = clean(speakingLabel.replace(/が話しています.*$|、?話し中.*$|\\s+is speaking.*$/i, ''));
                if (name) return name;
            }

            // 3) タイルの発話インジケーター
            for (const tile of document.querySelectorAll('[data-participant-id]')) {
                const indicator = tile.querySelector('[class*="IisKdb"],[jsname="A5il2e"],[class*="speaking"]');
                if (indicator && indicator.offsetParent) {
                    const name = nameOfTile(tile);
                    if (name) return name;
                }
            }
            return null;
        })()
        """

    /// 会議から退出する
    static let leaveScript = """
        (() => {
            \(helpers)
            return { clicked: clickByPattern(['通話から退出', 'Leave call', '^退出$']) };
        })()
        """
}

/// 会議画面の状態（`pageStatusScript` の結果を分類したもの）
enum MeetPageStatus: Equatable {
    /// 会議に入っている
    case inMeeting
    /// ホストの承認を待っている
    case waitingApproval
    /// 参加を断られた・入れない会議だった
    case denied
    /// 会議から外された
    case removed
    /// 会議が終わった・退出した画面になった
    case ended
    /// 会議コードが無効（存在しない会議・URL の打ち間違い）
    case notFound
    /// どれでもない（ロビー・読み込み中など）
    case other
}

/// 字幕ボタン 1 つ分の状態
struct MeetCaptionButton: Equatable {
    /// ボタンの表示名（aria-label と表示文字）
    var label: String
    /// `aria-pressed`（無ければ nil）
    var pressed: Bool?
}

/// 字幕をどう扱うかの判断結果
enum MeetCaptionToggle: Equatable {
    /// もうオンなので何もしない
    case alreadyOn
    /// このラベルのボタンを押す
    case click(String)
    /// ボタンが見つからないのでショートカット（c）を 1 回だけ試す
    case shortcut
    /// 状態が分からない（ショートカットも試した後）
    case unknown
}

/// Meet の画面の文言・状態から判断する純粋なロジック（ブラウザ無しでテストできる）
///
/// Meet の**表示文言への依存**はここと上のスクリプトだけに置く（壊れたらこのファイルだけ直す）。
enum MeetBotRules {

    /// 画面の文字から会議の状態を分類する
    ///
    /// 承認待ちと拒否を取り違えると通知が逆になるので、先に「終わり」系を見てから承認待ちを見る。
    ///
    /// - Parameters:
    ///   - inMeeting: 「通話から退出」ボタンが見えているか
    ///   - text: 会議外のときの画面の文字
    static func pageStatus(inMeeting: Bool, text: String) -> MeetPageStatus {
        if inMeeting { return .inMeeting }
        if matches(text, [
            "参加リクエストが拒否", "リクエストが拒否されました", "参加を拒否", "この通話に参加できません",
            "denied your request", "You can't join this call", "can't join this video call",
        ]) {
            return .denied
        }
        // 「削除されました」だけだと、チャットのメッセージ削除など会議と関係ない表示でも当たる。
        // 会議・通話から外されたことを言う文言だけに絞る（確定は呼び出し側で 2 回連続を見る）。
        if matches(text, [
            "通話から削除されました", "会議から削除されました", "退出させられました",
            "You've been removed from", "You have been removed from", "removed you from the",
        ]) {
            return .removed
        }
        if matches(text, [
            "会議コードを確認", "無効なビデオ通話名", "Check your meeting code", "Invalid video call name",
        ]) {
            return .notFound
        }
        if matches(text, [
            "通話は終了しました", "会議は終了しました", "通話から退出しました", "通話を終了しました",
            "The call has ended", "This call has ended", "You left the meeting", "You've left the meeting",
            "The meeting has ended",
        ]) {
            return .ended
        }
        if matches(text, [
            "参加をリクエストしています", "参加を許可すると", "承認されるまで", "参加の承認を待って",
            "Asking to be let in", "when someone lets you in", "Waiting for the host", "Someone will let you in",
        ]) {
            return .waitingApproval
        }
        return .other
    }

    /// 字幕ボタンの状態から、押すべきかを決める
    ///
    /// 間違えてオフにしないことを最優先にする。オンだと読める手掛かり（字幕が見えている・
    /// 押下状態・「オフにする」のラベル）が 1 つでもあれば何もしない。
    ///
    /// - Parameters:
    ///   - buttons: 字幕に関係するボタン
    ///   - regionVisible: 字幕の表示欄が見えているか
    ///   - shortcutTried: ショートカットを既に試したか
    static func captionToggle(buttons: [MeetCaptionButton], regionVisible: Bool, shortcutTried: Bool) -> MeetCaptionToggle {
        if regionVisible { return .alreadyOn }
        let related = buttons.filter { matches($0.label, ["字幕", "caption", "subtitle"]) }
        let offWords = ["オフにする", "turn off", "hide", "無効にする"]
        if related.contains(where: { $0.pressed == true || matches($0.label, offWords) }) {
            return .alreadyOn
        }
        let onWords = ["オンにする", "turn on", "show", "有効にする"]
        if let target = related.first(where: { matches($0.label, onWords) || $0.pressed == false }) {
            return .click(target.label)
        }
        return shortcutTried ? .unknown : .shortcut
    }

    /// Google にログイン済みかを Cookie から判定する
    ///
    /// Google のログインセッションを表す Cookie（`SID` など）が google.com にあり、
    /// 期限切れでなければログイン済みとみなす。値は見ない（名前と期限だけ）。
    ///
    /// - Parameters:
    ///   - cookies: CDP の `Storage.getCookies` が返した配列
    ///   - now: 期限の比較に使う現在時刻
    static func isLoggedIn(cookies: [[String: Any]], now: Date = Date()) -> Bool {
        let names: Set<String> = ["SID", "__Secure-1PSID", "__Secure-3PSID", "SAPISID"]
        return cookies.contains { cookie in
            guard let name = cookie["name"] as? String, names.contains(name),
                  let domain = cookie["domain"] as? String,
                  domain == "google.com" || domain.hasSuffix(".google.com") else { return false }
            // セッション Cookie は expires が -1。正の値で過去なら期限切れ
            if let expires = (cookie["expires"] as? NSNumber)?.doubleValue, expires > 0,
               expires < now.timeIntervalSince1970 {
                return false
            }
            return true
        }
    }

    /// 大文字小文字を区別せずに、いずれかの語を含むか
    private static func matches(_ text: String, _ words: [String]) -> Bool {
        words.contains { text.range(of: $0, options: .caseInsensitive) != nil }
    }
}
