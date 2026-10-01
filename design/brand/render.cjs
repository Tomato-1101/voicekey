// SVG → PNG（libvips/librsvg）。使い方: node render.cjs <in.svg> <out.png> <width> [height]
// sharp は隣のリポジトリ voicekey-site（~/Project/voicekey-site）にインストール済みのものを借りる
const path = require('path');
const sharp = require(path.resolve(__dirname, '..', '..', '..', 'voicekey-site', 'node_modules', 'sharp'));
const [, , inp, out, w, h] = process.argv;
const W = parseInt(w), H = h ? parseInt(h) : null;
(async () => {
  // 大きく描いてから縮める（小サイズでも縁がにじまない）
  const big = await sharp(inp, { density: 72 * Math.max(1, Math.ceil(2048 / 1024)) }).png().toBuffer();
  await sharp(big).resize(W, H || W, { fit: 'contain', background: { r: 0, g: 0, b: 0, alpha: 0 }, kernel: 'lanczos3' }).png().toFile(out);
})().catch(e => { console.error(e); process.exit(1); });
