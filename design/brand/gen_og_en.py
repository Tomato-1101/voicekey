# 英語ページ用の OG 画像（svg/og-light-en.svg）を svg/og-light.svg から作る。
# アイコンとワードマークは og-light.svg のパスをそのまま使い、タグラインのパスだけを英語に差し替える
# （Geist の TTF が無くても作れるように。書体・大きさ・字間・色・位置の決め方は gen_brand.py の card_svg と同じ）。
# 文言はサイトの英語 LP の見出し（voicekey-site の copy.ts: en.hero.title）。
# 使い方: swiftc -O outline.swift -o outline && python3 gen_og_en.py
# PNG 化: node render.cjs svg/og-light-en.svg <out.png> 1200 630
import json
import os
import re
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
TAG_EN = "Hold a key. Talk. It's typed."


def outline(font, size, kern, text):
    r = subprocess.run([os.path.join(HERE, 'outline'), font, str(size), str(kern), text],
                       capture_output=True, text=True, check=True)
    return json.loads(r.stdout)


src = open(os.path.join(HERE, 'svg', 'og-light.svg')).read()
# タグラインは最後の <path>（card_svg が最後に足す要素）
m = list(re.finditer(r'<path transform="translate\(([-\d.]+) ([-\d.]+)\)" d="[^"]*" fill="([^"]+)"/>', src))[-1]
x_old, y = float(m.group(1)), m.group(2)
TL_JA = outline('name:HiraginoSans-W6', 30, 0.5, '声で書く、いちばん速い方法。')
TL_EN = outline('name:HiraginoSans-W6', 30, 0.5, TAG_EN)
tx = x_old + TL_JA['bbox'][0]  # card_svg の tx（タグラインの左端）
# card_svg は横幅を max(ワードマーク, タグライン) で中央に置く。英語が日本語の幅を超えると全体の位置が変わるので止める
wm_and_tag_w = 1200 - 2 * (tx - 236 - 60) - 236 - 60
assert TL_EN['bbox'][2] <= wm_and_tag_w, (TL_EN['bbox'][2], wm_and_tag_w)
tag = f'<path transform="translate({tx - TL_EN["bbox"][0]:.2f} {y})" d="{TL_EN["d"]}" fill="{m.group(3)}"/>'
out = src[:m.start()] + tag + src[m.end():]
open(os.path.join(HERE, 'svg', 'og-light-en.svg'), 'w').write(out)
print('ok og-light-en.svg', TL_EN['bbox'][2], '<=', round(wm_and_tag_w, 2))
