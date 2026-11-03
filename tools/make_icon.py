# -*- coding: utf-8 -*-
"""生成 icon.ico（青绿圆角底 + 白色网络节点图形），纯 Python 无第三方依赖。"""
import struct
import sys
import zlib

TOP = (18, 148, 136)
BOT = (9, 84, 82)
NODES = [(0.50, 0.72, 0.105), (0.29, 0.33, 0.090), (0.71, 0.33, 0.090)]
EDGES = [(0, 1), (1, 2), (0, 2)]
LW = 0.036
RAD = 0.22


def sdf_round_rect(px, py):
    qx = abs(px - 0.5) - (0.5 - RAD)
    qy = abs(py - 0.5) - (0.5 - RAD)
    ax, ay = max(qx, 0.0), max(qy, 0.0)
    return (ax * ax + ay * ay) ** 0.5 + min(max(qx, qy), 0.0) - RAD


def seg_dist(px, py, a, b):
    x1, y1 = a[0], a[1]
    x2, y2 = b[0], b[1]
    dx, dy = x2 - x1, y2 - y1
    l2 = dx * dx + dy * dy
    t = 0.0 if l2 == 0 else max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / l2))
    ex, ey = x1 + t * dx - px, y1 + t * dy - py
    return (ex * ex + ey * ey) ** 0.5


def render(canvas=512, ss=2):
    W = canvas * ss
    px = bytearray(W * W * 4)
    idx = 0
    for j in range(W):
        y = (j + 0.5) / W
        t = y
        br, bg_, bb = (round(TOP[k] + (BOT[k] - TOP[k]) * t) for k in range(3))
        for i in range(W):
            x = (i + 0.5) / W
            if sdf_round_rect(x, y) <= 0:
                fg = False
                for cx, cy, cr in NODES:
                    dx, dy = x - cx, y - cy
                    if dx * dx + dy * dy <= cr * cr:
                        fg = True
                        break
                if not fg:
                    for a, b in EDGES:
                        if seg_dist(x, y, NODES[a], NODES[b]) <= LW / 2:
                            fg = True
                            break
                if fg:
                    px[idx:idx + 4] = bytes((255, 255, 255, 255))
                else:
                    px[idx:idx + 4] = bytes((br, bg_, bb, 255))
            idx += 4

    def box_resize(target):
        data = bytearray(target * target * 4)
        ratio = W // target if W % target == 0 else None
        step = W / target
        for ty in range(target):
            y0, y1 = int(ty * step), max(int(ty * step) + 1, int((ty + 1) * step))
            for tx in range(target):
                x0, x1 = int(tx * step), max(int(tx * step) + 1, int((tx + 1) * step))
                n = (x1 - x0) * (y1 - y0)
                r = g = b = a = 0
                for sy in range(y0, y1):
                    row = sy * W * 4
                    for sx in range(x0, x1):
                        o = row + sx * 4
                        r += px[o]
                        g += px[o + 1]
                        b += px[o + 2]
                        a += px[o + 3]
                o2 = (ty * target + tx) * 4
                data[o2:o2 + 4] = bytes((r // n, g // n, b // n, a // n))
        return bytes(data)

    return {s: box_resize(s) for s in (256, 64, 48, 32, 16)}


def png_bytes(w, rgba):
    rb = w * 4
    raw = b"".join(b"\x00" + rgba[y * rb:(y + 1) * rb] for y in range(w))

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, w, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


def main(out):
    sizes = render()
    imgs = [(s, png_bytes(s, sizes[s])) for s in (256, 64, 48, 32, 16)]
    header = struct.pack("<HHH", 0, 1, len(imgs))
    offset = 6 + 16 * len(imgs)
    entries = b""
    body = b""
    for s, data in imgs:
        entries += struct.pack("<BBBBHHII", 0 if s == 256 else s, 0 if s == 256 else s,
                               0, 0, 1, 32, len(data), offset)
        body += data
        offset += len(data)
    with open(out, "wb") as f:
        f.write(header + entries + body)
    print("icon written:", out)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "icon.ico")
