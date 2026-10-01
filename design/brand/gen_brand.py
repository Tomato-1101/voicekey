# 17 LEGEND（仕上げ版）の配布用マスター SVG を生成する。
# 文字はすべてパス化（<img> の SVG はフォントを読まない・環境差で崩れないため）。
import json
import os
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
# リポジトリではマスター SVG を design/brand/svg/ に置いているので、再生成もそこへ上書きする
OUT = os.path.join(HERE, 'svg')
os.makedirs(OUT, exist_ok=True)

SIGNAL = '#FF5A1F'
BONE, STONE, LEGEND, CARBON = '#F4F1EB', '#CFC9BE', '#7F796F', '#2B2A27'


def outline(font, size, kern, text):
    r = subprocess.run([os.path.join(HERE, 'outline'), font, str(size), str(kern), text],
                       capture_output=True, text=True, check=True)
    return json.loads(r.stdout)


G500 = os.path.join(HERE, 'fonts', 'Geist-500.ttf')
G600 = os.path.join(HERE, 'fonts', 'Geist-600.ttf')
LEG = outline(G500, 72, 0.5, 'voice')


def lg(i, stops, horiz=False):
    d = 'x1="0" y1="0" x2="1" y2="0"' if horiz else 'x1="0" y1="0" x2="0" y2="1"'
    s = ''.join(f'<stop offset="{o}" stop-color="{c}" stop-opacity="{op}"/>' for o, c, op in stops)
    return f'<linearGradient id="{i}" {d}>{s}</linearGradient>'


def rg(i, stops, cx=0.5, cy=0.5, r=0.5):
    s = ''.join(f'<stop offset="{o}" stop-color="{c}" stop-opacity="{op}"/>' for o, c, op in stops)
    return f'<radialGradient id="{i}" cx="{cx}" cy="{cy}" r="{r}">{s}</radialGradient>'


def defs(p):
    A = SIGNAL
    return ''.join([
        lg(f'{p}bsk', [(0, '#DDD8CE', 1), (1, '#B3AC9F', 1)]),
        lg(f'{p}btop', [(0, '#FAF9F6', 1), (1, '#EDEAE3', 1)]),
        lg(f'{p}bhous', [(0, '#DAD6CD', 1), (1, '#F4F2EC', 1)]),
        lg(f'{p}csk', [(0, '#3D3C39', 1), (1, '#171615', 1)]),
        lg(f'{p}ctop', [(0, '#383734', 1), (1, '#282725', 1)]),
        lg(f'{p}chous', [(0, '#121110', 1), (1, '#403F3C', 1)]),
        lg(f'{p}side', [(0, '#000000', 0.09), (0.16, '#000000', 0), (0.84, '#000000', 0), (1, '#000000', 0.09)], horiz=True),
        lg(f'{p}dish', [(0, '#000000', 0.05), (0.24, '#000000', 0), (1, '#000000', 0)]),
        lg(f'{p}rim', [(0, '#FFFFFF', 1), (0.45, '#FFFFFF', 0)]),
        lg(f'{p}crim', [(0, '#FFFFFF', 0.22), (0.45, '#FFFFFF', 0)]),
        rg(f'{p}glow', [(0, A, 0.6), (0.45, A, 0.22), (1, A, 0)]),
        rg(f'{p}cglow', [(0, A, 0.75), (1, A, 0)]),
        rg(f'{p}ledhi', [(0, '#FFFFFF', 0.6), (1, '#FFFFFF', 0)], cx=0.42, cy=0.36, r=0.5),
        rg(f'{p}ledeg', [(0.7, '#000000', 0), (1, '#000000', 0.1)]),
        f'<filter id="{p}blur" x="-20%" y="-20%" width="140%" height="140%"><feGaussianBlur stdDeviation="18"/></filter>',
    ])


def led(p, cx, cy, dark):
    g, h = (f'{p}cglow', f'{p}chous') if dark else (f'{p}glow', f'{p}bhous')
    return (f'<circle cx="{cx}" cy="{cy}" r="124" fill="url(#{g})"/>'
            f'<circle cx="{cx}" cy="{cy}" r="37" fill="url(#{h})"/>'
            f'<circle cx="{cx}" cy="{cy}" r="29" fill="{SIGNAL}"/>'
            f'<circle cx="{cx}" cy="{cy}" r="29" fill="url(#{p}ledeg)"/>'
            f'<circle cx="{cx}" cy="{cy}" r="29" fill="url(#{p}ledhi)"/>')


def skirt(p, dark, bleed=False):
    sk = 'csk' if dark else 'bsk'
    if bleed:
        # 全面塗り（macOS 26 以降のアイコンは OS が角丸マスクをかける）
        return (f'<rect width="1024" height="1024" fill="url(#{p}{sk})"/>'
                f'<rect width="1024" height="1024" fill="url(#{p}side)"/>')
    sq = '<rect x="100" y="100" width="824" height="824" rx="185" fill="{f}"/>'
    return (sq.format(f=f'url(#{p}{sk})') + sq.format(f=f'url(#{p}side)')
            + f'<rect x="101.5" y="101.5" width="821" height="821" rx="183.5" fill="none" stroke="#FFFFFF" '
              f'stroke-opacity="{0.06 if dark else 0.45}" stroke-width="3"/>')


def cap(p, dark, small=False):
    """スカート以外（影・天面・刻印・LED）。座標は 1024 基準・余白 100 の旧グリッド。"""
    top, rim, hous = (('ctop', 'crim', 'chous') if dark else ('btop', 'rim', 'bhous'))
    t = '<rect x="172" y="146" width="680" height="652" rx="132" fill="{f}"/>'
    s = ''
    if small:
        s += f'<rect x="172" y="162" width="680" height="652" rx="132" fill="#000000" fill-opacity="{0.3 if dark else 0.1}"/>'
    else:
        s += f'<rect x="180" y="186" width="664" height="636" rx="132" fill="#000000" fill-opacity="{0.5 if dark else 0.2}" filter="url(#{p}blur)"/>'
    s += t.format(f=f'url(#{p}{top})') + t.format(f=f'url(#{p}dish)')
    s += f'<rect x="173.5" y="147.5" width="677" height="649" rx="130.5" fill="none" stroke="url(#{p}{rim})" stroke-width="3"/>'
    if small:
        s += f'<rect x="244" y="674" width="220" height="52" rx="26" fill="{"#8E897F" if dark else "#A19B90"}"/>'
        s += f'<circle cx="694" cy="304" r="86" fill="url(#{p}{hous})"/><circle cx="694" cy="304" r="66" fill="{SIGNAL}"/>'
    else:
        s += f'<path transform="translate(264 706)" d="{LEG["d"]}" fill="{"#9D988E" if dark else "#7F796F"}"/>'
        s += led(p, 723, 275, dark)
    return s


def icon_body(p, dark, small=False, bleed=False):
    if bleed:
        k = 1024 / 824
        return skirt(p, dark, True) + f'<g transform="scale({k:.6f}) translate(-100 -100)">{cap(p, dark, small)}</g>'
    return skirt(p, dark) + cap(p, dark, small)


def svg(w, h, body, extra=''):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}">'
            f'{extra}{body}</svg>\n')


def icon_svg(dark, small=False, bleed=False):
    p = 'k'
    return svg(1024, 1024, icon_body(p, dark, small, bleed), f'<defs>{defs(p)}</defs>')


def auto_svg(small):
    """ライト=ボーン／ダーク=カーボンを OS の外観で自動で切り替える 1 枚の SVG（favicon 用）。"""
    style = '<style>.d{display:none}@media (prefers-color-scheme: dark){.l{display:none}.d{display:inline}}</style>'
    return svg(1024, 1024,
               f'<g class="l">{icon_body("l", False, small)}</g><g class="d">{icon_body("d", True, small)}</g>',
               f'{style}<defs>{defs("l")}{defs("d")}</defs>')


# ---- ワードマーク（Geist 600・字間 -0.035em・右肩に灯り） ----
WM_SIZE = 100
WM = outline(G600, WM_SIZE, -0.035 * WM_SIZE, 'voicekey')


def wordmark_group(color, x, y, size, glow=True):
    """ベースライン (x, y)、文字サイズ size のワードマーク。戻り値: (svg, 右端 x)"""
    k = size / WM_SIZE
    bx, by, bw, bh = WM['bbox']
    r = 0.095 * size
    cx = x + (bx + bw) * k + 0.13 * size + r
    cy = y + by * k + r + 0.02 * size
    g = ''
    if glow:
        g = (f'<circle cx="{cx:.2f}" cy="{cy:.2f}" r="{r * 2.6:.2f}" fill="url(#wmglow)"/>')
    s = (f'<path transform="translate({x:.2f} {y:.2f}) scale({k:.5f})" d="{WM["d"]}" fill="{color}"/>'
         f'{g}<circle cx="{cx:.2f}" cy="{cy:.2f}" r="{r:.2f}" fill="{SIGNAL}"/>')
    return s, cx + r


WM_DEFS = rg('wmglow', [(0, SIGNAL, 0.45), (0.4, SIGNAL, 0.16), (1, SIGNAL, 0)])


def wordmark_svg(color, size=100):
    bx, by, bw, bh = WM['bbox']
    k = size / WM_SIZE
    pad = 0.06 * size
    y = -by * k + pad + 0.02 * size
    body, right = wordmark_group(color, pad - bx * k, y, size)
    w = right + pad + 0.1 * size
    h = (bh * k) + pad * 2 + 0.04 * size
    return svg(round(w), round(h), body, f'<defs>{WM_DEFS}</defs>')


def lockup_svg(dark, icon_px=56, word=34, gap=9, small=True):
    """アイコン＋ワードマークの横組み。dark=True は暗い背景用（文字ボーン・アイコンはカーボン）。"""
    color = BONE if dark else CARBON
    p = 'm'
    s = icon_px / 1024
    # アイコンの見た目の外枠は 100..924（余白込み 1024）なので、余白ぶん左へ寄せて光学的に揃える
    ox = -100 * s
    icon = f'<g transform="translate({ox:.2f} 0) scale({s:.6f})">{icon_body(p, dark, small)}</g>'
    bx, by, bw, bh = WM['bbox']
    base = icon_px / 2 + (-by * word / WM_SIZE) / 2 - 0.02 * word
    wm, right = wordmark_group(color, ox + 924 * s + gap - bx * word / WM_SIZE, base, word)
    return svg(round(right + 8), icon_px, icon + wm, f'<defs>{defs(p)}{WM_DEFS}</defs>')


def write(name, text):
    with open(os.path.join(OUT, name), 'w') as f:
        f.write(text)


if __name__ == '__main__':
    write('icon-bone.svg', icon_svg(False))
    write('icon-carbon.svg', icon_svg(True))
    write('icon-bone-small.svg', icon_svg(False, small=True))
    write('icon-carbon-small.svg', icon_svg(True, small=True))
    write('icon-bone-bleed.svg', icon_svg(False, bleed=True))
    write('icon-carbon-bleed.svg', icon_svg(True, bleed=True))
    write('icon-auto-small.svg', auto_svg(True))
    write('wordmark-carbon.svg', wordmark_svg(CARBON))
    write('wordmark-bone.svg', wordmark_svg(BONE))
    write('lockup-light.svg', lockup_svg(False))
    write('lockup-dark.svg', lockup_svg(True))
    write('lockup-light-large.svg', lockup_svg(False, icon_px=120, word=72, gap=17, small=False))
    write('lockup-dark-large.svg', lockup_svg(True, icon_px=120, word=72, gap=17, small=False))
    print('ok', sorted(os.listdir(OUT)))


# ---- OG / ソーシャルプレビュー（アイコン＋ワードマーク＋タグライン） ----
TAG = '声で書く、いちばん速い方法。'


def card_svg(dark, W, H):
    TL = outline('name:HiraginoSans-W6', 30, 0.5, TAG)
    p = 'c'
    icon_vis, word, gap = 236, 104, 60
    s = icon_vis / 824
    bx, by, bw, bh = WM['bbox']
    k = word / WM_SIZE
    wm_w = (bx + bw) * k + 0.13 * word + 0.19 * word
    total = icon_vis + gap + max(wm_w, TL['bbox'][2])
    x0 = (W - total) / 2
    iy = (H - icon_vis) / 2
    icon = f'<g transform="translate({x0 - 100 * s:.2f} {iy - 100 * s:.2f}) scale({s:.6f})">{icon_body(p, dark)}</g>'
    tx = x0 + icon_vis + gap
    # ワードマークの x-height 中心とアイコン中心を揃え、タグラインを下に置く
    base = H / 2 + 6
    wm, _ = wordmark_group(BONE if dark else CARBON, tx - bx * k, base, word)
    tag = (f'<path transform="translate({tx - TL["bbox"][0]:.2f} {base + 62:.2f})" d="{TL["d"]}" '
           f'fill="{"#9D988E" if dark else LEGEND}"/>')
    bg = (f'<rect width="{W}" height="{H}" fill="url(#cbg)"/>')
    bgdef = lg('cbg', [(0, '#302F2C', 1), (1, '#1E1D1B', 1)]) if dark else lg('cbg', [(0, '#F7F5F0', 1), (1, '#EFEBE3', 1)])
    return svg(W, H, bg + icon + wm + tag, f'<defs>{bgdef}{defs(p)}{WM_DEFS}</defs>')


if __name__ == '__main__':
    write('og-light.svg', card_svg(False, 1200, 630))
    write('social-dark.svg', card_svg(True, 1280, 640))
    print('cards ok')
