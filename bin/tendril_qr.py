#!/usr/bin/env python3
"""tendril_qr — the pairing QR as terminal text, stdlib only (v0.2.1).

A first-time install must SHOW the pairing QR on a clean machine, so the
encoder is vendored (bin/tendril_qrcodegen.py, Project Nayuki, MIT) instead
of depending on `qrencode`. Rendering uses upper/lower half blocks — one
character per module column, two module rows per text line — drawn as dark
modules on a light field with a 2-module quiet zone, which phone cameras
read reliably on both dark and light terminals.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tendril_qrcodegen as qrg  # noqa: E402

QUIET = 2
# light field, dark modules: (top, bottom) dark? -> character
_CELLS = {
    (False, False): "\u2588",   # both light  -> full block (light ink)
    (True, False): "\u2584",    # top dark    -> lower half light
    (False, True): "\u2580",    # bottom dark -> upper half light
    (True, True): " ",          # both dark   -> background
}


def matrix(text, ecc=qrg.QrCode.Ecc.LOW):
    """The module matrix (list of rows of bool, True = dark), quiet zone
    included. LOW error correction keeps the version small enough for an
    80-column terminal; the code lives a few minutes on a screen."""
    qr = qrg.QrCode.encode_text(text, ecc)
    n = qr.get_size()
    size = n + 2 * QUIET
    return [[qr.get_module(x - QUIET, y - QUIET)
             if QUIET <= x < n + QUIET and QUIET <= y < n + QUIET else False
             for x in range(size)] for y in range(size)]


def render(text):
    """Text lines (no colour codes) drawing the QR; width == len(lines[0])."""
    m = matrix(text)
    if len(m) % 2:
        m.append([False] * len(m[0]))
    return ["".join(_CELLS[(m[y][x], m[y + 1][x])] for x in range(len(m[0])))
            for y in range(0, len(m), 2)]


if __name__ == "__main__":
    for line in render(sys.argv[1] if len(sys.argv) > 1 else "TENDRIL"):
        print(line)
