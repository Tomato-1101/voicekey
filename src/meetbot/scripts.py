"""
Google Meet のページ内で実行する JavaScript

**Meet の DOM に依存する箇所はこのファイルだけ**に閉じ込める。Meet の実装は予告なく
変わるので、壊れたときにここだけ直せば済むようにしてある（Mac 版の `MeetBotScripts.swift` と同じ中身）。

壊れにくくするための方針:
- クラス名（難読化されていて変わりやすい）を単独の頼りにしない。まず `jsname` や `aria-label`、
  ボタンの表示文字といった意味のある手掛かりで探し、駄目なら別の手掛かりへ落ちる。
- 日本語 UI / 英語 UI の両方の文言を見る。
- 見つからないときは例外を投げず、`null` / `false` を返す（呼び出し側が状態を判断する）。
"""

import json
from typing import Any, List

# 共通のヘルパ（各スクリプトの先頭に差し込む）。
# vis は「画面に出ているか」。Meet は使わないボタンも DOM に残すので、見えていないものを押さない。
_HELPERS = r"""
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


def js_literal(value: Any) -> str:
    """
    Python の値を JS のリテラルにする（文字列・配列を安全に埋め込むため）。

    Args:
        value: 文字列・配列など JSON にできる値

    Returns:
        JS の式としてそのまま読める文字列（JSON は JS の式として読める）
    """
    return json.dumps(value, ensure_ascii=False)


# Cookie 同意バナーを閉じる。
# 未ログインのプロファイルで最初に Meet を開くと同意画面が挟まり、その裏では参加ボタンに
# たどり着けない。押すとページが遷移するので、参加操作の前に別呼び出しで実行する。
CONSENT = (
    "(() => {" + _HELPERS + r"""
    return { clicked: clickByPattern(['^同意する$', 'すべて同意', 'Accept all', 'I agree', '^同意']) };
})()"""
)

# 会議コードのページから追い出されたか。
# 未ログインのプロファイルで会議 URL を開くと、Meet が紹介サイトへ飛ばすことがある。
# 会議コードのパスに留まっていれば false（ゲスト参加の名前欄が出ている状態も含む）。
LEFT_MEETING_PAGE = r"""(() => {
    if (location.host !== 'meet.google.com') return true;
    const path = location.pathname;
    return path === '/' || /^\/(about|landing)(\/|$)/.test(path);
})()"""


def join(display_name: str) -> str:
    """
    会議に参加するスクリプトを返す。

    1. 「マイクとカメラを使わずに続行」のダイアログが出ていれば押す（Chrome 側でカメラ・マイクを
       拒否しているので、Meet がこれを尋ねてくる）
    2. マイクとカメラを切る（`data-is-muted="false"` は Meet が入れている状態属性）
    3. ゲスト（Google に未ログイン）のときだけ出る名前欄を埋める
    4. 「今すぐ参加」か「参加をリクエスト」を押す

    名前を入れた直後はボタンがまだ押せないことがあるが、呼び出し側が数秒おきに押し直すので待たない。

    Args:
        display_name: 会議に出すボットの名前

    Returns:
        `{ clicked: 押したボタンの表示名か null, nameFilled: 名前欄を埋めたか }` を返す JS
    """
    return (
        "(() => {" + _HELPERS + r"""
    clickByPattern([
        'マイクとカメラを使用せずに続行', 'マイクとカメラなしで続行',
        'Continue without microphone and camera', 'without microphone'
    ]);
    // マイク・カメラを確実に切る（ボットは聞くだけ）
    document.querySelectorAll('[data-is-muted="false"]').forEach((b) => { try { b.click(); } catch (e) {} });

    // ゲスト参加のときだけ名前欄が出る。まず欄の説明（名前 / Your name）で探し、
    // 無ければ見えている 1 行の入力欄を使う
    const inputs = [...document.querySelectorAll('input[type="text"],input:not([type])')].filter(vis);
    const hint = (el) => ((el.getAttribute('aria-label') || '') + ' ' + (el.getAttribute('placeholder') || ''));
    const input = inputs.find((el) => /名前|your name|name/i.test(hint(el))) || inputs[0];
    let nameFilled = false;
    if (input && !input.value) {
        const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
        setter.call(input, """ + js_literal(display_name) + r""");
        input.dispatchEvent(new Event('input', { bubbles: true }));
        input.dispatchEvent(new Event('change', { bubbles: true }));
        nameFilled = true;
    }

    const clicked = clickByPattern([
        '今すぐ参加', 'Join now',
        '参加をリクエスト', 'Ask to join',
        '参加$', '^Join$'
    ]);
    return { clicked, nameFilled };
})()"""
    )


# 会議画面の状態を読む（入室できたか・承認待ちか・拒否・終了など）。
# 文言の判定は Python 側（rules.page_status）で行う。ここは材料を集めるだけ。
# 会議中は字幕の本文が画面の文字に混ざるので、会議中は文字を持ち帰らない。
# signInPage: Google のログイン画面へ飛ばされたか（ゲストが入れない会議）
# joinControls: 参加ボタンか名前欄が見えているか（見えている間は「ゲストは入れない」と決めつけない）
PAGE_STATUS = (
    "(() => {" + _HELPERS + r"""
    const inMeeting = !!buttons().find((b) => /通話から退出|Leave call|退出/i.test(label(b)));
    const text = inMeeting ? '' : (document.body ? document.body.innerText : '').slice(0, 3000);
    const signInPage = location.host === 'accounts.google.com';
    const joinButton = !!buttons().find((b) =>
        /今すぐ参加|Join now|参加をリクエスト|Ask to join/i.test(label(b)));
    const nameField = [...document.querySelectorAll('input[type="text"],input:not([type])')].some(vis);
    return { inMeeting, text, signInPage, joinControls: joinButton || nameField };
})()"""
)

# 会議に入ったあと、マイクとカメラが切れていることを確かめる。
# Chrome 側で拒否しているので通常は何も押さない。万一オンで入っていたら切る。
ENSURE_MUTED = r"""(() => {
    let count = 0;
    document.querySelectorAll('[data-is-muted="false"]').forEach((b) => {
        if (b.offsetParent) { try { b.click(); count++; } catch (e) {} }
    });
    return count;
})()"""

# 字幕ボタンの状態を読む（押すかどうかは Python 側の rules.caption_toggle が決める）。
# 状態を見ずに c キーを送ると、既に字幕がオンのときにオフにしてしまうので、まず集めてから判断する。
CAPTION_STATE = (
    "(() => {" + _HELPERS + r"""
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
})()"""
)


def click_caption_button(button_label: str) -> str:
    """
    字幕ボタンをラベルで押すスクリプトを返す。

    Args:
        button_label: `CAPTION_STATE` が返したラベル（そのまま一致で探す）

    Returns:
        押せたら true を返す JS
    """
    return (
        "(() => {" + _HELPERS + r"""
    const want = """ + js_literal(button_label) + r""";
    const hit = buttons().find((b) => label(b).slice(0, 80) === want);
    if (!hit) return false;
    hit.click();
    return true;
})()"""
    )


# 字幕のキーボードショートカット（c）を送る。
# ボタンがどれも見つからないときの最後の手段。オン・オフが切り替わるだけなので、
# 呼ぶのは状態が分からず字幕も見えていないときに 1 回だけ（rules.caption_toggle）。
CAPTION_SHORTCUT = r"""(() => {
    document.body.dispatchEvent(new KeyboardEvent('keydown', { key: 'c', code: 'KeyC', bubbles: true }));
    return true;
})()"""

# 字幕の言語ごとの、Meet の選択肢の表示名（日本語 UI / 英語 UI）
_LANGUAGE_NAMES = {
    "ja": ["日本語", "Japanese"],
    "en": ["英語", "English"],
}


def caption_language(language: str) -> str:
    """
    字幕の言語を選ぶスクリプトを返す（取れなくても記録は続ける＝ベストエフォート）。

    Meet は字幕をオンにすると、字幕欄か設定に「会議の言語」の選択欄を出す。
    見つからなければ何もしない（Meet が自動で判定した言語のまま）。

    Args:
        language: 選びたい言語（"ja" / "en"。知らない値は日本語として扱う）

    Returns:
        'already' / 'selected' / 'no-picker' / 'no-option' を返す JS
    """
    names: List[str] = _LANGUAGE_NAMES.get(language, _LANGUAGE_NAMES["ja"])
    return (
        "(async () => {" + _HELPERS + r"""
    const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
    const names = """ + js_literal(names) + r""";
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
})()"""
    )


# いま画面に出ている字幕を話者名つきで取り出す。
# Meet の字幕本文は jsname="tgaKEf" の要素に入る（複数の実装で長く使われている手掛かり）。
# 話者名は同じブロックの中にある短いテキストか、参加者アイコンの alt。
# どちらも取れないときは、字幕領域（aria-label に「字幕」/Captions）の生テキストで代替する。
CAPTIONS = (
    "(() => {" + _HELPERS + r"""
    const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
    const bodies = [...document.querySelectorAll('[jsname="tgaKEf"]')];
    // 字幕欄の枠は「字幕」「Captions」そのものを aria-label に持つ。ボタンは別物なので除く
    // （ツールバーの「字幕をオフにする」ボタンの合字 closed_caption を本文として拾う誤一致を防ぐ）
    const isButtonLike = (el) => !!el.closest('button,[role="button"]');
    const regionEl = [...document.querySelectorAll('[aria-label],[jsname]')].find((el) => {
        const text = (el.getAttribute('aria-label') || '').trim();
        return /^(字幕|captions)$/i.test(text) && !isButtonLike(el) && el.innerText && el.offsetParent;
    });
    // 字幕がオンか。本文の有無とは切り離す（誰も話していない間は本文が空でも字幕はオン）
    const pressedOn = buttons().some((b) =>
        /字幕|caption|subtitle/i.test(label(b)) && b.getAttribute('aria-pressed') === 'true');
    const captionsOn = bodies.some(vis) || !!regionEl || pressedOn;
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
        return { found: true, captionsOn, source: 'jsname', entries };
    }

    if (regionEl) {
        return { found: true, captionsOn, source: 'region', entries: [{ speaker: '', text: clean(regionEl.innerText) }] };
    }
    return { found: false, captionsOn, entries: [] };
})()"""
)

# 会議から退出する
LEAVE = (
    "(() => {" + _HELPERS + r"""
    return { clicked: clickByPattern(['通話から退出', 'Leave call', '^退出$']) };
})()"""
)

# 会議名（タブの題名）を読む
TITLE = "document.title"
