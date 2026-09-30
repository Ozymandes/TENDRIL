#!/usr/bin/env python3
"""Terminal-accurate PNG previews of the real remote-agents screen (dev tool).

Build-time only (needs Pillow). Renders like a modern phone terminal:
1:2 cells, box-drawing and block/quadrant elements drawn geometrically so
they tile seamlessly, everything else in a monospace font.

    preview.py [--cols 54] [--rows 36] [--out screen.png] [--masthead FILE.ans]

Without --masthead it captures bin/remote-agents' own render() output (so it
shows whatever masthead is integrated). With --masthead it composes that
asset + one blank row above the untouched selector.
"""
import argparse
import contextlib
import importlib.util
import io
import os
import re
import shutil
from importlib.machinery import SourceFileLoader

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
CLI = os.path.join(REPO, "bin", "remote-agents")

CELL_W, CELL_H = 20, 40                   # Termux-like ~1:2 cell
BG = (0, 0, 0)                            # Termux default background
FG = (255, 255, 255)                      # Termux default foreground
BASIC = [(0, 0, 0), (205, 49, 49), (13, 188, 121), (229, 229, 16),
         (36, 114, 200), (188, 63, 188), (17, 168, 205), (229, 229, 229)]
BRIGHT = [(102, 102, 102), (241, 76, 76), (35, 209, 139), (245, 245, 67),
          (59, 142, 234), (214, 112, 214), (41, 184, 219), (255, 255, 255)]
FONTS = ["/usr/share/fonts/TTF/JetBrainsMonoNerdFont-Regular.ttf",
         "/usr/share/fonts/liberation/LiberationMono-Regular.ttf"]

# quadrant bitmasks: TL=1 TR=2 BL=4 BR=8
QUAD = {"▘": 1, "▝": 2, "▖": 4, "▗": 8, "▀": 3, "▄": 12, "▌": 5, "▐": 10,
        "▛": 7, "▜": 11, "▙": 13, "▟": 14, "█": 15, "▚": 9, "▞": 6}
# box drawing: (up, down, left, right)
BOX = {"─": (0, 0, 1, 1), "│": (1, 1, 0, 0), "┌": (0, 1, 0, 1),
       "┐": (0, 1, 1, 0), "└": (1, 0, 0, 1), "┘": (1, 0, 1, 0),
       "├": (1, 1, 0, 1), "┤": (1, 1, 1, 0), "┬": (0, 1, 1, 1),
       "┴": (1, 0, 1, 1), "┼": (1, 1, 1, 1)}

TOKEN = re.compile(r"\x1b\[([0-9;:?]*)([@-~])|\r|\n|[^\x1b\r\n]")


def sgr(params, fg, bg):
    if params == "":
        return FG, BG
    p = [int(x) if x else 0 for x in re.split(r"[;:]", params)]
    i = 0
    while i < len(p):
        t = p[i]
        if t == 0:
            fg, bg = FG, BG
        elif t == 39:
            fg = FG
        elif t == 49:
            bg = BG
        elif 30 <= t <= 37:
            fg = BASIC[t - 30]
        elif 90 <= t <= 97:
            fg = BRIGHT[t - 90]
        elif 40 <= t <= 47:
            bg = BASIC[t - 40]
        elif 100 <= t <= 107:
            bg = BRIGHT[t - 100]
        elif t in (38, 48) and i + 4 < len(p) and p[i + 1] == 2:
            c = (p[i + 2], p[i + 3], p[i + 4])
            fg, bg = (c, bg) if t == 38 else (fg, c)
            i += 4
        i += 1
    return fg, bg


def parse(text, cols, rows):
    """Minimal VT: SGR, clear/home, CR/LF, autowrap at `cols` (so overflow shows)."""
    grid = [[(" ", FG, BG) for _ in range(cols)] for _ in range(rows)]
    x = y = 0
    fg, bg = FG, BG
    for m in TOKEN.finditer(text):
        tok = m.group(0)
        if m.group(2):
            if m.group(2) == "m":
                fg, bg = sgr(m.group(1), fg, bg)
            elif m.group(2) == "H":
                x = y = 0
            elif m.group(2) == "J":
                grid = [[(" ", FG, BG) for _ in range(cols)] for _ in range(rows)]
        elif tok == "\r":
            x = 0
        elif tok == "\n":
            x, y = 0, y + 1
        else:
            if x >= cols:
                x, y = 0, y + 1
            if y < rows:
                grid[y][x] = (tok, fg, bg)
            x += 1
    return grid


def draw(grid, path):
    rows, cols = len(grid), len(grid[0])
    img = Image.new("RGB", (cols * CELL_W, rows * CELL_H), BG)
    d = ImageDraw.Draw(img)
    font = next(ImageFont.truetype(f, 32) for f in FONTS if os.path.exists(f))
    lw = 2
    for y, row in enumerate(grid):
        for x, (ch, fg, bg) in enumerate(row):
            x0, y0 = x * CELL_W, y * CELL_H
            x1, y1 = x0 + CELL_W, y0 + CELL_H
            d.rectangle([x0, y0, x1 - 1, y1 - 1], fill=bg)
            if ch in QUAD:
                q, hw, hh = QUAD[ch], CELL_W // 2, CELL_H // 2
                for bit, (qx, qy) in ((1, (0, 0)), (2, (1, 0)), (4, (0, 1)), (8, (1, 1))):
                    if q & bit:
                        d.rectangle([x0 + qx * hw, y0 + qy * hh,
                                     x0 + (qx + 1) * hw - 1, y0 + (qy + 1) * hh - 1], fill=fg)
            elif ch in BOX:
                u, dn, l, r = BOX[ch]
                cx, cy = x0 + CELL_W // 2, y0 + CELL_H // 2
                if l:
                    d.rectangle([x0, cy - lw // 2, cx, cy + lw // 2 - 1], fill=fg)
                if r:
                    d.rectangle([cx - lw // 2, cy - lw // 2, x1 - 1, cy + lw // 2 - 1], fill=fg)
                if u:
                    d.rectangle([cx - lw // 2, y0, cx + lw // 2 - 1, cy], fill=fg)
                if dn:
                    d.rectangle([cx - lw // 2, cy - lw // 2, cx + lw // 2 - 1, y1 - 1], fill=fg)
            elif ch != " ":
                w = font.getlength(ch)
                d.text((x0 + (CELL_W - w) / 2, y0 + 2), ch, font=font, fill=fg)
    img.save(path)
    return path


def load_cli():
    loader = SourceFileLoader("remote_agents", CLI)
    spec = importlib.util.spec_from_loader("remote_agents", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    # previews always show the color path (import under a pipe disables it)
    mod.COLOR = True
    mod.C_RESET = "\033[0m"
    mod.C_LIME = "\033[38;2;203;253;117m"
    mod.C_TEAL = "\033[38;2;47;179;196m"
    mod.C_GRAY = "\033[38;2;112;128;136m"
    mod.C_WHITE = "\033[97m"
    mod.C_RED = "\033[38;2;232;92;111m"
    return mod


def capture(mod, cols, rows=36):
    """The CLI's real render() output at a stubbed terminal size."""
    real = shutil.get_terminal_size
    shutil.get_terminal_size = lambda fallback=(52, 24): os.terminal_size((cols, rows))
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            mod.render(mod.collect() or [], "")
    finally:
        shutil.get_terminal_size = real
    return buf.getvalue()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cols", type=int, default=54)
    ap.add_argument("--rows", type=int, default=36)
    ap.add_argument("--out", default="screen.png")
    ap.add_argument("--masthead")
    a = ap.parse_args()
    mod = load_cli()
    if a.masthead:                             # show the given asset instead
        mod.masthead = lambda width, body: []
    frame = capture(mod, a.cols, a.rows)
    if a.masthead:
        mast = open(a.masthead).read().rstrip("\n").split("\n")
        width = max(len(re.sub(r"\x1b\[[0-9;]*m", "", ln)) for ln in mast)
        pad = " " * max(0, (a.cols - width) // 2)
        head = "\n".join(pad + ln for ln in mast) + "\n\n"
        frame = frame.replace("\033[H\033[2J", "\033[H\033[2J" + head, 1)
    print(draw(parse(frame, a.cols, a.rows), a.out))


if __name__ == "__main__":
    main()
