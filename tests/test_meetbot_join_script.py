"""
Meet ボットの参加スクリプトが「押してよいボタン」だけを押すかの回帰テスト

本人と同じ Google アカウントでボットを入れると、Meet は「今すぐ参加」を出さず
「このデバイスに切り替える」（本人の通話を奪う）と「その他の参加方法」だけを出す（2026-11-18 実測）。
実会議は使えないので、node に最小の偽 DOM を作って実際のスクリプトを走らせ、押されたボタンを見る。
node が無い環境ではスキップする。
"""
import json
import shutil
import subprocess
import unittest

from src.meetbot import scripts

NODE = shutil.which("node")

HARNESS = """
const labels = %s;
const pressed = [];
const fakeButtons = labels.map((text) => ({
    offsetParent: {}, innerText: text,
    getAttribute: () => null, click: () => pressed.push(text),
}));
globalThis.document = { querySelectorAll: (s) => (s.includes('button') ? fakeButtons : []) };
globalThis.window = {};
const result = %s;
console.log(JSON.stringify({ clicked: result.clicked, pressed }));
"""


@unittest.skipUnless(NODE, "node が無い")
class JoinScriptTest(unittest.TestCase):
    """偽のロビー画面で参加スクリプトを走らせる"""

    def _pressed(self, labels):
        """見えているボタンが labels だけのページで実行し、押されたボタンの一覧を返す"""
        code = HARNESS % (json.dumps(labels, ensure_ascii=False), scripts.join("voicekey"))
        out = subprocess.run([NODE, "-e", code], capture_output=True, text=True, timeout=20, check=True)
        return json.loads(out.stdout)["pressed"]

    def test_same_account_lobby_opens_other_ways(self):
        # 切り替え（本人の通話を奪う）は押さず、「その他の参加方法」を開く
        self.assertEqual(self._pressed(["このデバイスに切り替える", "その他の参加方法"]), ["その他の参加方法"])

    def test_same_account_joins_here_too(self):
        self.assertEqual(
            self._pressed(["このデバイスに切り替える", "このデバイスでも参加", "コンパニオン モードを使用"]),
            ["このデバイスでも参加"])

    def test_same_account_english(self):
        self.assertEqual(self._pressed(["Switch here", "Other ways to join"]), ["Other ways to join"])
        self.assertEqual(self._pressed(["Switch here", "Join here too", "Use Companion mode"]), ["Join here too"])

    def test_normal_lobby_joins_now(self):
        self.assertEqual(self._pressed(["その他の参加方法", "今すぐ参加"]), ["今すぐ参加"])
        self.assertEqual(self._pressed(["参加をリクエスト"]), ["参加をリクエスト"])

    def test_nothing_to_press(self):
        self.assertEqual(self._pressed(["このデバイスに切り替える", "画面を共有"]), [])


STATUS_HARNESS = """
const spec = %s;
const element = (attributes, text) => ({
    offsetParent: {}, innerText: text || '',
    getAttribute: (name) => (name in attributes ? attributes[name] : null),
});
const buttons = spec.buttons.map((text) => element({}, text));
const tiles = spec.ids.map((id) => element({ 'data-participant-id': id }));
const counts = spec.counts.map((count) => element({ 'data-avatar-count': count }));
globalThis.document = {
    body: { innerText: '' },
    querySelectorAll: (s) => {
        if (s.includes('data-participant-id')) return tiles;
        if (s.includes('data-avatar-count')) return counts;
        if (s.includes('button')) return buttons;
        return [];
    },
};
globalThis.window = {};
globalThis.location = { host: 'meet.google.com' };
console.log(JSON.stringify(%s));
"""


@unittest.skipUnless(NODE, "node が無い")
class PageStatusParticipantsTest(unittest.TestCase):
    """偽の会議画面で状態取得スクリプトを走らせ、参加人数の読み取りを確かめる"""

    def _participants(self, buttons, ids, counts):
        """退出ボタン・参加者のタイル・人数バッジを並べたページで実行し、participants を返す"""
        spec = {"buttons": buttons, "ids": ids, "counts": counts}
        code = STATUS_HARNESS % (json.dumps(spec, ensure_ascii=False), scripts.PAGE_STATUS)
        out = subprocess.run([NODE, "-e", code], capture_output=True, text=True, timeout=20, check=True)
        return json.loads(out.stdout)["participants"]

    def test_counts_distinct_participants_and_badge(self):
        # 同じ参加者のタイルが複数あっても 1 人と数え、人数バッジと比べて大きい方を採る
        device = "spaces/x/devices/"
        self.assertEqual(self._participants(["通話から退出"], [device + "1", device + "1", device + "2"], ["2"]), 2)
        self.assertEqual(self._participants(["通話から退出"], [device + "1"], ["1"]), 1)
        self.assertEqual(self._participants(["通話から退出"], [device + "1"], ["3", "x"]), 3)
        self.assertEqual(self._participants(["通話から退出"], [], []), 0)

    def test_zero_outside_meeting(self):
        # 会議の外（ロビーなど）では、それらしい要素があっても 0
        self.assertEqual(self._participants(["今すぐ参加"], ["spaces/x/devices/1"], ["2"]), 0)


if __name__ == "__main__":
    unittest.main()


# 主催者用ボタンのパネルを真似た偽ページ。メニュー・パネルの開閉を状態で持ち、
# スクリプトを 1 手ずつ繰り返して、押した順と最後の状態を返す。
# 「会議の準備が整いました」ダイアログの閉じるを別に置き、パネルの閉じるだけを押すかも見る（2026-11-19 実測の形）
ACCESS_HARNESS = """
const spec = %s;
const clicks = [];
const state = { menu: false, panel: false };
const element = (text, aria, onClick, visible) => ({
    get offsetParent() { return !visible || visible() ? {} : null; },
    innerText: text,
    getAttribute: (name) => (name === 'aria-label' ? aria : null),
    click: onClick,
});
const more = element('more_vert', 'その他のオプション', () => { clicks.push('more'); state.menu = !state.menu; });
const dialogClose = element('close', '閉じる', () => clicks.push('dialog-close'));
const panelClose = element('close', '閉じる', () => { clicks.push('panel-close'); state.panel = false; }, () => state.panel);
const body = { parentElement: null, querySelectorAll: (s) => (s.includes('button') ? [more, dialogClose, panelClose] : []) };
const panel = { parentElement: body, querySelectorAll: (s) => (s.includes('button') ? [panelClose] : []) };
const group = { parentElement: panel, querySelectorAll: () => [] };
const radio = (text, checked) => ({
    labels: [{ innerText: text }], getAttribute: () => null, parentElement: group, checked,
    click() { clicks.push(text); radios.forEach((r) => { r.checked = r === this; }); },
});
const radios = [radio('オープン', spec.startOpen), radio('信頼済み', !spec.startOpen)];
const items = [element('settings 設定', null, () => clicks.push('settings'))];
if (spec.hostItem) {
    items.push(element('lock_person 主催者用ボタン', null, () => { clicks.push('host'); state.menu = false; state.panel = true; }));
}
globalThis.document = {
    querySelectorAll: (s) => {
        if (s.includes('radio')) return state.panel ? radios : [];
        if (s.includes('menuitem')) return state.menu ? items : [];
        if (s.includes('button')) return [more, dialogClose, panelClose];
        return [];
    },
};
globalThis.window = {};
const step = () => %s;
const steps = [];
for (let i = 0; i < 12; i++) {
    const r = step();
    steps.push(r);
    if (r === 'done' || r === 'unavailable') break;
}
console.log(JSON.stringify({ steps, clicks, open: radios[0].checked, panel: state.panel, menu: state.menu }));
"""


@unittest.skipUnless(NODE, "node が無い")
class OpenAccessScriptTest(unittest.TestCase):
    """ボットが始めた会議を「オープン」にするスクリプトを偽の主催者用パネルで走らせる"""

    def _run(self, host_item=True, start_open=False):
        spec = json.dumps({"hostItem": host_item, "startOpen": start_open})
        code = ACCESS_HARNESS % (spec, scripts.OPEN_ACCESS)
        out = subprocess.run([NODE, "-e", code], capture_output=True, text=True, timeout=20, check=True)
        return json.loads(out.stdout)

    def test_selects_open_and_closes_only_the_panel(self):
        r = self._run()
        self.assertEqual(r["steps"], ["menu", "panel", "selected", "done"])
        self.assertEqual(r["clicks"], ["more", "host", "オープン", "panel-close"])
        self.assertTrue(r["open"])
        self.assertFalse(r["panel"])

    def test_already_open_is_left_as_is(self):
        r = self._run(start_open=True)
        self.assertEqual(r["steps"], ["menu", "panel", "done"])
        self.assertNotIn("オープン", r["clicks"])

    def test_without_host_controls_closes_menu_and_gives_up(self):
        r = self._run(host_item=False)
        self.assertEqual(r["steps"], ["menu", "unavailable"])
        self.assertEqual(r["clicks"], ["more", "more"])
        self.assertFalse(r["menu"])
