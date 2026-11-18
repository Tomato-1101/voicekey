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
