"""
surfaces.charts — daily bar charts as PNG, standard library only.

No matplotlib on the box (and CLAUDE.md 5 would want a reason for it), so
this is a small rasteriser: one series, one bar per calendar day.

Honesty rules, enforced here:
  - a day with no data gets NO bar and a small gray x on the baseline;
    nothing is interpolated or smoothed across gaps;
  - a partial day (not yet complete) is drawn in a lighter tint;
  - the y-axis starts at zero for summed metrics (steps, kcal) and at a
    padded min for rate metrics (bpm, ms), and the axis values are printed.
The caption (sent as Telegram text) carries title, units, range and gaps.
"""

from __future__ import annotations

import struct
import zlib

W, H = 720, 360
PAD_L, PAD_R, PAD_T, PAD_B = 70, 20, 20, 40
BG = (255, 255, 255)
GRID = (230, 232, 236)
AXIS = (120, 124, 132)
INK = (60, 64, 72)
BAR = (37, 99, 235)
BAR_PARTIAL = (147, 180, 245)
MISSING = (160, 164, 170)

# 3x5 bitmap font for the few glyphs needed on axes.
FONT = {
    "0": "111101101101111", "1": "010110010010111", "2": "111001111100111",
    "3": "111001111001111", "4": "101101111001001", "5": "111100111001111",
    "6": "111100111101111", "7": "111001001001001", "8": "111101111101111",
    "9": "111101111001111", ".": "000000000000010", "-": "000000111000000",
    ",": "000000000010100", "/": "001001010100100", " ": "000000000000000",
    "k": "100101110101101", "%": "101001010100101",
}


class Canvas:
    def __init__(self, w=W, h=H):
        self.w, self.h = w, h
        self.px = bytearray(bytes(BG) * (w * h))

    def rect(self, x0, y0, x1, y1, c):
        x0, x1 = max(0, int(min(x0, x1))), min(self.w, int(max(x0, x1)))
        y0, y1 = max(0, int(min(y0, y1))), min(self.h, int(max(y0, y1)))
        row = bytes(c) * (x1 - x0)
        for y in range(y0, y1):
            i = (y * self.w + x0) * 3
            self.px[i:i + len(row)] = row

    def text(self, x, y, s, c=INK, scale=2):
        for ch in str(s):
            g = FONT.get(ch)
            if g:
                for i, bit in enumerate(g):
                    if bit == "1":
                        cx, cy = x + (i % 3) * scale, y + (i // 3) * scale
                        self.rect(cx, cy, cx + scale, cy + scale, c)
            x += 4 * scale

    def png(self) -> bytes:
        raw = b"".join(b"\x00" + bytes(self.px[y * self.w * 3:(y + 1) * self.w * 3])
                       for y in range(self.h))

        def chunk(t, d):
            return (struct.pack(">I", len(d)) + t + d
                    + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff))
        return (b"\x89PNG\r\n\x1a\n"
                + chunk(b"IHDR", struct.pack(">IIBBBBB", self.w, self.h, 8, 2, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def _num(v):
    if abs(v) >= 10000:
        return f"{v / 1000:.0f}k"
    if abs(v) >= 100:
        return f"{v:.0f}"
    return f"{v:.1f}"


def bar_chart(points, zero_based=True) -> bytes:
    """points: [(iso day, value|None, complete)]. Returns PNG bytes."""
    c = Canvas()
    vals = [v for _, v, _ in points if v is not None]
    lo = 0.0 if (zero_based or not vals) else min(vals) * 0.9
    hi = max(vals) * 1.1 if vals else 1.0
    if hi <= lo:
        hi = lo + 1
    x0, x1, y0, y1 = PAD_L, W - PAD_R, PAD_T, H - PAD_B

    def ymap(v):
        return y1 - (v - lo) / (hi - lo) * (y1 - y0)

    for i in range(5):                                      # recessive grid + labels
        v = lo + (hi - lo) * i / 4
        y = ymap(v)
        c.rect(x0, y, x1, y + 1, GRID)
        c.text(6, int(y) - 5, _num(v), AXIS)
    c.rect(x0, y1, x1, y1 + 1, AXIS)
    n = max(1, len(points))
    slot = (x1 - x0) / n
    bw = max(2, min(28, slot * 0.6))
    every = 1 if n <= 10 else (5 if n <= 31 else 10)
    for i, (day, v, complete) in enumerate(points):
        cx = x0 + slot * (i + 0.5)
        if v is None:                                       # gap: an x, never a bar
            for k in range(-4, 5):
                c.rect(cx + k, y1 - 6 + k, cx + k + 2, y1 - 4 + k, MISSING)
                c.rect(cx + k, y1 - 6 - k, cx + k + 2, y1 - 4 - k, MISSING)
        else:
            c.rect(cx - bw / 2, ymap(v), cx + bw / 2, y1, BAR if complete else BAR_PARTIAL)
        if i % every == 0 or i == n - 1:
            md = f"{int(day[5:7])}/{int(day[8:10])}"
            c.text(int(cx - len(md) * 4), y1 + 10, md, AXIS)
    return c.png()
