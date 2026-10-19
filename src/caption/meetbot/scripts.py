"""
Google Meet のページ内で実行する JavaScript（Mac 版 MeetBotScripts.swift の JS をそのまま移したもの）

**Meet の DOM に依存する箇所はこのファイルだけ**に閉じ込める。Meet の実装は予告なく変わるので、
壊れたときにここだけ直せば済むようにしてある（Mac と同じ JS を保つ。直すときは両方を同時に直す）。

壊れにくくするための方針:
- クラス名（難読化されていて変わりやすい）を**単独の頼りにしない**。まず `jsname` や `aria-label`、
  ボタンの表示文字といった意味のある手掛かりで探し、駄目なら別の手掛かりへ落ちる。
- 日本語 UI / 英語 UI の両方の文言を見る。
- 見つからないときは例外を投げず、null / false を返す（呼び出し側が状態を判断する）。
"""

import json

# 共通のヘルパ（各スクリプトの先頭に差し込む）。vis は「画面に出ているか」の判定
HELPERS = r"""
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


def js_literal(value) -> str:
    """Python の値を JS のリテラルに変換する（文字列・配列を安全に埋め込むため）。"""
    return json.dumps(value, ensure_ascii=False)


# Cookie 同意バナーを閉じる（未ログインのプロファイルで最初に挟まる。押すと遷移するので参加操作の前に別呼び出し）
CONSENT_SCRIPT = """
(() => {
    %s
    return { clicked: clickByPattern(['^同意する$', 'すべて同意', 'Accept all', 'I agree', '^同意']) };
})()
""" % HELPERS

# 会議コードのページから追い出されたか（未ログインだと /about → apps.google.com/meet へ飛ばされる）
LEFT_MEETING_PAGE_SCRIPT = r"""
(() => {
    if (location.host !== 'meet.google.com') return true;
    const path = location.pathname;
    return path === '/' || /^\/(about|landing)(\/|$)/.test(path);
})()
"""


def join_script(display_name: str) -> str:
    """会議に参加する（マイク・カメラなしで続行 → マイク・カメラを切る → ゲスト名 → 参加ボタン）。"""
    return """
(() => {
    %s
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
        setter.call(input, %s);
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
""" % (HELPERS, js_literal(display_name))


# 会議画面の状態を読む（判定は rules.page_status。会議中は字幕の本文が混ざるので文字を持ち帰らない）
PAGE_STATUS_SCRIPT = """
(() => {
    %s
    const inMeeting = !!buttons().find((b) => /通話から退出|Leave call|退出/i.test(label(b)));
    const text = inMeeting ? '' : (document.body ? document.body.innerText : '').slice(0, 3000);
    return { inMeeting, text };
})()
""" % HELPERS

# 会議に入ったあと、マイクとカメラが切れていることを確かめる（万一オンで入っていたら切る）
ENSURE_MUTED_SCRIPT = r"""
(() => {
    let count = 0;
    document.querySelectorAll('[data-is-muted="false"]').forEach((b) => {
        if (b.offsetParent) { try { b.click(); count++; } catch (e) {} }
    });
    return count;
})()
"""

# 字幕ボタンの状態を読む（押すかどうかは rules.caption_toggle が決める）
CAPTION_STATE_SCRIPT = """
(() => {
    %s
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
""" % HELPERS


def click_caption_button_script(button_label: str) -> str:
    """字幕ボタンをラベルで押す（CAPTION_STATE_SCRIPT が返したラベルとそのまま一致で探す）。"""
    return """
(() => {
    %s
    const want = %s;
    const hit = buttons().find((b) => label(b).slice(0, 80) === want);
    if (!hit) return false;
    hit.click();
    return true;
})()
""" % (HELPERS, js_literal(button_label))


# 字幕のキーボードショートカット（c）。状態が分からず字幕も見えていないときに 1 回だけ使う
CAPTION_SHORTCUT_SCRIPT = r"""
(() => {
    document.body.dispatchEvent(new KeyboardEvent('keydown', { key: 'c', code: 'KeyC', bubbles: true }));
    return true;
})()
"""


def caption_language_script(language: str) -> str:
    """字幕の言語を選ぶ（ベストエフォート。'already' / 'selected' / 'no-picker' / 'no-option'）。"""
    names = ["日本語", "Japanese"] if language == "ja" else ["英語", "English"]
    return """
(async () => {
    %s
    const clean = (s) => (s || '').replace(/\\s+/g, ' ').trim();
    const names = %s;
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
""" % (HELPERS, js_literal(names))


# いま画面に出ている字幕を話者名つきで取り出す（本文は jsname="tgaKEf"。取れなければ字幕欄の生テキスト）
CAPTION_SCRIPT = """
(() => {
    %s
    const clean = (s) => (s || '').replace(/\\s+/g, ' ').trim();
    const bodies = [...document.querySelectorAll('[jsname="tgaKEf"]')];
    // 字幕欄の枠は「字幕」「Captions」そのものを aria-label に持つ。ボタンは別物なので除く
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
})()
""" % HELPERS

# いま話している参加者の名前（取れなければ null）。文字起こしには使わず、議事録の行頭に添える名前だけ
ACTIVE_SPEAKER_SCRIPT = r"""
(() => {
    const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
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
        const name = clean(speakingLabel.replace(/が話しています.*$|、?話し中.*$|\s+is speaking.*$/i, ''));
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

# 会議から退出する
LEAVE_SCRIPT = """
(() => {
    %s
    return { clicked: clickByPattern(['通話から退出', 'Leave call', '^退出$']) };
})()
""" % HELPERS
