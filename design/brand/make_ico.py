# PNG を埋め込んだ .ico を組む（Vista 以降はすべて PNG 埋め込みで読める）。使い方: make_ico.py out.ico a.png b.png ...
import struct, sys
out, pngs = sys.argv[1], sys.argv[2:]
datas = [open(p, 'rb').read() for p in pngs]
sizes = [struct.unpack('>II', d[16:24]) for d in datas]
hdr = struct.pack('<HHH', 0, 1, len(datas))
off = 6 + 16 * len(datas)
ents, body = b'', b''
for (w, h), d in zip(sizes, datas):
    ents += struct.pack('<BBBBHHII', w % 256, h % 256, 0, 0, 1, 32, len(d), off + len(body))
    body += d
open(out, 'wb').write(hdr + ents + body)
