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

    /// 字幕をオンにする
    ///
    /// 既に ON のときに押して OFF にしてしまわないよう、**「オンにする」意味のラベルだけ**を押す。
    static let enableCaptionsScript = """
        (() => {
            \(helpers)
            const target = buttons().find((b) => {
                const text = label(b);
                if (!/字幕|caption|subtitle/i.test(text)) return false;
                return /オンにする|turn on|有効/i.test(text);
            });
            if (target) { target.click(); return 'clicked'; }
            // ラベルで見つからないときはキーボードショートカット（c）に頼る
            document.body.dispatchEvent(new KeyboardEvent('keydown', { key: 'c', bubbles: true }));
            return 'shortcut';
        })()
        """

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

    /// 会議から退出する
    static let leaveScript = """
        (() => {
            \(helpers)
            return { clicked: clickByPattern(['通話から退出', 'Leave call', '^退出$']) };
        })()
        """
}
