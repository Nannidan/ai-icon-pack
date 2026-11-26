#!/usr/bin/env python3
"""Render every apps_catalog.json entry that has no raw_icons/<drawable>.png locally (0 Neurons).

Each icon is a bespoke vector emblem chosen from the app's identity (package / app_name /
prompt_subject), drawn with pycairo at 1024x1024, given neon bloom with Pillow, then
downsampled to a 512x512 RGB PNG. Pack rules enforced on every icon:
  * full-bleed square on uniform #050508
  * emblem strictly inside the central 58% safe zone (QA-checked after render)
  * active ICON_STYLE palette (cf.env), no text / letters / watermark / outer frame
"""
import argparse
import hashlib
import json
import math
import os
import re
import sys
from pathlib import Path

import cairo
from PIL import Image, ImageChops, ImageDraw, ImageFilter

PROJECT_DIR = Path(__file__).resolve().parent
CATALOG_FILE = PROJECT_DIR / "apps_catalog.json"
RAW_DIR = PROJECT_DIR / "raw_icons"
LOCAL_MANIFEST = RAW_DIR / ".local_rendered.json"
ENV_FILE = Path.home() / ".config" / "ai-icon-pack" / "cf.env"

SS = 1024            # supersampled canvas
OUT = 512            # final icon size
BG = (5, 5, 8)       # #050508
SAFE = 0.58          # inner safe zone fraction
TILE_HALF = 268      # px @1024 -> 536 px badge, inside the 594 px safe zone with bloom margin
PI = math.pi


def hexrgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


CYAN, MAGENTA, PINK, PURPLE, TEAL, BLUE = (hexrgb(x) for x in
                                           ("00F5FF", "FF007F", "FF00E5", "9D00FF", "00FFC8", "2F6BFF"))
STYLES = {
    "cyberpunk": dict(pairs=[(CYAN, MAGENTA), (MAGENTA, CYAN), (CYAN, PINK), (MAGENTA, TEAL),
                             (CYAN, PURPLE), (PINK, CYAN), (BLUE, MAGENTA), (PURPLE, CYAN)],
                      bloom=(1.0, 0.85, 0.55), body=((0.075, 0.075, 0.115), (0.03, 0.03, 0.05)), core=0.72),
    "3d": dict(pairs=[(hexrgb("C8D7E6"), hexrgb("6F8299")), (hexrgb("A0B9D2"), CYAN), (hexrgb("DCE3EA"), hexrgb("5A6E87"))],
               bloom=(0.45, 0.3, 0.15), body=((0.13, 0.135, 0.15), (0.045, 0.045, 0.055)), core=0.85),
    "painted": dict(pairs=[(hexrgb("FFAF41"), hexrgb("825FFF")), (hexrgb("00C2A8"), hexrgb("FF4F8B")),
                           (hexrgb("FFD166"), hexrgb("3A86FF"))],
                    bloom=(0.7, 0.5, 0.3), body=((0.09, 0.07, 0.1), (0.035, 0.03, 0.045)), core=0.6),
    "retro": dict(pairs=[(hexrgb("FF8C28"), hexrgb("00DCC8")), (hexrgb("FF00A0"), hexrgb("FF8C28")),
                         (hexrgb("00DCC8"), hexrgb("FF00A0"))],
                  bloom=(1.0, 0.8, 0.5), body=((0.09, 0.05, 0.12), (0.035, 0.02, 0.05)), core=0.7),
}


# ===================================================================== path helpers
def circ(c, x, y, r):
    c.new_sub_path(); c.arc(x, y, r, 0, 2 * PI)


def arcp(c, x, y, r, a1, a2):
    c.new_sub_path(); c.arc(x, y, r, a1, a2)


def ell(c, x, y, rx, ry, rot=0.0, a1=0.0, a2=2 * PI):
    c.save(); c.translate(x, y); c.rotate(rot); c.scale(rx, ry)
    c.new_sub_path(); c.arc(0, 0, 1, a1, a2); c.restore()


def rrect(c, x, y, w, h, r):
    r = min(r, w / 2, h / 2)
    c.new_sub_path()
    c.arc(x + w - r, y + r, r, -PI / 2, 0); c.arc(x + w - r, y + h - r, r, 0, PI / 2)
    c.arc(x + r, y + h - r, r, PI / 2, PI); c.arc(x + r, y + r, r, PI, 1.5 * PI); c.close_path()


def ln(c, *pts):
    c.move_to(*pts[0])
    for q in pts[1:]:
        c.line_to(*q)


def poly(c, pts):
    ln(c, *pts); c.close_path()


def ngon(c, x, y, r, n, rot=-PI / 2):
    poly(c, [(x + r * math.cos(rot + 2 * PI * i / n), y + r * math.sin(rot + 2 * PI * i / n)) for i in range(n)])


def star(c, x, y, r, n=5, inner=0.45, rot=-PI / 2):
    pts = []
    for i in range(2 * n):
        rr = r if i % 2 == 0 else r * inner
        a = rot + PI * i / n
        pts.append((x + rr * math.cos(a), y + rr * math.sin(a)))
    poly(c, pts)


def sparkle(c, x, y, r):
    k = 0.18 * r
    c.move_to(x, y - r); c.curve_to(x + k * 0.3, y - k, x + k, y - k * 0.3, x + r, y)
    c.curve_to(x + k, y + k * 0.3, x + k * 0.3, y + k, x, y + r)
    c.curve_to(x - k * 0.3, y + k, x - k, y + k * 0.3, x - r, y)
    c.curve_to(x - k, y - k * 0.3, x - k * 0.3, y - k, x, y - r); c.close_path()


def gear_path(c, x, y, r, n=8, depth=0.24):
    pts = []
    for i in range(n):
        a, w = 2 * PI * i / n, PI / n
        for da, rr in ((-w * 0.62, r), (-w * 0.34, r * (1 + depth)), (w * 0.34, r * (1 + depth)), (w * 0.62, r)):
            pts.append((x + rr * math.cos(a + da), y + rr * math.sin(a + da)))
    poly(c, pts)


def shield_path(c, s=1.0, y0=0.0):
    c.new_sub_path()
    c.move_to(0, -0.92 * s + y0)
    c.curve_to(0.3 * s, -0.74 * s + y0, 0.55 * s, -0.72 * s + y0, 0.78 * s, -0.7 * s + y0)
    c.curve_to(0.8 * s, 0.08 * s + y0, 0.52 * s, 0.6 * s + y0, 0, 0.95 * s + y0)
    c.curve_to(-0.52 * s, 0.6 * s + y0, -0.8 * s, 0.08 * s + y0, -0.78 * s, -0.7 * s + y0)
    c.curve_to(-0.55 * s, -0.72 * s + y0, -0.3 * s, -0.74 * s + y0, 0, -0.92 * s + y0); c.close_path()


def bubble_path(c, x, y, w, h, r, flip=False):
    c.new_sub_path()
    if not flip:
        c.move_to(x + r, y); c.line_to(x + w - r, y); c.arc(x + w - r, y + r, r, -PI / 2, 0)
        c.line_to(x + w, y + h - r); c.arc(x + w - r, y + h - r, r, 0, PI / 2)
        c.line_to(x + 0.46 * w, y + h); c.line_to(x + 0.14 * w, y + h + 0.3); c.line_to(x + 0.26 * w, y + h)
        c.line_to(x + r, y + h); c.arc(x + r, y + h - r, r, PI / 2, PI)
        c.line_to(x, y + r); c.arc(x + r, y + r, r, PI, 1.5 * PI)
    else:
        c.move_to(x + r, y); c.line_to(x + w - r, y); c.arc(x + w - r, y + r, r, -PI / 2, 0)
        c.line_to(x + w, y + h - r); c.arc(x + w - r, y + h - r, r, 0, PI / 2)
        c.line_to(x + 0.74 * w, y + h); c.line_to(x + 0.86 * w, y + h + 0.3); c.line_to(x + 0.54 * w, y + h)
        c.line_to(x + r, y + h); c.arc(x + r, y + h - r, r, PI / 2, PI)
        c.line_to(x, y + r); c.arc(x + r, y + r, r, PI, 1.5 * PI)
    c.close_path()


def cloud_path(c, x=0.0, y=0.0, s=1.0):
    c.new_sub_path()
    c.arc(x - 0.45 * s, y + 0.12 * s, 0.33 * s, 0.5 * PI, 1.35 * PI)
    c.arc(x + 0.02 * s, y - 0.12 * s, 0.45 * s, 1.1 * PI, 1.95 * PI)
    c.arc(x + 0.5 * s, y + 0.15 * s, 0.3 * s, 1.5 * PI, 2.5 * PI)
    c.close_path()


def heart_path(c, x=0.0, y=0.0, s=1.0):
    c.new_sub_path()
    c.move_to(x, y + 0.85 * s)
    c.curve_to(x - 0.95 * s, y + 0.15 * s, x - 0.9 * s, y - 0.75 * s, x - 0.45 * s, y - 0.75 * s)
    c.curve_to(x - 0.2 * s, y - 0.75 * s, x - 0.05 * s, y - 0.6 * s, x, y - 0.42 * s)
    c.curve_to(x + 0.05 * s, y - 0.6 * s, x + 0.2 * s, y - 0.75 * s, x + 0.45 * s, y - 0.75 * s)
    c.curve_to(x + 0.9 * s, y - 0.75 * s, x + 0.95 * s, y + 0.15 * s, x, y + 0.85 * s); c.close_path()


def crescent(c, r1, cx, cy, r2):
    d, phi = math.hypot(cx, cy), math.atan2(cy, cx)
    al = math.acos((d * d + r1 * r1 - r2 * r2) / (2 * d * r1))
    a1, a2 = phi + al, phi - al + 2 * PI
    c.new_sub_path(); c.arc(0, 0, r1, a1, a2)
    p2 = (r1 * math.cos(a2), r1 * math.sin(a2)); p1 = (r1 * math.cos(a1), r1 * math.sin(a1))
    b2, b1 = math.atan2(p2[1] - cy, p2[0] - cx), math.atan2(p1[1] - cy, p1[0] - cx)
    sweep = (b1 - b2) % (2 * PI)
    if math.cos(b2 + sweep / 2 - (phi + PI)) > 0:
        c.arc(cx, cy, r2, b2, b2 + sweep)
    else:
        c.arc_negative(cx, cy, r2, b2, b1)
    c.close_path()


# ===================================================================== pen
class Pen:
    """Strokes/fills the current path for the active pass (glow colour or white-hot core)."""

    def __init__(self, c, cols, angle, lw, core, core_mix):
        self.c, self.cols, self.angle, self.lw, self.core, self.mix = c, cols, angle, lw, core, core_mix

    def _src(self, alpha=1.0):
        a, b = self.cols
        if self.core:
            a = tuple(v + (1 - v) * self.mix for v in a)
            b = tuple(v + (1 - v) * self.mix for v in b)
        ca, sa = math.cos(self.angle), math.sin(self.angle)
        g = cairo.LinearGradient(-1.1 * ca, -1.1 * sa, 1.1 * ca, 1.1 * sa)
        g.add_color_stop_rgba(0, *a, alpha); g.add_color_stop_rgba(1, *b, alpha)
        return g

    def s(self, w=1.0):
        c = self.c
        c.set_source(self._src(0.9 if self.core else 1.0))
        c.set_line_width(self.lw * w * (0.36 if self.core else 1.0))
        c.stroke()

    def f(self, a=0.2):
        self.c.set_source(self._src(a * (0.35 if self.core else 1.0))); self.c.fill_preserve()

    def fs(self, a=0.2, w=1.0):
        self.f(a); self.s(w)

    def solid(self, a=1.0):
        self.c.set_source(self._src(a * (0.75 if self.core else 1.0))); self.c.fill()

    def knock(self):
        c = self.c
        c.save(); c.set_operator(cairo.OPERATOR_CLEAR); c.fill_preserve(); c.restore()

    def ks(self, a=0.0, w=1.0):
        """knock out what's underneath, optionally tint, then stroke."""
        self.knock()
        if a:
            self.f(a)
        self.s(w)


# ===================================================================== glyph library
G = {}


def glyph(name):
    def deco(fn):
        G[name] = fn
        return fn
    return deco


@glyph("play")
def _(c, p):
    poly(c, [(-0.48, -0.74), (0.8, 0), (-0.48, 0.74)]); p.fs(0.22)


@glyph("playhex")
def _(c, p):
    ngon(c, 0, 0, 0.95, 6, 0); p.fs(0.12)
    poly(c, [(-0.25, -0.4), (0.45, 0), (-0.25, 0.4)]); p.s()


@glyph("terminal")
def _(c, p):
    rrect(c, -0.95, -0.75, 1.9, 1.5, 0.2); p.fs(0.12)
    ln(c, (-0.55, -0.28), (-0.18, 0.04), (-0.55, 0.36)); p.s(1.15)
    ln(c, (0.02, 0.38), (0.52, 0.38)); p.s(1.15)


@glyph("gear")
def _(c, p):
    gear_path(c, 0, 0, 0.68); p.fs(0.15)
    circ(c, 0, 0, 0.27); p.s()


@glyph("shield")
def _(c, p):
    shield_path(c); p.fs(0.16)
    shield_path(c, 0.62, 0.02); p.s(0.55)


@glyph("shieldcheck")
def _(c, p):
    shield_path(c); p.fs(0.16)
    ln(c, (-0.32, 0.02), (-0.07, 0.28), (0.36, -0.24)); p.s(1.1)


@glyph("lock")
def _(c, p):
    c.move_to(-0.38, -0.05); c.line_to(-0.38, -0.35); c.arc(0, -0.35, 0.38, PI, 2 * PI); c.line_to(0.38, -0.05); p.s()
    rrect(c, -0.62, -0.08, 1.24, 0.98, 0.16); p.fs(0.2)
    circ(c, 0, 0.32, 0.1); p.solid(); ln(c, (0, 0.38), (0, 0.6)); p.s(0.8)


@glyph("key")
def _(c, p):
    c.save(); c.rotate(-PI / 4)
    circ(c, -0.5, 0, 0.34); p.fs(0.15); circ(c, -0.5, 0, 0.11); p.s(0.7)
    ln(c, (-0.16, 0), (0.92, 0)); p.s()
    ln(c, (0.58, 0), (0.58, 0.28)); p.s(); ln(c, (0.8, 0), (0.8, 0.34)); p.s()
    c.restore()


@glyph("folder")
def _(c, p):
    poly(c, [(-0.9, -0.62), (-0.32, -0.62), (-0.16, -0.44), (0.9, -0.44), (0.9, 0.7), (-0.9, 0.7)]); p.fs(0.14)
    ln(c, (-0.9, -0.2), (0.9, -0.2)); p.s(0.7)


@glyph("folder2")
def _(c, p):
    poly(c, [(-0.9, -0.62), (-0.32, -0.62), (-0.16, -0.44), (0.9, -0.44), (0.9, 0.7), (-0.9, 0.7)]); p.fs(0.1)
    poly(c, [(-0.9, 0.7), (-0.62, -0.12), (1.0, -0.12), (0.72, 0.7)]); p.ks(0.26)


@glyph("globe")
def _(c, p):
    circ(c, 0, 0, 0.86); p.fs(0.12)
    ell(c, 0, 0, 0.38, 0.86); p.s(0.7)
    ln(c, (-0.86, 0), (0.86, 0)); p.s(0.7)
    ln(c, (-0.72, -0.45), (0.72, -0.45)); p.s(0.55); ln(c, (-0.72, 0.45), (0.72, 0.45)); p.s(0.55)


@glyph("planet")
def _(c, p):
    circ(c, 0, 0, 0.55); p.fs(0.2)
    ell(c, 0, 0, 0.98, 0.3, -0.38, -0.02 * PI, 1.02 * PI); p.s()
    ell(c, 0, 0, 0.98, 0.3, -0.38, 1.12 * PI, 1.88 * PI); p.s(0.6)
    circ(c, 0.62, -0.62, 0.07); p.solid()


@glyph("compass")
def _(c, p):
    circ(c, 0, 0, 0.86); p.fs(0.1)
    c.save(); c.rotate(PI / 4)
    poly(c, [(0, -0.62), (0.18, 0), (0, 0.62), (-0.18, 0)]); p.fs(0.3)
    c.restore(); circ(c, 0, 0, 0.06); p.solid()


@glyph("chat")
def _(c, p):
    bubble_path(c, -0.88, -0.75, 1.76, 1.2, 0.34); p.fs(0.15)
    for x in (-0.38, 0, 0.38):
        circ(c, x, -0.15, 0.09); p.solid()


@glyph("chats")
def _(c, p):
    bubble_path(c, -0.92, -0.85, 1.25, 0.85, 0.24); p.fs(0.12)
    bubble_path(c, -0.25, -0.08, 1.15, 0.78, 0.22, flip=True); p.ks(0.2)


@glyph("plane")
def _(c, p):
    poly(c, [(-0.9, -0.02), (0.88, -0.72), (0.42, 0.74)]); p.fs(0.16)
    ln(c, (0.88, -0.72), (-0.12, 0.22), (-0.04, 0.7), (0.15, 0.36)); p.s(0.8)


@glyph("envelope")
def _(c, p):
    rrect(c, -0.9, -0.62, 1.8, 1.24, 0.14); p.fs(0.14)
    ln(c, (-0.84, -0.54), (0, 0.12), (0.84, -0.54)); p.s()


@glyph("handset")
def _(c, p):
    c.new_sub_path()
    c.move_to(-0.38, -0.9); c.line_to(-0.12, -0.5); c.line_to(-0.3, -0.3)
    c.curve_to(-0.18, -0.02, 0.02, 0.18, 0.3, 0.3); c.line_to(0.5, 0.12); c.line_to(0.9, 0.38)
    c.line_to(0.76, 0.76); c.curve_to(0.1, 0.98, -0.98, -0.1, -0.76, -0.76); c.close_path()
    p.fs(0.22)


@glyph("camera")
def _(c, p):
    rrect(c, -0.92, -0.5, 1.84, 1.22, 0.22); p.fs(0.12)
    ln(c, (-0.42, -0.5), (-0.28, -0.72), (0.28, -0.72), (0.42, -0.5)); p.s()
    circ(c, 0, 0.1, 0.4); p.s(); circ(c, 0, 0.1, 0.17); p.f(0.4); p.s(0.6)
    circ(c, 0.62, -0.28, 0.07); p.solid()


@glyph("aperture")
def _(c, p):
    circ(c, 0, 0, 0.86); p.fs(0.1)
    for i in range(6):
        a = PI / 3 * i
        ln(c, (0.86 * math.cos(a), 0.86 * math.sin(a)), (0.36 * math.cos(a + 2 * PI / 3), 0.36 * math.sin(a + 2 * PI / 3)))
        p.s(0.7)


@glyph("instacam")
def _(c, p):
    rrect(c, -0.85, -0.85, 1.7, 1.7, 0.45); p.fs(0.12)
    circ(c, 0, 0, 0.4); p.s(); circ(c, 0.5, -0.5, 0.08); p.solid()


@glyph("image")
def _(c, p):
    rrect(c, -0.9, -0.72, 1.8, 1.44, 0.18); p.fs(0.1)
    ln(c, (-0.75, 0.52), (-0.26, -0.05), (0.08, 0.3), (0.34, 0.08), (0.76, 0.52)); p.s()
    circ(c, 0.42, -0.34, 0.14); p.solid(0.9)


@glyph("brush")
def _(c, p):
    c.save(); c.rotate(PI / 4)
    rrect(c, -0.1, -0.98, 0.2, 0.9, 0.1); p.fs(0.2)
    rrect(c, -0.17, -0.1, 0.34, 0.2, 0.05); p.s()
    c.move_to(-0.17, 0.12); c.curve_to(-0.2, 0.45, -0.05, 0.75, 0.1, 0.95); c.curve_to(0.15, 0.6, 0.2, 0.35, 0.17, 0.12); c.close_path()
    p.fs(0.35)
    c.restore()


@glyph("palette")
def _(c, p):
    c.new_sub_path(); c.move_to(0.1, -0.82)
    c.curve_to(0.75, -0.82, 0.98, -0.25, 0.85, 0.15); c.curve_to(0.75, 0.45, 0.35, 0.3, 0.3, 0.55)
    c.curve_to(0.25, 0.85, -0.05, 0.9, -0.35, 0.8); c.curve_to(-0.95, 0.55, -0.98, -0.82, 0.1, -0.82); c.close_path()
    p.fs(0.12)
    for (x, y) in ((-0.5, -0.05), (-0.25, -0.45), (0.2, -0.5), (0.55, -0.2)):
        circ(c, x, y, 0.11); p.solid()


@glyph("eyedropper")
def _(c, p):
    c.save(); c.rotate(PI / 4)
    rrect(c, -0.2, -0.98, 0.4, 0.38, 0.18); p.fs(0.3)
    ln(c, (-0.32, -0.56), (0.32, -0.56)); p.s()
    poly(c, [(-0.12, -0.52), (0.12, -0.52), (0.08, 0.5), (0, 0.72), (-0.08, 0.5)]); p.fs(0.25)
    c.restore()
    circ(c, -0.62, 0.72, 0.09); p.solid()


@glyph("droplet")
def _(c, p):
    c.new_sub_path(); c.move_to(0, -0.92)
    c.curve_to(0.16, -0.56, 0.66, -0.12, 0.66, 0.28); c.arc(0, 0.28, 0.66, 0, PI)
    c.curve_to(-0.66, -0.12, -0.16, -0.56, 0, -0.92); c.close_path(); p.fs(0.18)
    arcp(c, 0, 0.28, 0.42, 0.62 * PI, 0.95 * PI); p.s(0.6)


@glyph("speaker")
def _(c, p):
    poly(c, [(-0.88, -0.28), (-0.46, -0.28), (0.02, -0.72), (0.02, 0.72), (-0.46, 0.28), (-0.88, 0.28)]); p.fs(0.2)
    arcp(c, 0.02, 0, 0.42, -PI / 4, PI / 4); p.s(); arcp(c, 0.02, 0, 0.74, -PI / 4, PI / 4); p.s()


@glyph("waveform")
def _(c, p):
    hs = [0.25, 0.55, 0.85, 0.45, 0.7, 0.35, 0.6, 0.3, 0.15]
    for i, h in enumerate(hs):
        x = -0.8 + i * 0.2
        ln(c, (x, -h), (x, h)); p.s(0.9)


@glyph("mic")
def _(c, p):
    rrect(c, -0.26, -0.88, 0.52, 1.02, 0.26); p.fs(0.2)
    arcp(c, 0, -0.14, 0.5, 0, PI); p.s()
    ln(c, (0, 0.36), (0, 0.66)); p.s(); ln(c, (-0.3, 0.66), (0.3, 0.66)); p.s()


@glyph("note")
def _(c, p):
    ln(c, (-0.3, 0.48), (-0.3, -0.62)); p.s(); ln(c, (0.58, 0.32), (0.58, -0.8)); p.s()
    poly(c, [(-0.3, -0.62), (0.58, -0.8), (0.58, -0.58), (-0.3, -0.4)]); p.solid()
    ell(c, -0.52, 0.5, 0.25, 0.19, -0.3); p.solid(); ell(c, 0.36, 0.34, 0.25, 0.19, -0.3); p.solid()


@glyph("headphones")
def _(c, p):
    arcp(c, 0, 0.12, 0.76, PI, 2 * PI); p.s()
    rrect(c, -0.94, 0.05, 0.34, 0.66, 0.14); p.fs(0.25); rrect(c, 0.6, 0.05, 0.34, 0.66, 0.14); p.fs(0.25)


@glyph("cloud")
def _(c, p):
    cloud_path(c, 0, 0.05, 1.12); p.fs(0.15)


@glyph("suncloud")
def _(c, p):
    circ(c, -0.25, -0.3, 0.34); p.fs(0.25)
    for i in range(8):
        a = PI / 4 * i
        ln(c, (-0.25 + 0.48 * math.cos(a), -0.3 + 0.48 * math.sin(a)), (-0.25 + 0.64 * math.cos(a), -0.3 + 0.64 * math.sin(a)))
        p.s(0.7)
    cloud_path(c, 0.18, 0.3, 0.88); p.ks(0.18)


@glyph("storm")
def _(c, p):
    cloud_path(c, 0, -0.25, 1.05); p.fs(0.15)
    poly(c, [(0.1, -0.05), (-0.25, 0.45), (0.0, 0.45), (-0.15, 0.95), (0.3, 0.32), (0.05, 0.32), (0.22, -0.05)]); p.ks(0.4)


@glyph("clock")
def _(c, p):
    circ(c, 0, 0, 0.86); p.fs(0.12)
    for i in range(4):
        a = PI / 2 * i
        ln(c, (0.66 * math.cos(a), 0.66 * math.sin(a)), (0.78 * math.cos(a), 0.78 * math.sin(a))); p.s(0.7)
    ln(c, (0, -0.5), (0, 0), (0.36, 0.22)); p.s()


@glyph("timer")
def _(c, p):
    circ(c, 0, 0.1, 0.76); p.fs(0.12)
    ln(c, (-0.16, -0.88), (0.16, -0.88)); p.s(); ln(c, (0, -0.88), (0, -0.66)); p.s()
    ln(c, (0.56, -0.5), (0.7, -0.64)); p.s()
    c.move_to(0, 0.1); c.arc(0, 0.1, 0.5, -PI / 2, PI / 6); c.close_path(); p.fs(0.3, 0.6)


@glyph("calendar")
def _(c, p):
    rrect(c, -0.85, -0.7, 1.7, 1.56, 0.18); p.fs(0.1)
    ln(c, (-0.85, -0.3), (0.85, -0.3)); p.s(0.8)
    ln(c, (-0.42, -0.9), (-0.42, -0.55)); p.s(); ln(c, (0.42, -0.9), (0.42, -0.55)); p.s()
    for i in range(3):
        for j in range(2):
            circ(c, -0.45 + 0.45 * i, 0.08 + 0.4 * j, 0.08); p.solid(1 if (i, j) != (2, 1) else 0.45)


@glyph("checklist")
def _(c, p):
    for k, y in enumerate((-0.6, 0, 0.6)):
        rrect(c, -0.88, y - 0.18, 0.36, 0.36, 0.08); p.fs(0.15, 0.7)
        if k < 2:
            ln(c, (-0.8, y), (-0.7, y + 0.1), (-0.5, y - 0.15)); p.s(0.8)
        ln(c, (-0.3, y), (0.85, y)); p.s()


@glyph("document")
def _(c, p):
    poly(c, [(-0.62, -0.88), (0.25, -0.88), (0.66, -0.47), (0.66, 0.88), (-0.62, 0.88)]); p.fs(0.12)
    ln(c, (0.25, -0.88), (0.25, -0.47), (0.66, -0.47)); p.s(0.8)
    for y, w in ((-0.15, 0.78), (0.15, 0.78), (0.45, 0.5)):
        ln(c, (-0.36, y), (-0.36 + w, y)); p.s(0.75)


@glyph("cart")
def _(c, p):
    ln(c, (-0.95, -0.7), (-0.68, -0.7), (-0.44, 0.35), (0.66, 0.35), (0.86, -0.4), (-0.6, -0.4)); p.s()
    circ(c, -0.3, 0.66, 0.12); p.solid(); circ(c, 0.5, 0.66, 0.12); p.solid()


@glyph("bag")
def _(c, p):
    poly(c, [(-0.68, -0.32), (0.68, -0.32), (0.8, 0.86), (-0.8, 0.86)]); p.fs(0.15)
    arcp(c, 0, -0.32, 0.32, PI, 2 * PI); p.s()


@glyph("card")
def _(c, p):
    rrect(c, -0.92, -0.58, 1.84, 1.16, 0.15); p.fs(0.12)
    ln(c, (-0.92, -0.24), (0.92, -0.24)); p.s(1.3)
    rrect(c, -0.66, 0.06, 0.34, 0.26, 0.05); p.f(0.4); p.s(0.6)
    ln(c, (0.2, 0.32), (0.66, 0.32)); p.s(0.7)


@glyph("cards")
def _(c, p):
    for k, y in enumerate((-0.8, -0.5, -0.2)):
        rrect(c, -0.88, y, 1.76, 1.0, 0.14); p.ks(0.08 + 0.06 * k, 0.9)
    circ(c, 0.45, 0.45, 0.13); p.s(0.7); circ(c, 0.62, 0.45, 0.13); p.s(0.7)


@glyph("coins")
def _(c, p):
    for y in (0.5, 0.18, -0.14):
        ell(c, 0, y, 0.72, 0.26); p.ks(0.12, 0.9)
        ln(c, (-0.72, y), (-0.72, y + 0.2)); p.s(0.9); ln(c, (0.72, y), (0.72, y + 0.2)); p.s(0.9)
    ell(c, 0, -0.46, 0.72, 0.26); p.ks(0.2); ell(c, 0, -0.46, 0.42, 0.14); p.s(0.6)


@glyph("wallet")
def _(c, p):
    rrect(c, -0.9, -0.55, 1.8, 1.3, 0.18); p.fs(0.12)
    ln(c, (-0.7, -0.55), (0.45, -0.85), (0.6, -0.55)); p.s(0.8)
    rrect(c, 0.3, -0.05, 0.6, 0.4, 0.12); p.ks(0.2); circ(c, 0.5, 0.15, 0.07); p.solid()


@glyph("bank")
def _(c, p):
    poly(c, [(-0.92, -0.38), (0, -0.92), (0.92, -0.38)]); p.fs(0.18)
    for x in (-0.6, -0.2, 0.2, 0.6):
        ln(c, (x, -0.2), (x, 0.55)); p.s()
    ln(c, (-0.9, 0.76), (0.9, 0.76)); p.s(1.2)


@glyph("chartup")
def _(c, p):
    ln(c, (-0.86, -0.86), (-0.86, 0.8), (0.9, 0.8)); p.s(0.8)
    ln(c, (-0.6, 0.42), (-0.2, 0.0), (0.1, 0.22), (0.68, -0.5)); p.s(1.1)
    ln(c, (0.36, -0.52), (0.7, -0.52), (0.7, -0.18)); p.s(1.1)


@glyph("barchart")
def _(c, p):
    for i, h in enumerate((0.7, 1.2, 0.9, 1.55)):
        x = -0.82 + i * 0.44
        rrect(c, x, 0.8 - h, 0.3, h, 0.08); p.fs(0.15 + 0.08 * i)


@glyph("piechart")
def _(c, p):
    c.move_to(0, 0); c.arc(0, 0, 0.8, 0, 1.5 * PI); c.close_path(); p.fs(0.15)
    c.move_to(0.1, -0.1); c.arc(0.1, -0.1, 0.8, -PI / 2, 0); c.close_path(); p.fs(0.35)


@glyph("gauge")
def _(c, p):
    arcp(c, 0, 0.22, 0.82, 0.8 * PI, 2.2 * PI); p.s()
    for i in range(5):
        a = 0.85 * PI + i * 0.325 * PI
        ln(c, (0.6 * math.cos(a), 0.22 + 0.6 * math.sin(a)), (0.7 * math.cos(a), 0.22 + 0.7 * math.sin(a))); p.s(0.6)
    ln(c, (0, 0.22), (0.38, -0.28)); p.s(1.1); circ(c, 0, 0.22, 0.11); p.solid()


@glyph("cursor")
def _(c, p):
    poly(c, [(-0.5, -0.88), (0.58, 0.14), (0.06, 0.2), (0.32, 0.74), (0.12, 0.84), (-0.13, 0.3), (-0.5, 0.64)]); p.fs(0.25)


@glyph("tap")
def _(c, p):
    circ(c, 0, -0.3, 0.16); p.solid(); circ(c, 0, -0.3, 0.38); p.s(0.7)
    arcp(c, 0, -0.3, 0.62, 1.05 * PI, 1.95 * PI); p.s(0.5)
    rrect(c, -0.14, -0.28, 0.28, 1.16, 0.14); p.ks(0.2)


@glyph("gesture")
def _(c, p):
    arcp(c, 0, 0.25, 0.78, 1.12 * PI, 1.88 * PI); p.s()
    a1, a2 = 1.12 * PI, 1.88 * PI
    for a, sgn in ((a1, 1), (a2, -1)):
        x, y = 0.78 * math.cos(a), 0.25 + 0.78 * math.sin(a)
        ln(c, (x + sgn * 0.05, y - 0.28), (x, y), (x + sgn * 0.28, y + 0.05)); p.s()
    circ(c, 0, 0.3, 0.15); p.solid(); circ(c, 0, 0.3, 0.32); p.s(0.6)


@glyph("wifi")
def _(c, p):
    for r in (0.34, 0.68, 1.02):
        arcp(c, 0, 0.58, r, 1.25 * PI, 1.75 * PI); p.s()
    circ(c, 0, 0.58, 0.11); p.solid()


@glyph("tower")
def _(c, p):
    ln(c, (-0.36, 0.88), (0, -0.28), (0.36, 0.88)); p.s(); ln(c, (-0.22, 0.38), (0.22, 0.38)); p.s(0.7)
    circ(c, 0, -0.42, 0.1); p.solid()
    for r in (0.34, 0.6):
        arcp(c, 0, -0.42, r, -0.28 * PI, 0.18 * PI); p.s(0.8); arcp(c, 0, -0.42, r, 0.82 * PI, 1.28 * PI); p.s(0.8)


@glyph("signal")
def _(c, p):
    for i, h in enumerate((0.45, 0.8, 1.15, 1.5)):
        x = -0.8 + i * 0.44
        rrect(c, x, 0.78 - h, 0.28, h, 0.07); p.fs(0.15 + 0.1 * i)


@glyph("nfc")
def _(c, p):
    rrect(c, -0.78, -0.86, 1.56, 1.72, 0.22); p.fs(0.08)
    for r in (0.25, 0.48, 0.71):
        arcp(c, -0.42, 0, r, -PI / 3, PI / 3); p.s()


@glyph("radar")
def _(c, p):
    circ(c, 0, 0, 0.86); p.s(); circ(c, 0, 0, 0.52); p.s(0.6); circ(c, 0, 0, 0.12); p.solid()
    c.move_to(0, 0); c.arc(0, 0, 0.86, -PI / 2, -PI / 8); c.close_path(); p.f(0.35)
    circ(c, 0.42, -0.35, 0.07); p.solid(); circ(c, -0.35, 0.45, 0.06); p.solid(0.7)


@glyph("download")
def _(c, p):
    ln(c, (0, -0.86), (0, 0.24)); p.s(); ln(c, (-0.38, -0.14), (0, 0.26), (0.38, -0.14)); p.s()
    ln(c, (-0.82, 0.22), (-0.82, 0.76), (0.82, 0.76), (0.82, 0.22)); p.s()


@glyph("box")
def _(c, p):
    poly(c, [(0, -0.86), (0.8, -0.46), (0, -0.06), (-0.8, -0.46)]); p.fs(0.22)
    ln(c, (-0.8, -0.46), (-0.8, 0.46), (0, 0.88), (0.8, 0.46), (0.8, -0.46)); p.s(); ln(c, (0, -0.06), (0, 0.88)); p.s()
    ln(c, (-0.4, -0.66), (0.4, -0.26)); p.s(0.6)


@glyph("tiltcube")
def _(c, p):
    c.save(); c.rotate(0.26)
    rrect(c, -0.74, -0.74, 1.48, 1.48, 0.1); p.fs(0.2)
    rrect(c, -0.2, -0.2, 0.4, 0.4, 0.04); p.ks(0, 0.8)
    c.restore()


@glyph("widgets")
def _(c, p):
    rrect(c, -0.86, -0.86, 0.76, 0.76, 0.18); p.fs(0.2)
    circ(c, 0.48, -0.48, 0.38); p.fs(0.12)
    rrect(c, -0.86, 0.1, 0.76, 0.76, 0.18); p.fs(0.12)
    rrect(c, 0.1, 0.1, 0.76, 0.76, 0.18); p.fs(0.28)


@glyph("grid")
def _(c, p):
    for i in range(3):
        for j in range(3):
            rrect(c, -0.86 + i * 0.6, -0.86 + j * 0.6, 0.48, 0.48, 0.12); p.fs(0.1 + 0.05 * ((i + j) % 3), 0.75)


@glyph("iconpack")
def _(c, p):
    rrect(c, -0.86, -0.86, 0.76, 0.76, 0.22); p.fs(0.2)
    rrect(c, -0.86, 0.1, 0.76, 0.76, 0.22); p.fs(0.12)
    rrect(c, 0.1, 0.1, 0.76, 0.76, 0.22); p.fs(0.25)
    sparkle(c, 0.48, -0.48, 0.42); p.fs(0.35)


@glyph("pixelgrid")
def _(c, p):
    pattern = ["0110", "1111", "1111", "0110"]
    alphas = [[0.2, 0.9, 0.6, 0.2], [0.7, 0.35, 0.95, 0.5], [0.45, 0.85, 0.3, 0.8], [0.2, 0.6, 0.9, 0.2]]
    for j, row in enumerate(pattern):
        for i, v in enumerate(row):
            if v == "1":
                rrect(c, -0.84 + i * 0.43, -0.84 + j * 0.43, 0.36, 0.36, 0.06); p.solid(alphas[j][i])


@glyph("venn")
def _(c, p):
    for a in (-PI / 2, PI / 6, 5 * PI / 6):
        circ(c, 0.36 * math.cos(a), 0.36 * math.sin(a) + 0.04, 0.5); p.fs(0.14, 0.85)


@glyph("sparkle4")
def _(c, p):
    sparkle(c, -0.08, 0.08, 0.86); p.fs(0.25)
    sparkle(c, 0.62, -0.62, 0.24); p.solid(0.9)


@glyph("starburst")
def _(c, p):
    lens = [0.88, 0.7, 0.82, 0.66, 0.9, 0.72, 0.8, 0.68, 0.86, 0.7, 0.84, 0.64]
    for i, L in enumerate(lens):
        a = 2 * PI * i / 12 - PI / 2
        ln(c, (0.14 * math.cos(a), 0.14 * math.sin(a)), (L * math.cos(a), L * math.sin(a))); p.s(1.25)


@glyph("knot")
def _(c, p):
    for k in range(3):
        ell(c, 0, 0, 0.86, 0.36, k * PI / 3); p.s(0.85)
    circ(c, 0, 0, 0.12); p.solid()


@glyph("controller")
def _(c, p):
    c.new_sub_path(); c.move_to(-0.55, -0.45); c.line_to(0.55, -0.45)
    c.curve_to(0.95, -0.45, 1.0, 0.6, 0.75, 0.66); c.curve_to(0.55, 0.7, 0.45, 0.35, 0.3, 0.3); c.line_to(-0.3, 0.3)
    c.curve_to(-0.45, 0.35, -0.55, 0.7, -0.75, 0.66); c.curve_to(-1.0, 0.6, -0.95, -0.45, -0.55, -0.45); c.close_path()
    p.fs(0.15)
    ln(c, (-0.62, -0.08), (-0.3, -0.08)); p.s(0.8); ln(c, (-0.46, -0.24), (-0.46, 0.08)); p.s(0.8)
    circ(c, 0.38, -0.2, 0.07); p.solid(); circ(c, 0.56, -0.02, 0.07); p.solid()


@glyph("ruler")
def _(c, p):
    c.save(); c.rotate(-PI / 4)
    rrect(c, -0.98, -0.28, 1.96, 0.56, 0.08); p.fs(0.14)
    for i in range(-4, 5):
        L = 0.24 if i % 2 == 0 else 0.13
        ln(c, (i * 0.2, -0.28), (i * 0.2, -0.28 + L)); p.s(0.6)
    c.restore()


@glyph("battery")
def _(c, p):
    rrect(c, -0.42, -0.74, 0.84, 1.6, 0.14); p.fs(0.1)
    ln(c, (-0.16, -0.88), (0.16, -0.88)); p.s(1.3)
    poly(c, [(0.08, -0.5), (-0.24, 0.12), (0.0, 0.12), (-0.08, 0.6), (0.24, -0.04), (0.0, -0.04)]); p.fs(0.5)


@glyph("bolt")
def _(c, p):
    poly(c, [(0.14, -0.95), (-0.52, 0.12), (-0.04, 0.12), (-0.16, 0.95), (0.52, -0.15), (0.05, -0.15)]); p.fs(0.3)


@glyph("moon")
def _(c, p):
    crescent(c, 0.82, 0.42, -0.28, 0.66); p.fs(0.2)
    sparkle(c, 0.55, 0.45, 0.18); p.solid(0.85); circ(c, 0.25, -0.65, 0.05); p.solid(0.7)


@glyph("eye")
def _(c, p):
    c.new_sub_path(); c.move_to(-0.95, 0); c.curve_to(-0.5, -0.62, 0.5, -0.62, 0.95, 0)
    c.curve_to(0.5, 0.62, -0.5, 0.62, -0.95, 0); c.close_path(); p.fs(0.1)
    circ(c, 0, 0, 0.32); p.s(); circ(c, 0, 0, 0.13); p.solid()


@glyph("fingerprint")
def _(c, p):
    for i, r in enumerate((0.18, 0.36, 0.54, 0.72, 0.9)):
        arcp(c, 0, 0.12, r, PI * (1.0 + 0.04 * i), PI * (2.0 - 0.03 * i)); p.s(0.75)
        if i < 4:
            arcp(c, 0, 0.12, r, PI * (0.05 + 0.06 * i), PI * (0.55 - 0.05 * i)); p.s(0.75)


@glyph("house")
def _(c, p):
    ln(c, (-0.86, -0.02), (0, -0.82), (0.86, -0.02)); p.s()
    ln(c, (-0.62, -0.22), (-0.62, 0.82), (0.62, 0.82), (0.62, -0.22)); p.s()
    rrect(c, -0.16, 0.3, 0.32, 0.52, 0.06); p.fs(0.3, 0.8)


@glyph("bus")
def _(c, p):
    rrect(c, -0.66, -0.86, 1.32, 1.5, 0.22); p.fs(0.1)
    rrect(c, -0.5, -0.62, 1.0, 0.56, 0.08); p.fs(0.22, 0.8)
    circ(c, -0.38, 0.34, 0.08); p.solid(); circ(c, 0.38, 0.34, 0.08); p.solid()
    ln(c, (-0.44, 0.66), (-0.44, 0.9)); p.s(1.2); ln(c, (0.44, 0.66), (0.44, 0.9)); p.s(1.2)


@glyph("cup")
def _(c, p):
    poly(c, [(-0.6, -0.42), (0.48, -0.42), (0.36, 0.78), (-0.48, 0.78)]); p.fs(0.15)
    arcp(c, 0.46, 0.02, 0.26, -PI / 2, PI / 2); p.s()
    ln(c, (-0.68, -0.42), (0.56, -0.42)); p.s(1.1)
    for x in (-0.3, 0.05):
        c.move_to(x, -0.58); c.curve_to(x - 0.14, -0.72, x + 0.14, -0.8, x, -0.95); p.s(0.7)


@glyph("pill")
def _(c, p):
    c.save(); c.rotate(-PI / 4)
    rrect(c, -0.82, -0.3, 1.64, 0.6, 0.3); p.fs(0.1)
    c.new_sub_path(); c.arc(-0.52, 0, 0.3, PI / 2, 1.5 * PI); c.line_to(0, -0.3); c.line_to(0, 0.3); c.close_path(); p.f(0.35)
    ln(c, (0, -0.3), (0, 0.3)); p.s(0.8)
    c.restore()
    ln(c, (0.6, -0.86), (0.6, -0.46)); p.s(0.8); ln(c, (0.4, -0.66), (0.8, -0.66)); p.s(0.8)


@glyph("heart")
def _(c, p):
    heart_path(c, 0, 0.02, 1.0); p.fs(0.18)
    heart_path(c, 0, 0.0, 0.55); p.s(0.55)


@glyph("heartpulse")
def _(c, p):
    heart_path(c, 0, 0.02, 1.0); p.fs(0.14)
    ln(c, (-0.72, -0.05), (-0.32, -0.05), (-0.16, -0.36), (0.05, 0.26), (0.2, -0.05), (0.72, -0.05)); p.s(0.9)


@glyph("bug")
def _(c, p):
    ell(c, 0, 0.18, 0.46, 0.6); p.fs(0.15)
    circ(c, 0, -0.55, 0.22); p.fs(0.25)
    ln(c, (0, -0.32), (0, 0.76)); p.s(0.6)
    for y, dx in ((-0.05, 0.36), (0.25, 0.4), (0.55, 0.34)):
        ln(c, (-0.44, y), (-0.44 - dx, y - 0.12)); p.s(0.8); ln(c, (0.44, y), (0.44 + dx, y - 0.12)); p.s(0.8)
    ln(c, (-0.12, -0.74), (-0.3, -0.94)); p.s(0.7); ln(c, (0.12, -0.74), (0.3, -0.94)); p.s(0.7)


@glyph("wrench")
def _(c, p):
    c.save(); c.rotate(-PI / 4)
    rrect(c, -0.12, -0.22, 0.24, 1.14, 0.12); p.fs(0.2)
    c.new_sub_path(); c.arc(0, -0.55, 0.36, -PI / 2 + 0.55, -PI / 2 - 0.55 + 2 * PI)
    c.line_to(-0.11, -0.5); c.line_to(0.11, -0.5); c.close_path(); p.ks(0.2)
    c.restore()


@glyph("magnifier")
def _(c, p):
    circ(c, -0.16, -0.16, 0.56); p.fs(0.12)
    ln(c, (0.25, 0.25), (0.8, 0.8)); p.s(1.6)


@glyph("link")
def _(c, p):
    c.save(); c.rotate(-PI / 4)
    rrect(c, -0.9, -0.26, 1.0, 0.52, 0.26); p.s()
    rrect(c, -0.1, -0.26, 1.0, 0.52, 0.26); p.s()
    c.restore()


@glyph("tv")
def _(c, p):
    rrect(c, -0.92, -0.68, 1.84, 1.18, 0.15); p.fs(0.12)
    ln(c, (-0.3, -0.68), (0, -0.92), (0.3, -0.68)); p.s(0.8)
    ln(c, (-0.36, 0.76), (0.36, 0.76)); p.s()


@glyph("monitor")
def _(c, p):
    rrect(c, -0.92, -0.72, 1.84, 1.2, 0.12); p.fs(0.1)
    ln(c, (0, 0.48), (0, 0.76)); p.s(); ln(c, (-0.4, 0.78), (0.4, 0.78)); p.s()


@glyph("cast")
def _(c, p):
    ln(c, (-0.86, -0.22), (-0.86, -0.66), (0.86, -0.66), (0.86, 0.66), (0.16, 0.66)); p.s()
    for r in (0.32, 0.62, 0.92):
        arcp(c, -0.86, 0.66, r, -PI / 2, 0); p.s(0.9)
    circ(c, -0.86, 0.66, 0.1); p.solid()


@glyph("remote")
def _(c, p):
    rrect(c, -0.34, -0.94, 0.68, 1.88, 0.26); p.fs(0.1)
    circ(c, 0, -0.42, 0.2); p.s(0.8); circ(c, 0, -0.42, 0.06); p.solid()
    for y in (0.12, 0.38, 0.64):
        circ(c, -0.13, y, 0.06); p.solid(); circ(c, 0.13, y, 0.06); p.solid()


@glyph("keyboard")
def _(c, p):
    rrect(c, -0.96, -0.56, 1.92, 1.12, 0.16); p.fs(0.1)
    for j, n in enumerate((6, 5)):
        for i in range(n):
            x = -0.7 + i * 0.28 + j * 0.14
            rrect(c, x - 0.08, -0.34 + j * 0.28 - 0.08, 0.16, 0.16, 0.04); p.solid(0.85)
    ln(c, (-0.42, 0.3), (0.42, 0.3)); p.s()


@glyph("translate")
def _(c, p):
    bubble_path(c, -0.94, -0.88, 1.12, 0.86, 0.22); p.fs(0.15)
    circ(c, -0.38, -0.45, 0.24); p.s(0.65); ell(c, -0.38, -0.45, 0.11, 0.24); p.s(0.55)
    ln(c, (-0.62, -0.45), (-0.14, -0.45)); p.s(0.55)
    bubble_path(c, -0.18, -0.12, 1.12, 0.86, 0.22, flip=True); p.ks(0.22)
    ln(c, (0.14, 0.22), (0.62, 0.22)); p.s(0.7); ln(c, (0.48, 0.1), (0.62, 0.22), (0.48, 0.34)); p.s(0.7)
    ln(c, (0.62, 0.46), (0.14, 0.46)); p.s(0.7); ln(c, (0.28, 0.34), (0.14, 0.46), (0.28, 0.58)); p.s(0.7)


@glyph("archive")
def _(c, p):
    rrect(c, -0.82, -0.78, 1.64, 0.38, 0.08); p.fs(0.2)
    ln(c, (-0.72, -0.4), (-0.72, 0.86), (0.72, 0.86), (0.72, -0.4)); p.s()
    for k in range(6):
        y = -0.3 + k * 0.16
        ln(c, (-0.08 if k % 2 else 0.0, y), (0.0 if k % 2 else 0.08, y)); p.s(0.8)
    rrect(c, -0.14, 0.62, 0.28, 0.14, 0.04); p.solid()


@glyph("sync")
def _(c, p):
    arcp(c, 0, 0, 0.72, 1.08 * PI, 1.85 * PI); p.s()
    arcp(c, 0, 0, 0.72, 0.08 * PI, 0.85 * PI); p.s()
    for a, d in ((1.85 * PI, 1), (0.85 * PI, 1)):
        x, y = 0.72 * math.cos(a), 0.72 * math.sin(a)
        tx, ty = -math.sin(a) * d, math.cos(a) * d
        ln(c, (x - 0.26 * tx - 0.2 * ty, y - 0.26 * ty + 0.2 * tx), (x, y), (x - 0.26 * tx + 0.2 * ty, y - 0.26 * ty - 0.2 * tx)); p.s()


@glyph("transfer")
def _(c, p):
    rrect(c, -0.92, -0.62, 0.62, 1.24, 0.12); p.fs(0.12)
    rrect(c, 0.3, -0.62, 0.62, 1.24, 0.12); p.fs(0.22)
    ln(c, (-0.2, -0.22), (0.2, -0.22)); p.s(0.8); ln(c, (0.08, -0.34), (0.2, -0.22), (0.08, -0.1)); p.s(0.8)
    ln(c, (0.2, 0.22), (-0.2, 0.22)); p.s(0.8); ln(c, (-0.08, 0.1), (-0.2, 0.22), (-0.08, 0.34)); p.s(0.8)


@glyph("trash")
def _(c, p):
    ln(c, (-0.76, -0.6), (0.76, -0.6)); p.s()
    ln(c, (-0.26, -0.6), (-0.2, -0.82), (0.2, -0.82), (0.26, -0.6)); p.s()
    poly(c, [(-0.6, -0.44), (0.6, -0.44), (0.48, 0.86), (-0.48, 0.86)]); p.fs(0.12)
    ln(c, (-0.2, -0.2), (-0.17, 0.6)); p.s(0.7); ln(c, (0.2, -0.2), (0.17, 0.6)); p.s(0.7)


@glyph("broom")
def _(c, p):
    ln(c, (0.78, -0.92), (0.06, -0.06)); p.s()
    poly(c, [(-0.12, -0.16), (0.24, 0.16), (-0.18, 0.9), (-0.88, 0.48)]); p.fs(0.2)
    for t in (0.3, 0.55, 0.8):
        ln(c, (-0.12 + (0.24 + 0.12) * t * 0.5, -0.16 + 0.32 * t * 0.5), (-0.88 + 0.7 * t, 0.48 + 0.42 * t)); p.s(0.5)
    sparkle(c, 0.55, 0.42, 0.2); p.solid(0.9); sparkle(c, -0.55, -0.55, 0.14); p.solid(0.7)


@glyph("plug")
def _(c, p):
    rrect(c, -0.42, -0.38, 0.84, 0.68, 0.18); p.fs(0.2)
    ln(c, (-0.18, -0.38), (-0.18, -0.82)); p.s(); ln(c, (0.18, -0.38), (0.18, -0.82)); p.s()
    c.move_to(0, 0.3); c.curve_to(0, 0.72, 0.5, 0.6, 0.5, 0.95); p.s()


@glyph("ghost")
def _(c, p):
    c.new_sub_path(); c.move_to(-0.7, 0.85); c.line_to(-0.7, -0.18); c.arc(0, -0.18, 0.7, PI, 2 * PI)
    c.line_to(0.7, 0.85); c.line_to(0.47, 0.66); c.line_to(0.23, 0.85); c.line_to(0, 0.66)
    c.line_to(-0.23, 0.85); c.line_to(-0.47, 0.66); c.close_path(); p.fs(0.15)
    ell(c, -0.24, -0.2, 0.09, 0.14); p.solid(); ell(c, 0.24, -0.2, 0.09, 0.14); p.solid()


@glyph("egg")
def _(c, p):
    ell(c, 0, 0.06, 0.74, 0.92); p.fs(0.12)
    rrect(c, -0.4, -0.42, 0.8, 0.62, 0.12); p.fs(0.2, 0.8)
    for (x, y) in ((-0.1, -0.2), (0.1, -0.2), (-0.15, -0.06), (0.0, -0.06), (0.15, -0.06), (0.0, 0.06)):
        rrect(c, x - 0.05, y - 0.05, 0.1, 0.1, 0.02); p.solid()
    for x in (-0.28, 0, 0.28):
        circ(c, x, 0.5, 0.08); p.solid(0.85)


@glyph("newspaper")
def _(c, p):
    rrect(c, -0.86, -0.72, 1.56, 1.44, 0.1); p.fs(0.1)
    ln(c, (0.7, -0.36), (0.94, -0.36), (0.94, 0.6)); p.s(0.8)
    rrect(c, -0.62, -0.48, 0.52, 0.42, 0.05); p.fs(0.3, 0.6)
    for y in (-0.42, -0.2):
        ln(c, (0.04, y), (0.46, y)); p.s(0.6)
    for y in (0.1, 0.3, 0.5):
        ln(c, (-0.62, y), (0.46, y)); p.s(0.6)


@glyph("pen")
def _(c, p):
    c.save(); c.translate(0.2, -0.2); c.rotate(PI / 4)
    poly(c, [(0, -0.75), (0.3, -0.05), (0, 0.6), (-0.3, -0.05)]); p.fs(0.2)
    ln(c, (0, 0.6), (0, 0.05)); p.s(0.6); circ(c, 0, 0.02, 0.07); p.solid()
    c.restore()
    c.move_to(-0.88, 0.78); c.curve_to(-0.62, 0.42, -0.46, 1.0, -0.22, 0.7); c.curve_to(-0.02, 0.48, 0.14, 0.92, 0.42, 0.74); p.s(0.8)


@glyph("pin")
def _(c, p):
    c.new_sub_path(); c.move_to(0, 0.92); c.curve_to(-0.36, 0.42, -0.66, 0.06, -0.66, -0.24)
    c.arc(0, -0.24, 0.66, PI, 2 * PI); c.curve_to(0.66, 0.06, 0.36, 0.42, 0, 0.92); c.close_path(); p.fs(0.16)
    circ(c, 0, -0.24, 0.24); p.s(0.8)


@glyph("person")
def _(c, p):
    circ(c, 0, -0.45, 0.32); p.fs(0.2)
    c.new_sub_path(); c.move_to(-0.7, 0.8); c.curve_to(-0.7, 0.04, 0.7, 0.04, 0.7, 0.8); c.close_path(); p.fs(0.15)


@glyph("people")
def _(c, p):
    for x, s in ((-0.55, 0.7), (0.55, 0.7)):
        circ(c, x, -0.28, 0.24 * s / 0.7 * 0.8); p.fs(0.12, 0.8)
        c.new_sub_path(); c.move_to(x - 0.42, 0.7); c.curve_to(x - 0.42, 0.12, x + 0.42, 0.12, x + 0.42, 0.7); c.close_path(); p.fs(0.1, 0.8)
    circ(c, 0, -0.42, 0.3); p.ks(0.22)
    c.new_sub_path(); c.move_to(-0.6, 0.86); c.curve_to(-0.6, 0.0, 0.6, 0.0, 0.6, 0.86); c.close_path(); p.ks(0.18)


@glyph("family")
def _(c, p):
    circ(c, -0.4, -0.5, 0.26); p.fs(0.2)
    c.new_sub_path(); c.move_to(-0.92, 0.7); c.curve_to(-0.92, -0.1, 0.12, -0.1, 0.12, 0.7); c.close_path(); p.fs(0.14)
    circ(c, 0.48, -0.06, 0.2); p.ks(0.25)
    c.new_sub_path(); c.move_to(0.08, 0.86); c.curve_to(0.08, 0.26, 0.88, 0.26, 0.88, 0.86); c.close_path(); p.ks(0.2)


@glyph("code")
def _(c, p):
    ln(c, (-0.44, -0.55), (-0.9, 0), (-0.44, 0.55)); p.s(1.1)
    ln(c, (0.44, -0.55), (0.9, 0), (0.44, 0.55)); p.s(1.1)
    ln(c, (0.16, -0.72), (-0.16, 0.72)); p.s()


@glyph("chip")
def _(c, p):
    rrect(c, -0.56, -0.56, 1.12, 1.12, 0.14); p.fs(0.12)
    rrect(c, -0.26, -0.26, 0.52, 0.52, 0.06); p.fs(0.35, 0.7)
    for t in (-0.3, 0, 0.3):
        ln(c, (t, -0.56), (t, -0.86)); p.s(0.7); ln(c, (t, 0.56), (t, 0.86)); p.s(0.7)
        ln(c, (-0.56, t), (-0.86, t)); p.s(0.7); ln(c, (0.56, t), (0.86, t)); p.s(0.7)


@glyph("magnet")
def _(c, p):
    c.new_sub_path(); c.move_to(-0.72, -0.82); c.line_to(-0.72, 0.05); c.arc_negative(0, 0.05, 0.72, PI, 0)
    c.line_to(0.72, -0.82); c.line_to(0.3, -0.82); c.line_to(0.3, 0.05); c.arc(0, 0.05, 0.3, 0, PI)
    c.line_to(-0.3, -0.82); c.close_path(); p.fs(0.15)
    ln(c, (-0.72, -0.5), (-0.3, -0.5)); p.s(); ln(c, (0.3, -0.5), (0.72, -0.5)); p.s()


@glyph("videocam")
def _(c, p):
    rrect(c, -0.94, -0.52, 1.2, 1.04, 0.2); p.fs(0.15)
    poly(c, [(0.34, -0.14), (0.92, -0.5), (0.92, 0.5), (0.34, 0.14)]); p.fs(0.25)


@glyph("hanger")
def _(c, p):
    c.new_sub_path(); c.arc(0, -0.62, 0.18, PI, 2.5 * PI)
    c.line_to(0, -0.3); c.line_to(0.92, 0.5); c.line_to(-0.92, 0.5); c.line_to(0, -0.3); p.s()


@glyph("sliders")
def _(c, p):
    for x, y in ((-0.55, 0.3), (0, -0.35), (0.55, 0.15)):
        ln(c, (x, -0.86), (x, 0.86)); p.s(0.8)
        circ(c, x, y, 0.17); p.ks(0.4)


@glyph("equalizer")
def _(c, p):
    hs = [(0.5, 0.2), (0.9, -0.3), (0.65, 0.1), (0.85, -0.45), (0.45, 0.25)]
    for i, (h, k) in enumerate(hs):
        x = -0.72 + i * 0.36
        ln(c, (x, -h), (x, h)); p.s(0.75)
        rrect(c, x - 0.12, k - 0.07, 0.24, 0.14, 0.05); p.ks(0.5, 0.8)


@glyph("rocket")
def _(c, p):
    c.new_sub_path(); c.move_to(0, -0.95); c.curve_to(0.46, -0.6, 0.42, 0.1, 0.28, 0.45); c.line_to(-0.28, 0.45)
    c.curve_to(-0.42, 0.1, -0.46, -0.6, 0, -0.95); c.close_path(); p.fs(0.15)
    circ(c, 0, -0.3, 0.15); p.s(0.8)
    poly(c, [(-0.3, 0.05), (-0.62, 0.52), (-0.26, 0.42)]); p.fs(0.3, 0.8)
    poly(c, [(0.3, 0.05), (0.62, 0.52), (0.26, 0.42)]); p.fs(0.3, 0.8)
    c.move_to(-0.14, 0.56); c.curve_to(-0.1, 0.75, 0.0, 0.85, 0, 0.95); c.curve_to(0.0, 0.85, 0.1, 0.75, 0.14, 0.56); p.s(0.8)


@glyph("crosshair")
def _(c, p):
    circ(c, 0, 0, 0.64); p.fs(0.08); circ(c, 0, 0, 0.08); p.solid()
    for a in range(4):
        t = a * PI / 2
        ln(c, (0.4 * math.cos(t), 0.4 * math.sin(t)), (0.94 * math.cos(t), 0.94 * math.sin(t))); p.s(0.9)


@glyph("lotus")
def _(c, p):
    for deg, s in ((-64, 0.72), (64, 0.72), (-32, 0.88), (32, 0.88), (0, 1.0)):
        c.save(); c.translate(0, 0.55); c.rotate(math.radians(deg)); c.scale(s, s)
        c.new_sub_path(); c.move_to(0, 0); c.curve_to(0.32, -0.36, 0.22, -0.9, 0, -1.2)
        c.curve_to(-0.22, -0.9, -0.32, -0.36, 0, 0); c.close_path(); c.restore()
        p.ks(0.14, 0.85)
    arcp(c, 0, 0.3, 0.62, 0.15 * PI, 0.85 * PI); p.s(0.7)


@glyph("cone")
def _(c, p):
    def hw(y):
        return 0.12 + (y + 0.86) / 1.4 * 0.48
    poly(c, [(-0.12, -0.86), (0.12, -0.86), (0.6, 0.54), (-0.6, 0.54)]); p.fs(0.15)
    for y in (-0.3, 0.12):
        poly(c, [(-hw(y), y), (hw(y), y), (hw(y + 0.16), y + 0.16), (-hw(y + 0.16), y + 0.16)]); p.solid(0.85)
    rrect(c, -0.86, 0.54, 1.72, 0.32, 0.1); p.fs(0.25)


@glyph("watch")
def _(c, p):
    rrect(c, -0.32, -0.95, 0.64, 1.9, 0.12); p.fs(0.08, 0.8)
    rrect(c, -0.52, -0.56, 1.04, 1.12, 0.28); p.ks(0.15)
    ln(c, (0, -0.3), (0, 0), (0.22, 0.14)); p.s(0.8)
    ln(c, (0.52, -0.12), (0.64, -0.12)); p.s()


@glyph("lifebuoy")
def _(c, p):
    circ(c, 0, 0, 0.86); p.s(); circ(c, 0, 0, 0.42); p.s()
    for a in (PI / 4, 3 * PI / 4, 5 * PI / 4, 7 * PI / 4):
        c.new_sub_path(); c.arc(0, 0, 0.86, a - 0.22, a + 0.22); c.arc_negative(0, 0, 0.42, a + 0.42, a - 0.42); c.close_path()
        p.f(0.45); c.new_path()


@glyph("flask")
def _(c, p):
    poly(c, [(-0.2, -0.86), (0.2, -0.86), (0.2, -0.3), (0.66, 0.64), (0.55, 0.86), (-0.55, 0.86), (-0.66, 0.64), (-0.2, -0.3)])
    p.fs(0.1)
    poly(c, [(-0.44, 0.2), (0.44, 0.2), (0.66, 0.64), (0.55, 0.86), (-0.55, 0.86), (-0.66, 0.64)]); p.f(0.3); c.new_path()
    circ(c, -0.12, 0.5, 0.08); p.solid(); circ(c, 0.18, 0.36, 0.06); p.solid(0.8)
    ln(c, (-0.3, -0.86), (0.3, -0.86)); p.s()


@glyph("puzzle")
def _(c, p):
    c.new_sub_path(); c.move_to(-0.6, -0.48); c.line_to(-0.18, -0.48); c.arc(0.0, -0.48, 0.18, PI, 2 * PI)
    c.line_to(0.6, -0.48); c.line_to(0.6, -0.02); c.arc(0.6, 0.16, 0.18, -PI / 2, PI / 2)
    c.line_to(0.6, 0.72); c.line_to(-0.6, 0.72); c.close_path(); p.fs(0.18)


@glyph("tiles")
def _(c, p):
    for k, (x, y) in enumerate(((-0.9, -0.62), (0.05, -0.62), (-0.9, 0.08), (0.05, 0.08))):
        rrect(c, x, y, 0.85, 0.54, 0.27); p.fs(0.35 if k in (0, 3) else 0.08, 0.8)
        circ(c, x + (0.58 if k in (0, 3) else 0.27), y + 0.27, 0.12); p.solid(0.9 if k in (0, 3) else 0.5)


@glyph("split")
def _(c, p):
    rrect(c, -0.9, -0.84, 1.8, 0.72, 0.14); p.fs(0.2)
    rrect(c, -0.9, 0.12, 1.8, 0.72, 0.14); p.fs(0.1)
    ln(c, (-0.22, 0), (0.22, 0)); p.s(1.3)


@glyph("database")
def _(c, p):
    ell(c, 0, -0.6, 0.72, 0.22); p.fs(0.22)
    ln(c, (-0.72, -0.6), (-0.72, 0.6)); p.s(); ln(c, (0.72, -0.6), (0.72, 0.6)); p.s()
    for y in (0.0, 0.6):
        ell(c, 0, y, 0.72, 0.22, 0, 0, PI); p.s()


@glyph("devices")
def _(c, p):
    rrect(c, -0.96, -0.66, 1.3, 0.96, 0.08); p.fs(0.1)
    ln(c, (-1.04, 0.46), (0.44, 0.46)); p.s()
    rrect(c, 0.32, -0.34, 0.58, 1.06, 0.1); p.ks(0.2)


@glyph("phone")
def _(c, p):
    rrect(c, -0.46, -0.9, 0.92, 1.8, 0.18); p.fs(0.1)
    ln(c, (-0.12, -0.72), (0.12, -0.72)); p.s(0.7); circ(c, 0, 0.68, 0.07); p.solid()


@glyph("window")
def _(c, p):
    rrect(c, -0.9, -0.76, 1.8, 1.52, 0.14); p.fs(0.08)
    ln(c, (-0.9, -0.38), (0.9, -0.38)); p.s(0.8)
    for i in range(3):
        circ(c, -0.68 + i * 0.18, -0.57, 0.055); p.solid()
    rrect(c, -0.66, -0.16, 0.6, 0.7, 0.06); p.fs(0.3, 0.6)
    for y in (-0.1, 0.15, 0.4):
        ln(c, (0.08, y), (0.66, y)); p.s(0.55)


@glyph("toolbox")
def _(c, p):
    rrect(c, -0.9, -0.3, 1.8, 1.12, 0.12); p.fs(0.12)
    ln(c, (-0.3, -0.3), (-0.3, -0.6), (0.3, -0.6), (0.3, -0.3)); p.s()
    ln(c, (-0.9, 0.12), (0.9, 0.12)); p.s(0.7)
    rrect(c, -0.15, 0.0, 0.3, 0.24, 0.05); p.ks(0.4, 0.8)


@glyph("sticker")
def _(c, p):
    poly(c, [(-0.8, -0.8), (0.8, -0.8), (0.8, 0.24), (0.24, 0.8), (-0.8, 0.8)]); p.fs(0.12)
    poly(c, [(0.8, 0.24), (0.3, 0.3), (0.24, 0.8)]); p.fs(0.35, 0.8)
    arcp(c, -0.12, -0.12, 0.36, 0.15 * PI, 0.85 * PI); p.s(0.8)
    circ(c, -0.3, -0.3, 0.07); p.solid(); circ(c, 0.06, -0.3, 0.07); p.solid()


@glyph("book")
def _(c, p):
    for sgn in (-1, 1):
        c.new_sub_path(); c.move_to(0, -0.55); c.curve_to(sgn * 0.35, -0.78, sgn * 0.7, -0.72, sgn * 0.92, -0.6)
        c.line_to(sgn * 0.92, 0.7); c.curve_to(sgn * 0.7, 0.6, sgn * 0.35, 0.62, 0, 0.82); c.close_path(); p.fs(0.12 if sgn < 0 else 0.2)
    sparkle(c, 0.46, -0.08, 0.24); p.solid(0.9)


@glyph("bulb")
def _(c, p):
    c.new_sub_path(); c.arc(0, -0.26, 0.58, 0.8 * PI, 2.2 * PI); c.line_to(0.24, 0.55); c.line_to(-0.24, 0.55); c.close_path()
    p.fs(0.15)
    ln(c, (-0.24, 0.7), (0.24, 0.7)); p.s(0.8); ln(c, (-0.14, 0.86), (0.14, 0.86)); p.s(0.8)
    ln(c, (-0.14, 0.3), (-0.1, -0.2), (0.1, -0.2), (0.14, 0.3)); p.s(0.6)


@glyph("leaf")
def _(c, p):
    c.new_sub_path(); c.move_to(-0.72, 0.72); c.curve_to(-0.82, -0.2, -0.1, -0.86, 0.82, -0.82)
    c.curve_to(0.78, 0.1, 0.1, 0.82, -0.72, 0.72); c.close_path(); p.fs(0.18)
    ln(c, (-0.72, 0.72), (0.46, -0.46)); p.s(0.7)
    for t in (0.35, 0.6):
        x, y = -0.72 + 1.18 * t, 0.72 - 1.18 * t
        ln(c, (x, y), (x - 0.02, y - 0.3)); p.s(0.5); ln(c, (x, y), (x + 0.3, y + 0.02)); p.s(0.5)


@glyph("gitgraph")
def _(c, p):
    ln(c, (-0.42, -0.5), (-0.42, 0.5)); p.s()
    c.move_to(0.42, -0.1); c.curve_to(0.42, 0.26, -0.42, 0.1, -0.42, 0.36); p.s()
    circ(c, -0.42, -0.66, 0.17); p.ks(0.4); circ(c, -0.42, 0.66, 0.17); p.ks(0.4); circ(c, 0.42, -0.27, 0.17); p.ks(0.4)


@glyph("whale")
def _(c, p):
    c.new_sub_path(); c.move_to(-0.9, 0.05)
    c.curve_to(-0.9, 0.62, 0.2, 0.7, 0.55, 0.22); c.curve_to(0.62, 0.1, 0.66, -0.05, 0.64, -0.22)
    c.line_to(0.92, -0.5); c.line_to(0.56, -0.44); c.line_to(0.4, -0.72); c.line_to(0.4, -0.3)
    c.curve_to(0.3, -0.1, 0.0, -0.25, -0.35, -0.25); c.curve_to(-0.65, -0.25, -0.9, -0.15, -0.9, 0.05); c.close_path()
    p.fs(0.18)
    circ(c, -0.52, -0.02, 0.06); p.solid()
    ln(c, (-0.3, -0.42), (-0.3, -0.7)); p.s(0.7)
    arcp(c, -0.45, -0.7, 0.15, 1.2 * PI, 2.0 * PI); p.s(0.6); arcp(c, -0.15, -0.7, 0.15, 1.0 * PI, 1.8 * PI); p.s(0.6)


@glyph("opera")
def _(c, p):
    arcp(c, 0, 0, 0.78, -0.75 * PI, 0.55 * PI); p.s(1.35)
    arcp(c, 0, 0, 0.78, 0.75 * PI, 1.08 * PI); p.s(1.35)
    ell(c, 0, 0, 0.88, 0.34, -0.42); p.s(0.85)
    circ(c, 0, 0, 0.22); p.fs(0.3, 0.8)
    ln(c, (-0.9, -0.52), (-0.76, -0.84), (-0.44, -0.84)); p.s(0.75)
    ln(c, (0.9, 0.52), (0.76, 0.84), (0.44, 0.84)); p.s(0.75)


@glyph("swirl")
def _(c, p):
    arcp(c, 0, 0, 0.84, 0.1 * PI, 1.75 * PI); p.s(1.2)
    c.move_to(-0.7, 0.1); c.curve_to(-0.6, -0.5, 0.45, -0.55, 0.62, 0.05)
    c.curve_to(0.66, 0.25, 0.3, 0.28, -0.1, 0.2); c.curve_to(-0.1, 0.5, 0.3, 0.7, 0.75, 0.42); p.s()


@glyph("crane")
def _(c, p):
    poly(c, [(-0.86, 0.12), (-0.08, -0.1), (0.62, 0.12), (-0.05, 0.56)]); p.fs(0.15)
    poly(c, [(-0.08, -0.1), (-0.5, -0.86), (0.22, 0.02)]); p.fs(0.3, 0.85)
    poly(c, [(0.04, -0.04), (0.42, -0.72), (0.4, 0.06)]); p.fs(0.2, 0.85)
    ln(c, (0.5, 0.06), (0.86, -0.5), (0.98, -0.42)); p.s(0.85)
    ln(c, (-0.62, 0.06), (-0.92, -0.4)); p.s(0.85)


@glyph("flower")
def _(c, p):
    for k in range(4):
        a = k * PI / 2 + PI / 4
        ell(c, 0.42 * math.cos(a), 0.42 * math.sin(a), 0.44, 0.26, a); p.ks(0.15, 0.85)
    circ(c, 0, 0, 0.16); p.solid()


@glyph("dots9")
def _(c, p):
    lit = {(0, 1), (1, 1), (2, 1), (1, 2)}
    for i in range(3):
        for j in range(3):
            circ(c, -0.6 + 0.6 * i, -0.6 + 0.6 * j, 0.19); p.solid(1.0 if (i, j) in lit else 0.3)


@glyph("dotdiamond")
def _(c, p):
    for col, n in enumerate((1, 2, 3, 2, 1)):
        x = -0.72 + col * 0.36
        for k in range(n):
            y = (k - (n - 1) / 2) * 0.36
            circ(c, x, y, 0.14 - 0.02 * abs(col - 2)); p.solid(1.0 - 0.15 * abs(col - 2))


@glyph("dots4")
def _(c, p):
    for (x, y, r) in ((-0.42, -0.1, 0.24), (0.06, -0.1, 0.2), (0.46, -0.1, 0.16), (0.1, 0.44, 0.3)):
        circ(c, x, y, r); p.solid(0.95)


@glyph("prism")
def _(c, p):
    ngon(c, 0, 0.12, 0.92, 3); p.fs(0.12)
    ngon(c, 0, 0.12, 0.46, 3, PI / 2); p.fs(0.3, 0.8)


@glyph("calc")
def _(c, p):
    rrect(c, -0.76, -0.86, 1.52, 1.72, 0.2); p.fs(0.08)
    s = 0.17
    ln(c, (-0.36 - s, -0.36), (-0.36 + s, -0.36)); p.s(); ln(c, (-0.36, -0.36 - s), (-0.36, -0.36 + s)); p.s()
    ln(c, (0.36 - s, -0.36), (0.36 + s, -0.36)); p.s()
    ln(c, (-0.36 - s * 0.7, 0.36 - s * 0.7), (-0.36 + s * 0.7, 0.36 + s * 0.7)); p.s()
    ln(c, (-0.36 + s * 0.7, 0.36 - s * 0.7), (-0.36 - s * 0.7, 0.36 + s * 0.7)); p.s()
    ln(c, (0.36 - s, 0.28), (0.36 + s, 0.28)); p.s(); ln(c, (0.36 - s, 0.44), (0.36 + s, 0.44)); p.s()


@glyph("block")
def _(c, p):
    circ(c, 0, 0, 0.8); p.fs(0.1); ln(c, (-0.56, 0.56), (0.56, -0.56)); p.s()


@glyph("sigil")
def _(c, p, h=0):
    n = 3 + h % 5
    ngon(c, 0, 0, 0.9, n, -PI / 2 + (h >> 3) % 7 * 0.1); p.fs(0.1)
    star(c, 0, 0, 0.6, n, 0.5, PI / 2); p.fs(0.25, 0.85)
    circ(c, 0, 0, 0.12); p.solid()
    for i in range(n):
        a = -PI / 2 + 2 * PI * i / n
        circ(c, 0.9 * math.cos(a), 0.9 * math.sin(a), 0.07); p.solid()


# badges (drawn small at the lower-right, unit space)
@glyph("plus")
def _(c, p):
    ln(c, (0, -0.62), (0, 0.62)); p.s(); ln(c, (-0.62, 0), (0.62, 0)); p.s()


@glyph("check")
def _(c, p):
    ln(c, (-0.55, 0.0), (-0.15, 0.42), (0.6, -0.42)); p.s()


@glyph("recdot")
def _(c, p):
    circ(c, 0, 0, 0.42); p.solid(); circ(c, 0, 0, 0.75); p.s()


@glyph("star")
def _(c, p):
    star(c, 0, 0.06, 0.88); p.fs(0.35)


# ===================================================================== identity -> glyph
GLYPH_MAP = {
    "com.soul_99.suIMD": "sliders+gear",
    "com.google.android.apps.chromecast.app": "house+wifi",
    "com.google.chromeremotedesktop": "monitor+cursor",
    "com.anthropic.claude": "starburst",
    "com.google.android.apps.cloudconsole": "cloud+terminal",
    "com.google.android.apps.credentialmanager": "key+shield",
    "com.creditkarma.mobile.international": "gauge",
    "com.csdroid.pkg": "box+magnifier",
    "ru.ryyxuaxi.bahqmbnyw": "sigil",
    "eu.darken.sdmse": "broom",
    "com.dd.doordash": "bag+pin",
    "com.deepseek.chat": "whale",
    "demigos.com.mobilism": "download+people",
    "de.dertyp7214.mixplorerthemecreator": "folder2+brush",
    "com.docusign.ink": "pen",
    "com.drdisagree.colorblendr": "venn",
    "com.google.android.apps.docs.editors.docs": "document",
    "com.einnovation.temu": "bag+star",
    "com.enflick.android.TextNow": "chat+handset",
    "com.sameerasw.essentials": "toolbox",
    "com.estrongs.android.pop": "folder+cloud",
    "com.facebook.katana": "people",
    "com.facebook.orca": "chat+bolt",
    "vn.fighttech.touchlock": "tap+lock",
    "com.fitbit.FitbitMobile": "dotdiamond",
    "com.google.android.apps.fitness": "heartpulse",
    "ginlemon.flower.slcompanionapp": "flower+link",
    "com.delphicoder.flud.paid": "magnet+download",
    "com.epicgames.fortnite": "storm",
    "com.freeit.java": "cup+code",
    "com.google.android.apps.bard": "sparkle4",
    "com.github.android": "gitgraph",
    "io.github.androidmonitor": "chip+heartpulse",
    "com.appslab.glass.widgets": "widgets",
    "gmikhail.colorpicker": "eyedropper",
    "com.google.android.keep": "bulb",
    "com.google.android.apps.walletnfcrel": "cards",
    "com.google.android.apps.googleassistant": "dots4",
    "com.google.android.apps.googlevoice": "mic+chat",
    "com.salat.gsplit": "split",
    "eu.sisik.hackendebug.full": "bug+terminal",
    "com.hamondev.shevery": "window+bolt",
    "helium314.keyboard": "keyboard",
    "com.hicloud.android.clone": "transfer",
    "com.ceyhan.sets": "gear+eye",
    "in.hridayan.ashell": "terminal+bolt",
    "com.hscnapps.ruler": "ruler",
    "io.github.vvb2060.ims.mod": "tower+gear",
    "app.mystery0.ims.tensor": "tower+chip",
    "dev.jason.com.google.android.inputmethod.latin": "keyboard+gear",
    "com.instagram.android": "instacam",
    "com.rosan.installer.x.revived": "box+download",
    "com.jahertor.edgecalendar": "calendar+split",
    "com.jahertor.weather": "suncloud",
    "com.joaomgcd.autocontacts": "person+bolt",
    "com.joaomgcd.autotools": "wrench+bolt",
    "kerneldroid.nightzuku": "crane+moon",
    "com.google.android.apps.kids.familylink": "family",
    "com.kieronquinn.app.ambientmusicmod": "note+waveform",
    "com.ebay.kijiji.ca": "bag+magnifier",
    "com.kitsumed.shizucallrecorder": "handset+recdot",
    "air.kukulive.mailnow": "envelope+bolt",
    "org.kustom.widget": "widgets+brush",
    "com.google.android.apps.labs.language.tailwind": "book",
    "com.jndapp.line.x.iconpack": "grid",
    "com.liuzh.deviceinfo": "chip",
    "com.loblaw.shoppersdrugmart": "pill",
    "com.machiav3lli.fdroid": "box+star",
    "com.google.android.apps.magazines": "newspaper",
    "com.marsvard.stickermakerforwhatsapp": "sticker",
    "ru.maximoff.apktool": "box+wrench",
    "com.microsoft.appmanager": "devices",
    "com.microsoft.bing": "magnifier+sparkle4",
    "com.microsoft.copilot": "knot",
    "com.microsoft.emmx": "swirl",
    "com.minma.icon.free": "iconpack",
    "com.mirfatif.permissionmanagerx.pro": "shieldcheck+star",
    "com.mirfatif.permissionmanagerx": "shield+eye",
    "com.mixplorer.silver": "folder2",
    "cum.ahfu.mobi.material": "prism+brush",
    "app.morphe.manager": "play+gear",
    "com.mouseops.app": "cursor+gear",
    "in.ms.packagenamesscripter": "box+code",
    "com.amazon.mShop.android.shopping": "cart",
    "com.mobile_infographics_tools.mydrive_ext": "piechart",
    "com.myklarnamobile": "card+timer",
    "com.nannidan.aiarticons": "iconpack+sparkle4",
    "com.nannidan.cursorops": "cursor+bolt",
    "com.nannidan.pixelblendr": "pixelgrid",
    "com.neofinancial.neo": "card+star",
    "com.niksoftware.snapseed": "leaf",
    "top.nkbe.npatch": "puzzle+wrench",
    "com.teslacoilsw.launcher": "house+star",
    "com.microsoft.office.outlook": "envelope+calendar",
    "com.opera.gx": "opera",
    "com.pixel8pro.rromanager": "grid+gear",
    "com.google.android.apps.paidtasks": "coins+check",
    "ch.patrickfrei.phonetinfo.free": "phone+magnifier",
    "com.payne.okux": "remote+bolt",
    "com.paypal.android.p2pmobile": "wallet+sync",
    "com.payton.touchblocker": "tap+block",
    "com.smartswitch.phonecloneapp.phonetransfer": "transfer+sync",
    "com.iudesk.android.photo.editor": "image+sliders",
    "com.picsart.studio": "image+brush",
    "com.pixeltimer.app": "timer",
    "com.google.android.play.games": "controller",
    "com.popovicialinc.gama": "chip+bolt",
    "app.morphe.pot.helper": "flask",
    "com.pzolee.wifiinfoPro": "wifi+magnifier",
    "com.quickcursor": "cursor",
    "zed.rainxch.githubstore": "bag+gitgraph",
    "com.rbc.mobile.android": "bank",
    "digital.rbi.timhortons": "cup",
    "com.microsoft.rdc.androidx": "monitor+link",
    "com.realsignal.wifiscanner": "radar",
    "com.usertesting.recorder.krsna": "monitor+recdot",
    "com.androguide.recovery.emulator": "lifebuoy",
    "com.Relmtech.RemotePaid": "remote+wifi",
    "com.wantrobapps.virtualpet": "egg",
    "com.boedec.hoel.speaker.cleaner.remove.water": "droplet+speaker",
    "com.alyn.resamp": "controller+people",
    "com.resukisu.resukisu": "shield+chip",
    "app.revanced.android.gms": "puzzle",
    "me.rhunk.snapenhance": "ghost+sparkle4",
    "com.rk.taskmanager": "barchart",
    "com.roblox.client": "tiltcube",
    "com.roku.remote": "remote",
    "me.timschneeberger.rootlessjamesdsp": "equalizer",
    "dev.rushi.apkdownloadhelper": "download+box",
    "app.rvx.android.youtube": "play",
    "com.sameerasw.overcast": "cloud",
    "com.sammy.sbatterytweaks": "battery+gear",
    "io.github.samolego.canta": "trash",
    "org.client.scrcpy": "cast",
    "eu.thedarken.sdm.unlocker": "broom+key",
    "com.sec.android.easyMover": "transfer+check",
    "com.sec.android.app.sbrowser": "planet",
    "ninja.sesame.app.edge": "magnifier+bolt",
    "by4a.setedit22": "database",
    "com.sezzle.sezzlemobile": "coins+timer",
    "af.shizuku.plus.api": "crane+plus",
    "com.shopify.arrive": "box+pin",
    "show.taps": "tap",
    "it.simonesestito.ntiles": "tiles",
    "ginlemon.flowerfree": "flower",
    "net.smartlauncher.beakynews": "newspaper+flower",
    "net.smartlauncher.wallpaperstudio": "image+flower",
    "com.smartpack.packagemanager": "box+gear",
    "com.smartwidgetlabs.nfctools": "nfc",
    "com.smoothie.wirelessDebuggingSwitch": "wifi+bug",
    "com.snapchat.android": "ghost",
    "com.spaceman.verticonspro": "iconpack+star",
    "ioapp.speaker.dustwater": "speaker+droplet",
    "com.ss.edgegestures": "gesture",
    "com.stickmouse.cursor": "cursor+star",
    "com.stremio.one": "playhex",
    "com.google.android.apps.subscriptions.red": "cloud+star",
    "org.swiftapps.swiftbackup": "cloud+sync",
    "de.szalkowski.activitylauncher": "rocket",
    "com.tailscale.ipn": "dots9",
    "net.dinglisch.android.taskerm": "gear+bolt",
    "com.google.android.apps.tasks": "checklist",
    "com.ghisler.tcplugins.FTP": "folder+sync",
    "org.telegram.messenger": "plane",
    "org.telegram.plus": "plane+plus",
    "es.rafalense.telegram.themes": "plane+brush",
    "com.termux.api": "terminal+plug",
    "com.termux.gui": "window+terminal",
    "com.maazm7d.termuxhub": "terminal+grid",
    "com.termux.widget": "terminal+widgets",
    "com.termux.x11": "terminal+monitor",
    "eu.thedarken.sdm": "broom+star",
    "com.thetransitapp.droid": "bus",
    "me.timschneeberger.shizustore": "crane+bag",
    "com.tom.rv2ide": "code+gear",
    "com.agy.tracingbabbaloo": "pen+star",
    "com.google.android.apps.translate": "translate",
    "com.tribalfs.pixels": "pixelgrid+gear",
    "com.agy.triggeralign": "crosshair+timer",
    "com.agy.triggeralign.v2": "crosshair+plus",
    "com.umutk.blackout": "moon",
    "com.urbandroid.mind": "lotus",
    "org.videolan.vlc": "cone",
    "com.vistechprojects.millimeter": "ruler+phone",
    "io.github.vvb2060.ims": "tower",
    "io.wallpaperengine.weclient": "image+gear",
    "com.wealthsimple.trade": "chartup",
    "dev.dect.wear.complicationbox": "watch",
    "com.google.android.webview.dev": "globe+code",
    "com.whatsapp": "chat+handset",
    "com.wissamhbooss.colorpicker": "eyedropper+palette",
    "com.x8bit.bitwarden": "shield+lock",
    "com.deniscerri.ytdl": "download+play",
    "ru.zdevs.zarchiver": "archive",
    "us.zoom.videomeetings": "videocam",
    "dev.zwander.cellreader": "signal+magnifier",
    "dev.zwander.installwithoptions": "box+sliders",
    "com.zzkko": "hanger",
    "com.google.android.aicore": "chip+sparkle4",
    "com.google.android.apps.betterbug": "bug",
    "com.google.android.calculator": "calc",
    "com.google.android.calendar": "calendar",
    "com.google.android.deskclock": "clock",
    "com.google.android.contacts": "person",
    "com.google.android.apps.nbu.files": "folder",
    "com.google.android.apps.docs": "prism",
    "com.google.android.googlequicksearchbox": "magnifier",
    "com.google.android.apps.pixel.health": "heart",
    "com.google.android.apps.pixel.nowplaying": "note",
    "com.google.android.apps.recorder": "waveform",
    "com.google.android.apps.weather": "suncloud",
    "com.google.android.apps.safetyhub": "shieldcheck",
    "com.google.android.apps.tachyon": "videocam+plus",
    "com.google.android.apps.tycho": "signal",
    "com.google.android.videos": "tv",
    "com.android.virtualization.terminal": "terminal+box",
}

KEYWORD_GLYPHS = [
    (r"terminal|shell|termux|adb", "terminal"), (r"youtube|video|tube|player|stream", "play"),
    (r"camera|lens|scan", "camera"), (r"photo|gallery|wallpaper|image", "image"), (r"music|audio|sound", "note"),
    (r"mail|gmail", "envelope"), (r"chat|messag|sms", "chat"), (r"phone|dialer|call", "handset"),
    (r"browser|web|chrome|firefox", "globe"), (r"map|nav|location", "pin"), (r"setting|config|tweak", "gear"),
    (r"file|explorer|folder", "folder"), (r"bank|finance|money|pay", "card"), (r"shop|store|market", "bag"),
    (r"game|play", "controller"), (r"weather", "suncloud"), (r"clock|alarm", "clock"), (r"calendar", "calendar"),
    (r"note|keep|doc", "document"), (r"vpn|secur|guard|auth|privacy|permission", "shield"),
    (r"wifi|network|net", "wifi"), (r"keyboard|input", "keyboard"), (r"backup|cloud|sync", "cloud"),
    (r"cursor|mouse", "cursor"), (r"launcher|home", "house"), (r"icon|theme", "iconpack"),
    (r"ai|assistant|gpt|bot", "sparkle4"), (r"install|apk|package", "box"), (r"color|paint", "palette"),
    (r"timer|task", "checklist"), (r"health|fit", "heartpulse"), (r"translate|language", "translate"),
]


def glyph_spec(app):
    spec = GLYPH_MAP.get(app["package"])
    if spec:
        return spec, "curated"
    hay = f"{app['package']} {app['app_name']} {app['prompt_subject']}".lower()
    for rx, g in KEYWORD_GLYPHS:
        if re.search(rx, hay):
            return g, "keyword"
    return "sigil", "sigil"


# ===================================================================== renderer
def load_style():
    style = os.environ.get("ICON_STYLE", "")
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            if line.strip().startswith("ICON_STYLE="):
                style = line.split("=", 1)[1].strip().strip('"').strip("'")
    return style if style in STYLES else "cyberpunk"


def tile_path(c, shape, t):
    if shape == "rsq":
        rrect(c, -t, -t, 2 * t, 2 * t, t * 0.42)
    elif shape == "circle":
        circ(c, 0, 0, t)
    elif shape == "hex":
        ngon(c, 0, 0, t * 1.05, 6, 0)


def surface_to_rgb(surf):
    surf.flush()
    return Image.frombuffer("RGBA", (surf.get_width(), surf.get_height()), bytes(surf.get_data()),
                            "raw", "BGRA", surf.get_stride(), 1).convert("RGB")


def scale_img(img, k):
    return img.point(lambda v: min(255, int(v * k)))


def render_icon(app, style_name):
    st = STYLES[style_name]
    h = int(hashlib.sha1(app["package"].encode()).hexdigest(), 16)
    cols = st["pairs"][h % len(st["pairs"])]
    shape = ("rsq", "rsq", "circle", "rsq", "free", "circle", "rsq", "hex")[(h >> 8) % 8]
    angle = math.radians((h >> 16) % 360)
    spec, source = glyph_spec(app)
    parts = spec.split("+")
    main, badge = parts[0], (parts[1] if len(parts) > 1 else None)

    R = 215.0 if shape == "free" else 175.0         # px per glyph unit
    lw = 13.0 / R
    t_unit = TILE_HALF / R

    # --- opaque base: #050508 + obsidian badge body
    base = cairo.ImageSurface(cairo.FORMAT_RGB24, SS, SS)
    c = cairo.Context(base)
    c.set_source_rgb(*(v / 255 for v in BG)); c.paint()
    c.translate(SS / 2, SS / 2); c.scale(R, R)
    if shape != "free":
        top, bot = st["body"]
        tile_path(c, shape, t_unit)
        g = cairo.LinearGradient(0, -t_unit, 0, t_unit)
        g.add_color_stop_rgb(0, *top); g.add_color_stop_rgb(1, *bot)
        c.set_source(g); c.fill_preserve()
        rg = cairo.RadialGradient(-0.45 * t_unit, -0.6 * t_unit, 0, -0.45 * t_unit, -0.6 * t_unit, 1.4 * t_unit)
        rg.add_color_stop_rgba(0, *cols[0], 0.09); rg.add_color_stop_rgba(1, *cols[1], 0.0)
        c.set_source(rg); c.fill()
        tile_path(c, shape, t_unit * 0.94)
        c.set_source_rgba(1, 1, 1, 0.06); c.set_line_width(2.0 / R); c.stroke()
    else:
        rg = cairo.RadialGradient(0, 0, 0, 0, 0, 1.35)
        rg.add_color_stop_rgba(0, *cols[0], 0.07); rg.add_color_stop_rgba(1, 0, 0, 0, 0)
        c.set_source(rg); circ(c, 0, 0, 1.35); c.fill()

    # --- neon layers (glow + white-hot core)
    layers = []
    for core in (False, True):
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, SS, SS)
        c = cairo.Context(surf)
        c.set_line_cap(cairo.LINE_CAP_ROUND); c.set_line_join(cairo.LINE_JOIN_ROUND)
        c.translate(SS / 2, SS / 2); c.scale(R, R)
        pen = Pen(c, cols, angle, lw, core, st["core"])
        if shape != "free":
            tile_path(c, shape, t_unit); pen.s(0.62)
        else:
            arcp(c, 0, 0, 1.28, angle, angle + 1.2 * PI); pen.s(0.4)
        fn = G[main]
        fn(c, pen, h) if main == "sigil" else fn(c, pen)
        c.new_path()
        if badge:
            bx = by = 0.66
            circ(c, bx, by, 0.52); pen.knock(); c.new_path()
            circ(c, bx, by, 0.48); pen.f(0.12); pen.s(0.55); c.new_path()
            c.save(); c.translate(bx, by); c.scale(0.33, 0.33)
            bpen = Pen(c, (cols[1], cols[0]), angle + PI, lw / 0.33 * 0.75, core, st["core"])
            G[badge](c, bpen); c.new_path(); c.restore()
        layers.append(surface_to_rgb(surf))
    glow, core_img = layers

    k1, k2, k3 = st["bloom"]
    bloom = ImageChops.screen(scale_img(glow.filter(ImageFilter.GaussianBlur(9)), k1),
                              scale_img(glow.filter(ImageFilter.GaussianBlur(26)), k2))
    bloom = ImageChops.screen(bloom, scale_img(glow.filter(ImageFilter.GaussianBlur(58)), k3))
    out = ImageChops.screen(surface_to_rgb(base), bloom)
    out = ImageChops.add(out, glow)
    out = ImageChops.add(out, scale_img(core_img, 0.85))
    if style_name == "painted":
        noise = Image.effect_noise((SS, SS), 22).convert("RGB")
        out = ImageChops.add(out, scale_img(ImageChops.multiply(noise, scale_img(glow, 1.0)), 0.25))
    if style_name == "retro":
        scan = Image.new("RGB", (SS, SS), (0, 0, 0)); d = ImageDraw.Draw(scan)
        for y in range(0, SS, 6):
            d.line([(0, y), (SS, y)], fill=(18, 18, 18))
        out = ImageChops.subtract(out, scan)
    out = out.resize((OUT, OUT), Image.Resampling.LANCZOS)
    # pin the corners/edge band to the exact pack background
    edge = Image.new("L", (OUT, OUT), 255); ImageDraw.Draw(edge).rectangle([24, 24, OUT - 25, OUT - 25], fill=0)
    out.paste(Image.new("RGB", (OUT, OUT), BG), (0, 0), edge.filter(ImageFilter.GaussianBlur(8)))
    return out, spec, source, shape


def qa(img):
    """Return list of rule violations for a rendered 512x512 icon."""
    errs = []
    if img.size != (OUT, OUT) or img.mode != "RGB":
        errs.append(f"size/mode {img.size} {img.mode}")
    for pt in ((0, 0), (OUT - 1, 0), (0, OUT - 1), (OUT - 1, OUT - 1), (OUT // 2, 2), (2, OUT // 2)):
        px = img.getpixel(pt)
        if max(abs(a - b) for a, b in zip(px, BG)) > 2:
            errs.append(f"background {pt}={px}")
    lo, hi = OUT * (1 - SAFE) / 2, OUT * (1 + SAFE) / 2
    bright = img.convert("L").point(lambda v: 255 if v > 110 else 0)
    bbox = bright.getbbox()
    if bbox and (bbox[0] < lo - 2 or bbox[1] < lo - 2 or bbox[2] > hi + 2 or bbox[3] > hi + 2):
        errs.append(f"emblem bbox {bbox} exceeds safe zone [{lo:.0f},{hi:.0f}]")
    if not bbox:
        errs.append("empty emblem")
    return errs


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--style", choices=list(STYLES), default=None, help="override ICON_STYLE from cf.env")
    ap.add_argument("--only", default="", help="substring filter on package")
    ap.add_argument("--force", action="store_true", help="re-render icons previously rendered locally")
    ap.add_argument("--sheet", default="", help="write a contact sheet PNG of rendered icons here")
    args = ap.parse_args()

    style = args.style or load_style()
    apps = sorted(json.loads(CATALOG_FILE.read_text())["apps"], key=lambda a: a.get("priority", 1e9))
    RAW_DIR.mkdir(exist_ok=True)
    manifest = json.loads(LOCAL_MANIFEST.read_text()) if LOCAL_MANIFEST.exists() else {}

    todo, seen = [], set()
    for a in apps:
        d = a["drawable"]
        if d in seen or (args.only and args.only.lower() not in a["package"].lower()):
            continue
        seen.add(d)
        exists = (RAW_DIR / f"{d}.png").exists()
        if not exists or (args.force and d in manifest):
            todo.append(a)
    print(f"Style: {style}. {len(todo)} icons to render locally (0 Neurons).")

    rendered, problems, sources = [], [], {}
    for i, a in enumerate(todo, 1):
        img, spec, source, shape = render_icon(a, style)
        errs = qa(img)
        out = RAW_DIR / f"{a['drawable']}.png"
        img.save(out, "PNG", optimize=True)
        manifest[a["drawable"]] = {"package": a["package"], "glyph": spec, "source": source, "shape": shape, "style": style}
        sources[source] = sources.get(source, 0) + 1
        rendered.append((a, img))
        flag = "  QA: " + "; ".join(errs) if errs else ""
        if errs:
            problems.append((a["drawable"], errs))
        print(f"[{i}/{len(todo)}] #{a['priority']} {a['app_name']} -> {a['drawable']} [{spec}/{shape}]{flag}")
    LOCAL_MANIFEST.write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n")

    if args.sheet and rendered:
        cols, cell = 12, 128
        rows = math.ceil(len(rendered) / cols)
        sheet = Image.new("RGB", (cols * cell, rows * cell), BG)
        for k, (_, img) in enumerate(rendered):
            sheet.paste(img.resize((cell, cell), Image.Resampling.LANCZOS), ((k % cols) * cell, (k // cols) * cell))
        sheet.save(args.sheet)
        print(f"Contact sheet: {args.sheet}")

    print(f"Rendered {len(rendered)} icons ({sources}). QA failures: {len(problems)}")
    for d, e in problems:
        print(f"  {d}: {e}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
