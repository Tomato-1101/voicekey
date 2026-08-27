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

    /// 会議に参加する
    ///
    /// 1. マイクとカメラを切る（`data-is-muted="false"` は Meet が入れている状態属性）
    /// 2. ゲストのときだけ出る名前欄を埋める
    /// 3. 「今すぐ参加」か「参加をリクエスト」を押す
    ///
    /// - Parameter displayName: 会議に出すボットの名前
    static func joinScript(displayName: String) -> String {
        let escaped = displayName.replacingOccurrences(of: "'", with: "\\'")
        return """
            (() => {
                \(helpers)
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
