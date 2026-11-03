# -*- coding: utf-8 -*-
"""用 Win32 GDI 抓取指定窗口，输出 PNG（无需第三方库）。"""
import ctypes
import ctypes.wintypes as wt
import struct
import sys
import zlib

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
user32.SetProcessDPIAware()


class RECT(ctypes.Structure):
    _fields_ = [("left", wt.LONG), ("top", wt.LONG), ("right", wt.LONG), ("bottom", wt.LONG)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
                ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG),
                ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD),
                ("biClrImportant", wt.DWORD)]


def _chunk(tag, data):
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def write_png(path, w, h, bgra):
    out = bytearray(len(bgra))
    out[0::4] = bgra[2::4]
    out[1::4] = bgra[1::4]
    out[2::4] = bgra[0::4]
    out[3::4] = b"\xff" * (w * h)
    rb = w * 4
    raw = b"".join(b"\x00" + bytes(out[y * rb:(y + 1) * rb]) for y in range(h))
    png = (b"\x89PNG\r\n\x1a\n"
           + _chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
           + _chunk(b"IDAT", zlib.compress(raw, 6))
           + _chunk(b"IEND", b""))
    with open(path, "wb") as f:
        f.write(png)


def capture(hwnd, path, crop=None, scale=1):
    r = RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    w, h = r.right - r.left, r.bottom - r.top
    if w <= 0 or h <= 0:
        raise RuntimeError("window has no area")
    hdc = user32.GetDC(None)
    mdc = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    gdi32.SelectObject(mdc, bmp)
    # 优先用 PrintWindow 直接重绘窗口内容，即使被遮挡/最小化也能取到画面
    ok = False
    try:
        ok = bool(user32.PrintWindow(hwnd, mdc, 2))  # PW_RENDERFULLCONTENT
    except Exception:
        ok = False
    if not ok:
        gdi32.BitBlt(mdc, 0, 0, w, h, hdc, r.left, r.top, 0x00CC0020)
    bi = BITMAPINFOHEADER()
    bi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bi.biWidth = w
    bi.biHeight = -h
    bi.biPlanes = 1
    bi.biBitCount = 32
    bi.biCompression = 0
    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bi), 0)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mdc)
    user32.ReleaseDC(None, hdc)
    raw = bytes(buf.raw)

    if crop:
        cx, cy, cw, ch = crop
        cx = max(0, min(cx, w - 1)); cy = max(0, min(cy, h - 1))
        cw = min(cw, w - cx); ch = min(ch, h - cy)
        rows = []
        for y in range(cy, cy + ch):
            base = (y * w + cx) * 4
            rows.append(raw[base:base + cw * 4])
        raw, w, h = b"".join(rows), cw, ch

    if scale and scale > 1:
        s = int(scale)
        out = bytearray()
        for y in range(h):
            row = raw[y * w * 4:(y + 1) * w * 4]
            wide = bytearray()
            for x in range(w):
                px = row[x * 4:x * 4 + 4]
                wide.extend(px * s)
            for _ in range(s):
                out.extend(wide)
        raw, w, h = bytes(out), w * s, h * s

    write_png(path, w, h, raw)
    return w, h


if __name__ == "__main__":
    title = sys.argv[1] if len(sys.argv) > 1 else None
    out = sys.argv[2] if len(sys.argv) > 2 else "shot.png"
    crop = None
    scale = 1
    if len(sys.argv) > 3:
        crop = [int(v) for v in sys.argv[3].split(",")]
    if len(sys.argv) > 4:
        scale = int(sys.argv[4])
    hwnd = user32.FindWindowW(None, title) if title else user32.GetForegroundWindow()
    if not hwnd:
        print("window not found:", title)
        sys.exit(1)
    print("captured", capture(hwnd, out, crop, scale), "->", out)
