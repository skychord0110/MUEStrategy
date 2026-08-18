# -*- coding: utf-8 -*-
"""右肩上がりだけで組んだアイコン案100種。

これまでの案の多くはV字（下げてから上げる）を紋にしていた。あれは
「投げ売り反発」を表す図だったが、損益がしゃがむようにも読める。
このファイルの案は**単調に上がる形だけ**を使い、V字と落ち込みを一切出さない。

案は10群×10案。群ごとに上昇の見せ方を変え、群の中では作り方を1案ずつ変える。
    R1〜R10    線
    R11〜R20   棒
    R21〜R30   段
    R31〜R40   矢
    R41〜R50   面
    R51〜R60   光
    R61〜R70   生きもの
    R71〜R80   幾何
    R81〜R90   印刷
    R91〜R100  もの

描画の仕組みは icon_candidates.py のものを使う（あちらが本体、ここは案の定義）。

実行:
    python ui/icon_rising.py            一覧シートを書き出す
    python ui/icon_rising.py --ico R42  案を確定してアイコンにする
出力:
    ui/icon_preview/rising.png
"""
import math
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

import icon_candidates as ic  # noqa: E402

tile, radial, xform, qbez = ic.tile, ic.radial, ic.xform, ic.qbez
rr_inside, dist_to_polyline = ic.rr_inside, ic.dist_to_polyline

# ── 色 ──────────────────────────────────────────────────────────────
INK = (0x0C, 0x10, 0x1A)
NIGHT = (0x10, 0x1C, 0x3A)
DEEP = (0x14, 0x2B, 0x6E)
BLUE = (0x2A, 0x5C, 0xE0)
SKY = (0x5F, 0xA8, 0xF5)
CYAN = (0x1F, 0xD6, 0xE0)
TEAL = (0x0E, 0xA5, 0x9E)
GREEN = (0x2E, 0xC4, 0x6B)
LIME = (0xB8, 0xF0, 0x2A)
YELLOW = (0xFF, 0xD1, 0x1A)
AMBER = (0xFF, 0xA5, 0x14)
ORANGE = (0xFF, 0x6A, 0x14)
RUST = (0xB4, 0x45, 0x1E)
CORAL = (0xFF, 0x5A, 0x4E)
PINK = (0xFF, 0x3D, 0x8A)
MAGENTA = (0xD6, 0x2C, 0xC8)
VIOLET = (0x7C, 0x3A, 0xED)
INDIGO = (0x3B, 0x30, 0xC4)
PLUM = (0x3A, 0x14, 0x46)
CREAM = (0xFF, 0xF2, 0xE0)
BONE = (0xEC, 0xE6, 0xD8)
SAND = (0xE8, 0xC9, 0x8A)
WHITE = (0xFF, 0xFF, 0xFF)


# ── 上昇の骨格。すべて x が増えると y が減る（＝右上がり）────────────
def rise(n=5, x0=0.08, x1=0.94, y0=0.86, y1=0.13, bow=0.06):
    """右上がりの折れ線。bow で下に膨らませ、加速して見せる。"""
    out = []
    for i in range(n):
        t = i / (n - 1)
        x = x0 + (x1 - x0) * t
        y = y0 + (y1 - y0) * t + bow * math.sin(math.pi * t)
        out.append((x, y))
    return out


RISE = rise()
RISE_S = rise(n=4, bow=0.0)


def steps_poly(n=5, x0=0.07, x1=0.93, ybot=0.90, ytop=0.15):
    """階段。左下から右上へ、床まで閉じた多角形にして返す。"""
    dx, dy = (x1 - x0) / n, (ybot - ytop) / n
    pts = []
    for i in range(n):
        x, y = x0 + i * dx, ybot - i * dy
        pts += [(x, y), (x + dx, y), (x + dx, y - dy)]
    return [(x0, ybot)] + pts + [(x1, ybot)]


def bar_specs(n=5, x0=0.10, x1=0.90, ybot=0.87, ytop=0.15, gap=0.30):
    """右へ行くほど高くなる棒。(中心x, 上端, 幅) を返す。"""
    w = (x1 - x0) / n
    for i in range(n):
        yield (x0 + w * (i + 0.5),
               ybot - (ybot - ytop) * (i + 1) / n,
               w * (1 - gap))


def head(tip, deg, size, spread=0.40):
    """進行方向 deg（画面座標・上向きは負）に向いた三角の矢尻。"""
    a = math.radians(deg)
    return [tip] + [(tip[0] - size * math.cos(a + d),
                     tip[1] - size * math.sin(a + d)) for d in (spread, -spread)]


def shift(pts, dx, dy):
    return [(x + dx, y + dy) for x, y in pts]


def shadow(c, pts, w, col, reach=1.0, step=0.038):
    for i in range(int(reach / step), 0, -1):
        d = i * step
        c.stroke(shift(pts, d, d), w, (col, 1.0))


DESIGNS = []


def design(key, name):
    def deco(fn):
        DESIGNS.append((key, name, fn))
        return fn
    return deco


# ══ R1〜R10 線 ══════════════════════════════════════════════════════
@design("R1", "線＋長い影")
def r1(c):
    tile(c, ORANGE, AMBER, edge=None)
    shadow(c, RISE, 0.135, RUST)
    c.stroke(RISE, 0.135, (DEEP, 1.0))


@design("R2", "破線")
def r2(c):
    tile(c, DEEP, NIGHT, edge=None)
    p = rise(n=22)
    for i in range(0, 21, 2):
        c.stroke([p[i], p[i + 1]], 0.085, (LIME, 1.0))


@design("R3", "二重線")
def r3(c):
    tile(c, CREAM, BONE, edge=None)
    c.stroke(RISE, 0.180, (INDIGO, 1.0))
    c.stroke(RISE, 0.070, (CREAM, 1.0))


@design("R4", "先細り")
def r4(c):
    tile(c, PLUM, INK, edge=None)
    c.taper((0.06, 0.90), (0.45, 0.74), (0.95, 0.10), 0.20, 0.02, (PINK, 1.0))


@design("R5", "だんだん大きい点")
def r5(c):
    tile(c, NIGHT, INK, edge=None)
    p = rise(n=9)
    for i, (x, y) in enumerate(p):
        c.ring(x, y, 0.020 + 0.008 * i, 0.0, (CYAN, 1.0))


@design("R6", "ネオン管")
def r6(c):
    tile(c, INK, (0x18, 0x08, 0x2A), edge=None)
    for w, a in ((0.26, 0.10), (0.17, 0.16), (0.11, 0.26)):
        c.stroke(RISE, w, (MAGENTA, a))
    c.stroke(RISE, 0.055, (WHITE, 1.0))


@design("R7", "斜線で塗った帯")
def r7(c):
    tile(c, BONE, CREAM, edge=None)
    near = ic.near_polyline(RISE, 0.20)
    c.shape((0, 0, 1, 1),
            lambda x, y: near(x, y) and ((x * 2.4 + y * 2.4) % 0.075) < 0.036,
            (RUST, 1.0))
    c.stroke(RISE, 0.020, (RUST, 1.0))


@design("R8", "リボン")
def r8(c):
    tile(c, TEAL, (0x06, 0x5E, 0x5A), edge=None)
    c.polygon(RISE + shift(RISE, 0.0, 0.20)[::-1], (CREAM, 1.0))
    c.polygon(shift(RISE, 0.0, 0.20) + shift(RISE, 0.0, 0.29)[::-1],
              (SAND, 1.0))


@design("R9", "節点のある針金")
def r9(c):
    tile(c, DEEP, NIGHT, edge=None)
    c.stroke(RISE, 0.028, (SKY, 1.0))
    for x, y in RISE:
        c.ring(x, y, 0.055, 0.032, (WHITE, 1.0))


@design("R10", "筆")
def r10(c):
    tile(c, CREAM, SAND, edge=None)
    c.taper((0.05, 0.88), (0.30, 0.86), (0.52, 0.52), 0.05, 0.17, (INK, 1.0))
    c.taper((0.52, 0.52), (0.74, 0.30), (0.96, 0.12), 0.17, 0.02, (INK, 1.0))


# ══ R11〜R20 棒 ═════════════════════════════════════════════════════
@design("R11", "角丸の棒")
def r11(c):
    tile(c, INDIGO, (0x22, 0x1C, 0x86), edge=None)
    for cx, top, w in bar_specs():
        c.round_rect(cx - w / 2, top, cx + w / 2, 0.88, w / 2, (LIME, 1.0))


@design("R12", "グラデの棒")
def r12(c):
    tile(c, INK, NIGHT, edge=None)
    for cx, top, w in bar_specs():
        c.round_rect(cx - w / 2, top, cx + w / 2, 0.88, 0.012,
                     ((CYAN, 1.0), (VIOLET, 1.0), (0.0, 0.10), (0.0, 0.90)))


@design("R13", "上面のある棒")
def r13(c):
    tile(c, SAND, CREAM, edge=None)
    for cx, top, w in bar_specs():
        c.polygon([(cx - w / 2, top), (cx - w / 2 + 0.035, top - 0.030),
                   (cx + w / 2 + 0.035, top - 0.030), (cx + w / 2, top)],
                  (ORANGE, 1.0))
        c.round_rect(cx - w / 2, top, cx + w / 2, 0.88, 0.0, (RUST, 1.0))


@design("R14", "点を積んだ棒")
def r14(c):
    tile(c, NIGHT, INK, edge=None)
    for i, (cx, top, w) in enumerate(bar_specs()):
        y = 0.86
        while y > top:
            c.ring(cx, y, w * 0.30, 0.0, (AMBER, 1.0))
            y -= w * 0.72


@design("R15", "輪郭だけの棒")
def r15(c):
    tile(c, CORAL, (0xE0, 0x38, 0x30), edge=None)
    for cx, top, w in bar_specs():
        c.round_rect(cx - w / 2, top, cx + w / 2, 0.88, 0.02, (CREAM, 1.0))
        c.round_rect(cx - w / 2 + 0.022, top + 0.022, cx + w / 2 - 0.022, 0.90,
                     0.008, (CORAL, 1.0))


@design("R16", "分割された棒")
def r16(c):
    tile(c, DEEP, NIGHT, edge=None)
    for i, (cx, top, w) in enumerate(bar_specs()):
        for k in range(i + 1):
            y = 0.86 - k * 0.145
            c.round_rect(cx - w / 2, y - 0.115, cx + w / 2, y, 0.02,
                         (GREEN if k < i else LIME, 1.0))


@design("R17", "間隔が詰まる棒")
def r17(c):
    tile(c, PLUM, INK, edge=None)
    x = 0.08
    for i in range(7):
        w = 0.085 - i * 0.006
        top = 0.84 - i * 0.105
        c.round_rect(x, top, x + w, 0.88, 0.012, (PINK, 1.0))
        x += w + 0.055 - i * 0.006


@design("R18", "積み上げ棒")
def r18(c):
    tile(c, CREAM, BONE, edge=None)
    for i, (cx, top, w) in enumerate(bar_specs()):
        mid = top + (0.88 - top) * 0.45
        c.round_rect(cx - w / 2, top, cx + w / 2, mid, 0.01, (ORANGE, 1.0))
        c.round_rect(cx - w / 2, mid, cx + w / 2, 0.88, 0.01, (INDIGO, 1.0))


@design("R19", "浮いた棒")
def r19(c):
    tile(c, TEAL, (0x07, 0x5C, 0x58), edge=None)
    prev = 0.88
    for cx, top, w in bar_specs():
        c.round_rect(cx - w / 2, top, cx + w / 2, prev, 0.015, (CREAM, 1.0))
        prev = top + 0.03


@design("R20", "棒と頂点を結ぶ線")
def r20(c):
    tile(c, NIGHT, INK, edge=None)
    tops = []
    for cx, top, w in bar_specs():
        c.round_rect(cx - w / 2, top, cx + w / 2, 0.88, 0.012, (DEEP, 1.0))
        tops.append((cx, top))
    c.stroke(tops, 0.030, (YELLOW, 1.0))
    for x, y in tops:
        c.ring(x, y, 0.035, 0.0, (YELLOW, 1.0))


# ══ R21〜R30 段 ═════════════════════════════════════════════════════
@design("R21", "ベタの階段")
def r21(c):
    tile(c, YELLOW, AMBER, edge=None)
    c.polygon(steps_poly(), (INK, 1.0))


@design("R22", "アイソメの階段")
def r22(c):
    tile(c, SKY, BLUE, edge=None)
    for i in range(4):
        x, y = 0.14 + i * 0.19, 0.74 - i * 0.155
        c.polygon([(x, y), (x + 0.19, y - 0.10), (x + 0.19, y + 0.06),
                   (x, y + 0.16)], (CREAM, 1.0))
        c.polygon([(x, y + 0.16), (x + 0.19, y + 0.06), (x + 0.19, y + 0.155),
                   (x, y + 0.255)], (DEEP, 1.0))


@design("R23", "紙を重ねた段")
def r23(c):
    tile(c, BONE, CREAM, edge=None)
    for i in range(5):
        x, y = 0.06 + i * 0.165, 0.80 - i * 0.145
        c.round_rect(x + 0.012, y + 0.012, x + 0.30, y + 0.20, 0.02,
                     (INK, 0.18))
        c.round_rect(x, y, x + 0.288, y + 0.188, 0.02, (CORAL, 1.0))


@design("R24", "押し出しの階段")
def r24(c):
    tile(c, INK, NIGHT, edge=None)
    sp = steps_poly()
    for i in range(14, 0, -1):
        c.polygon(shift(sp, i * 0.012, -i * 0.008), (VIOLET, 1.0))
    c.polygon(sp, (MAGENTA, 1.0))


@design("R25", "等高線の階段")
def r25(c):
    tile(c, DEEP, NIGHT, edge=None)
    for k in range(7):
        c.polygon(shift(steps_poly(), 0.0, k * 0.052), (CYAN, 1.0))
        c.polygon(shift(steps_poly(), 0.0, k * 0.052 + 0.026), (DEEP, 1.0))


@design("R26", "地を抜いた階段")
def r26(c):
    tile(c, LIME, (0x9A, 0xD0, 0x10), edge=None)
    c.polygon(steps_poly(n=6), (INK, 1.0))
    c.polygon(shift(steps_poly(n=6), 0.0, 0.14), (LIME, 1.0))


@design("R27", "ドット絵の階段")
def r27(c):
    tile(c, NIGHT, INK, edge=None)
    n = 12
    for ix in range(n):
        h = 2 + int(ix * 0.72)
        for k in range(h):
            iy = n - 1 - k
            c.round_rect(ix / n, iy / n, (ix + 1) / n - 0.008,
                         (iy + 1) / n - 0.008, 0.0, (ORANGE, 1.0))


@design("R28", "タイル貼りの階段")
def r28(c):
    tile(c, CREAM, BONE, edge=None)
    inside = rr_inside(0, 0, 1, 1, 0)
    sp = steps_poly()

    def inpoly(x, y):
        cnt, j = False, len(sp) - 1
        for i in range(len(sp)):
            xi, yi = sp[i]
            xj, yj = sp[j]
            if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
                cnt = not cnt
            j = i
        return cnt
    c.shape((0, 0, 1, 1),
            lambda x, y: inpoly(x, y) and inside(x, y)
            and (x % 0.09) > 0.012 and (y % 0.09) > 0.012, (TEAL, 1.0))


@design("R29", "段ごとに色が変わる")
def r29(c):
    tile(c, INK, NIGHT, edge=None)
    cols = (INDIGO, VIOLET, MAGENTA, PINK, ORANGE)
    for i, col in enumerate(cols):
        x, y = 0.07 + i * 0.172, 0.90 - (i + 1) * 0.150
        c.round_rect(x, y, x + 0.172, 0.90, 0.01, (col, 1.0))


@design("R30", "円弧でつないだ段")
def r30(c):
    tile(c, SAND, CREAM, edge=None)
    for i in range(4):
        cx, cy = 0.26 + i * 0.20, 0.86 - i * 0.185
        c.ring(cx, cy, 0.205, 0.135, (RUST, 1.0),
               a0=math.radians(180), a1=math.radians(272))


# ══ R31〜R40 矢 ═════════════════════════════════════════════════════
@design("R31", "重ねたシェブロン")
def r31(c):
    tile(c, DEEP, NIGHT, edge=None)
    for i in range(4):
        d = i * 0.145
        c.stroke([(0.16, 0.72 - d), (0.50, 0.50 - d), (0.84, 0.72 - d)], 0.075,
                 (CYAN, 1.0 - i * 0.18))


@design("R32", "尾を引く矢")
def r32(c):
    tile(c, INK, PLUM, edge=None)
    c.stroke(RISE, 0.075, ((INK, 0.0), (CORAL, 1.0), (0.08, 0.86), (0.80, 0.24)))
    c.polygon(head((0.97, 0.09), -40, 0.30), (CORAL, 1.0))


@design("R33", "矢でできた矢")
def r33(c):
    tile(c, CREAM, BONE, edge=None)
    for i in range(6):
        t = i / 5.0
        x = 0.14 + t * 0.62
        y = 0.80 - t * 0.56
        c.polygon(head((x + 0.13, y - 0.11), -40, 0.155), (INDIGO, 1.0))


@design("R34", "天井を突き破る矢")
def r34(c):
    tile(c, NIGHT, INK, edge=None)
    c.round_rect(0.0, 0.26, 1.0, 0.335, 0.0, (SKY, 0.30))
    for k in range(5):
        c.round_rect(0.10 + k * 0.19, 0.24, 0.10 + k * 0.19 + 0.075, 0.355,
                     0.01, (SKY, 0.0))
    c.stroke([(0.30, 0.92), (0.62, 0.34)], 0.115, (YELLOW, 1.0))
    c.polygon(head((0.70, 0.05), -60, 0.30), (YELLOW, 1.0))


@design("R35", "折り紙の矢")
def r35(c):
    tile(c, BONE, CREAM, edge=None)
    c.polygon([(0.10, 0.86), (0.52, 0.60), (0.44, 0.78)], (TEAL, 1.0))
    c.polygon([(0.52, 0.60), (0.94, 0.14), (0.62, 0.72)], (GREEN, 1.0))
    c.polygon([(0.94, 0.14), (0.58, 0.30), (0.88, 0.46)], (LIME, 1.0))


@design("R36", "輪郭だけの矢")
def r36(c):
    tile(c, MAGENTA, VIOLET, edge=None)
    c.stroke(RISE_S, 0.140, (CREAM, 1.0))
    c.stroke(RISE_S, 0.075, (MAGENTA, 1.0))
    c.polygon(head((0.97, 0.10), -42, 0.28), (CREAM, 1.0))
    c.polygon(head((0.93, 0.17), -42, 0.16), (MAGENTA, 1.0))


@design("R37", "二股の矢")
def r37(c):
    tile(c, INK, DEEP, edge=None)
    c.stroke([(0.10, 0.88), (0.46, 0.60)], 0.085, (CREAM, 1.0))
    for ang, col in ((-30, AMBER), (-58, CYAN)):
        tip = (0.46 + 0.48 * math.cos(math.radians(ang)),
               0.60 + 0.48 * math.sin(math.radians(ang)))
        c.stroke([(0.46, 0.60), tip], 0.085, (col, 1.0))
        c.polygon(head(tip, ang, 0.20), (col, 1.0))


@design("R38", "円の中の矢")
def r38(c):
    tile(c, CREAM, BONE, edge=None)
    c.ring(0.5, 0.5, 0.415, 0.335, (RUST, 1.0))
    c.stroke([(0.30, 0.68), (0.66, 0.36)], 0.095, (RUST, 1.0))
    c.polygon(head((0.74, 0.28), -42, 0.22), (RUST, 1.0))


@design("R39", "彗星")
def r39(c):
    tile(c, INK, NIGHT, edge=None)
    for w, a in ((0.28, 0.10), (0.17, 0.18), (0.09, 0.34)):
        c.stroke([(0.02, 0.98), (0.72, 0.30)], w, (CYAN, a))
    c.ring(0.78, 0.24, 0.115, 0.0, (WHITE, 1.0))
    c.ring(0.78, 0.24, 0.175, 0.135, (CYAN, 0.55))


@design("R40", "帯が矢になる")
def r40(c):
    tile(c, ORANGE, AMBER, edge=None)
    c.polygon([(0.0, 0.92), (0.62, 0.36), (0.62, 0.62), (0.0, 1.02)],
              (DEEP, 1.0))
    c.polygon([(0.55, 0.50), (0.97, 0.08), (0.97, 0.55)], (DEEP, 1.0))


# ══ R41〜R50 面 ═════════════════════════════════════════════════════
@design("R41", "エリアチャート")
def r41(c):
    tile(c, CREAM, BONE, edge=None)
    p = rise(n=7, x0=-0.05, x1=1.05)
    c.polygon(p + [(1.05, 1.05), (-0.05, 1.05)], (TEAL, 1.0))
    c.stroke(p, 0.045, (INK, 1.0))


@design("R42", "掃引グラデ")
def r42(c):
    tile(c, INK, INK, edge=None)
    c.round_rect(0, 0, 1, 1, 0.215,
                 ((VIOLET, 1.0), (CYAN, 1.0), (0.05, 0.95), (0.95, 0.05)))
    c.stroke(RISE, 0.055, (WHITE, 0.95))
    c.ring(0.92, 0.11, 0.062, 0.0, (WHITE, 1.0))


@design("R43", "網点のグラデ")
def r43(c):
    tile(c, CREAM, CREAM, edge=None)
    n = 15
    for iy in range(n):
        for ix in range(n):
            x, y = (ix + 0.5) / n, (iy + 0.5) / n
            t = max(0.0, min(1.0, (x + (1 - y)) / 2))
            c.ring(x, y, 0.046 * t, 0.0, (INDIGO, 1.0))


@design("R44", "ディザの傾斜")
def r44(c):
    tile(c, YELLOW, YELLOW, edge=None)
    n = 20
    for iy in range(n):
        for ix in range(n):
            t = ((ix / (n - 1)) + (1 - iy / (n - 1))) / 2
            if t * 16 > ic.BAYER4[iy % 4][ix % 4]:
                c.round_rect(ix / n, iy / n, (ix + 1) / n, (iy + 1) / n, 0.0,
                             (PLUM, 1.0))


@design("R45", "テラゾー")
def r45(c):
    tile(c, BONE, CREAM, edge=None)
    cols = (CORAL, TEAL, AMBER, INDIGO)
    for i in range(46):
        t = (i * 0.0217) % 1.0
        x = 0.04 + ((i * 7) % 23) / 23.0 * 0.92
        y = 0.96 - ((i * 11) % 19) / 19.0 * 0.92
        if (1 - y) + x < 0.75 + t * 0.5:
            continue
        c.ring(x, y, 0.018 + (i % 3) * 0.010, 0.0, (cols[i % 4], 1.0))


@design("R46", "モザイク")
def r46(c):
    tile(c, NIGHT, INK, edge=None)
    n = 10
    for iy in range(n):
        for ix in range(n):
            t = (ix / (n - 1) + (1 - iy / (n - 1))) / 2
            if t < 0.42:
                continue
            col = (SKY if t > 0.78 else (BLUE if t > 0.60 else DEEP))
            c.round_rect(ix / n + 0.006, iy / n + 0.006,
                         (ix + 1) / n - 0.006, (iy + 1) / n - 0.006, 0.012,
                         (col, 1.0))


@design("R47", "ステンドグラス")
def r47(c):
    tile(c, INK, INK, edge=None)
    cols = (VIOLET, INDIGO, TEAL, GREEN, LIME, YELLOW, AMBER)
    for i, col in enumerate(cols):
        y0 = 1.05 - i * 0.16
        c.polygon([(-0.05, y0), (1.05, y0 - 0.42), (1.05, y0 - 0.55),
                   (-0.05, y0 - 0.13)], (col, 1.0))


@design("R48", "対角で二分")
def r48(c):
    tile(c, DEEP, DEEP, edge=None)
    c.polygon([(-0.05, 1.05), (1.05, -0.05), (1.05, 1.05)], (AMBER, 1.0))
    c.stroke([(-0.05, 1.05), (1.05, -0.05)], 0.030, (CREAM, 1.0))


@design("R49", "層になった稜線")
def r49(c):
    tile(c, PLUM, INK, edge=None)
    cols = (INDIGO, VIOLET, MAGENTA, PINK, CORAL)
    for i, col in enumerate(cols):
        p = rise(n=6, x0=-0.05, x1=1.05, y0=1.02 - i * 0.02,
                 y1=0.62 - i * 0.145, bow=0.05)
        c.polygon(p + [(1.05, 1.05), (-0.05, 1.05)], (col, 1.0))


@design("R50", "地形の等高塗り")
def r50(c):
    tile(c, (0x06, 0x2A, 0x3A), (0x04, 0x18, 0x26), edge=None)
    cols = ((0x0B, 0x4C, 0x5E), TEAL, GREEN, LIME, YELLOW)
    for i, col in enumerate(cols):
        c.shape((0, 0, 1, 1),
                (lambda k: lambda x, y: (x + (1 - y)) / 2 > 0.40 + k * 0.105)(i),
                (col, 1.0))


# ══ R51〜R60 光 ═════════════════════════════════════════════════════
@design("R51", "日の出")
def r51(c):
    tile(c, (0x2A, 0x12, 0x4E), (0x0A, 0x06, 0x20), edge=None)
    for rad, col in ((0.95, MAGENTA), (0.66, CORAL), (0.44, AMBER),
                     (0.26, YELLOW)):
        c.ring(0.70, 0.66, rad, 0.0,
               ("r", (col, 0.95), (col, 0.0), (0.70, 0.66), rad))
    c.ring(0.70, 0.66, 0.175, 0.0, ((0xFF, 0xF6, 0xC8), 1.0))
    c.polygon([(-0.05, 1.05), (-0.05, 0.92), (1.05, 0.66), (1.05, 1.05)],
              ((0x0A, 0x06, 0x20), 1.0))


@design("R52", "放射光")
def r52(c):
    tile(c, AMBER, ORANGE, edge=None)
    for i in range(9):
        a = -95 + i * 11.0
        c.polygon([(0.10, 0.92)] + [(0.10 + 1.5 * math.cos(math.radians(v)),
                                     0.92 + 1.5 * math.sin(math.radians(v)))
                                    for v in (a, a + 5.5)], (CREAM, 0.85))
    c.stroke(RISE, 0.050, (RUST, 1.0))


@design("R53", "残光")
def r53(c):
    tile(c, INK, NIGHT, edge=None)
    for i in range(9):
        t = i / 8.0
        p = rise(n=5, x0=0.08 - t * 0.06, y0=0.86 + t * 0.05,
                 x1=0.94 - t * 0.10, y1=0.13 + t * 0.09)
        c.stroke(p, 0.075, (GREEN, 0.10 + 0.10 * (1 - t)))
    c.stroke(RISE, 0.055, (LIME, 1.0))


@design("R54", "ネオン（二色）")
def r54(c):
    tile(c, (0x12, 0x06, 0x24), INK, edge=None)
    for w, a in ((0.24, 0.12), (0.14, 0.20)):
        c.stroke(RISE, w, (CYAN, a))
    c.stroke(RISE, 0.048, (CYAN, 1.0))
    c.stroke(shift(RISE, 0.0, 0.13), 0.048, (PINK, 1.0))


@design("R55", "メッシュグラデ")
def r55(c):
    tile(c, INDIGO, VIOLET, edge=None)
    for cx, cy, rad, col in ((0.10, 0.92, 0.70, MAGENTA), (0.92, 0.10, 0.70, CYAN),
                             (0.55, 0.45, 0.50, PINK)):
        c.ring(cx, cy, rad, 0.0,
               ("r", (col, 0.80), (col, 0.0), (cx, cy), rad), mode="screen")
    c.stroke(RISE, 0.038, (WHITE, 0.90))


@design("R56", "レンズフレア")
def r56(c):
    tile(c, INK, NIGHT, edge=None)
    c.stroke([(0.02, 0.98), (0.98, 0.02)], 0.016, (SKY, 0.55))
    for t, rad, col in ((0.28, 0.055, VIOLET), (0.46, 0.085, TEAL),
                        (0.62, 0.045, AMBER), (0.80, 0.13, CORAL)):
        c.ring(0.02 + t * 0.96, 0.98 - t * 0.96, rad, rad * 0.6, (col, 0.75))
    c.ring(0.86, 0.14, 0.10, 0.0, (WHITE, 1.0))
    c.ring(0.86, 0.14, 0.30, 0.0, ("r", (WHITE, 0.35), (WHITE, 0.0),
                                   (0.86, 0.14), 0.30))


@design("R57", "オーロラの帯")
def r57(c):
    tile(c, (0x05, 0x0E, 0x24), INK, edge=None)
    for i, col in enumerate((TEAL, GREEN, CYAN, VIOLET)):
        p = rise(n=7, x0=-0.05, x1=1.05, y0=1.00 - i * 0.10,
                 y1=0.42 - i * 0.10, bow=0.10)
        c.stroke(p, 0.075, (col, 0.65), mode="screen")


@design("R58", "ビーム")
def r58(c):
    tile(c, NIGHT, INK, edge=None)
    c.polygon([(0.0, 1.02), (0.0, 0.86), (1.02, 0.02), (1.02, 0.34)],
              ((YELLOW, 0.30), (YELLOW, 1.0), (0.0, 1.0), (1.0, 0.0)))
    c.stroke([(0.0, 0.94), (1.02, 0.18)], 0.020, (WHITE, 0.9))


@design("R59", "火花")
def r59(c):
    tile(c, INK, (0x24, 0x0A, 0x02), edge=None)
    c.stroke(RISE, 0.030, (AMBER, 0.55))
    for i in range(26):
        t = (i % 13) / 12.0
        x = 0.08 + t * 0.86 + ((i * 37) % 11 - 5) * 0.012
        y = 0.86 - t * 0.73 + ((i * 53) % 11 - 5) * 0.014
        c.ring(x, y, 0.008 + (i % 3) * 0.007, 0.0,
               (YELLOW if i % 2 else ORANGE, 1.0))


@design("R60", "夜明け")
def r60(c):
    tile(c, INK, INK, edge=None)
    cols = ((0x08, 0x0E, 0x2E), INDIGO, VIOLET, MAGENTA, CORAL, AMBER, YELLOW)
    for i, col in enumerate(cols):
        y = 1.10 - i * 0.165
        c.polygon([(-0.05, y), (1.05, y - 0.30), (1.05, y - 0.47),
                   (-0.05, y - 0.17)], (col, 1.0))


# ══ R61〜R70 生きもの ═══════════════════════════════════════════════
@design("R61", "芽")
def r61(c):
    tile(c, CREAM, BONE, edge=None)
    c.taper((0.42, 0.94), (0.46, 0.60), (0.62, 0.20), 0.075, 0.030, (TEAL, 1.0))
    c.taper((0.55, 0.52), (0.86, 0.44), (0.90, 0.16), 0.010, 0.150, (GREEN, 1.0))
    c.taper((0.50, 0.62), (0.20, 0.58), (0.16, 0.32), 0.010, 0.115, (LIME, 1.0))


@design("R62", "葉")
def r62(c):
    tile(c, (0x06, 0x36, 0x2E), (0x03, 0x1E, 0x1A), edge=None)
    c.polygon(qbez((0.10, 0.90), (0.20, 0.24), (0.90, 0.12))
              + qbez((0.90, 0.12), (0.30, 0.82), (0.10, 0.90)), (LIME, 1.0))
    c.stroke([(0.10, 0.90), (0.90, 0.12)], 0.020, (TEAL, 1.0))


@design("R63", "蔓")
def r63(c):
    tile(c, BONE, CREAM, edge=None)
    p = [(0.12 + 0.80 * t / 24, 0.90 - 0.76 * t / 24
          + 0.055 * math.sin(t / 24 * 9)) for t in range(25)]
    c.stroke(p, 0.032, (TEAL, 1.0))
    for i in (5, 11, 17, 23):
        c.ring(p[i][0], p[i][1] - 0.055, 0.045, 0.0, (GREEN, 1.0))


@design("R64", "炎")
def r64(c):
    tile(c, INK, (0x2A, 0x08, 0x00), edge=None)
    for w0, col in ((0.42, RUST), (0.30, ORANGE), (0.18, AMBER), (0.08, YELLOW)):
        c.taper((0.40, 0.94), (0.30, 0.52), (0.74, 0.08), w0, 0.02, (col, 1.0))


@design("R65", "泡")
def r65(c):
    tile(c, (0x04, 0x2A, 0x4E), (0x02, 0x14, 0x2E), edge=None)
    for i in range(11):
        t = i / 10.0
        x = 0.14 + t * 0.74 + 0.05 * math.sin(t * 8)
        y = 0.92 - t * 0.80
        r = 0.022 + t * 0.070
        c.ring(x, y, r, r * 0.72, (CYAN, 1.0))


@design("R66", "羽根")
def r66(c):
    tile(c, PLUM, INK, edge=None)
    for i in range(7):
        t = i / 6.0
        c.taper((0.12 + t * 0.62, 0.90 - t * 0.62),
                (0.24 + t * 0.62, 0.80 - t * 0.68),
                (0.30 + t * 0.66, 0.62 - t * 0.56), 0.075, 0.012,
                (PINK if i % 2 else CORAL, 1.0))


@design("R67", "翼")
def r67(c):
    tile(c, DEEP, NIGHT, edge=None)
    for i in range(5):
        d = i * 0.075
        c.taper((0.08 + d * 0.4, 0.86 - d * 0.2), (0.42, 0.66 - d),
                (0.94, 0.34 - d), 0.055, 0.012, (SKY, 1.0 - i * 0.13))


@design("R68", "波頭")
def r68(c):
    tile(c, (0x04, 0x1E, 0x40), INK, edge=None)
    c.taper((-0.08, 1.00), (0.30, 0.36), (0.98, 0.20), 0.38, 0.04, (CREAM, 1.0))
    c.taper((0.02, 1.04), (0.36, 0.48), (0.98, 0.32), 0.34, 0.04, (CYAN, 1.0))
    c.taper((0.12, 1.08), (0.44, 0.62), (1.00, 0.46), 0.30, 0.04, (BLUE, 1.0))


@design("R69", "煙")
def r69(c):
    tile(c, BONE, CREAM, edge=None)
    for i in range(9):
        t = i / 8.0
        x = 0.16 + t * 0.68 + 0.06 * math.sin(t * 7)
        y = 0.90 - t * 0.78
        c.ring(x, y, 0.035 + t * 0.075, 0.0, ((0x6A, 0x72, 0x82), 0.55))


@design("R70", "種から茎")
def r70(c):
    tile(c, (0x2A, 0x1A, 0x08), (0x14, 0x0C, 0x04), edge=None)
    c.ring(0.16, 0.88, 0.075, 0.0, (SAND, 1.0))
    c.taper((0.16, 0.88), (0.40, 0.70), (0.86, 0.14), 0.055, 0.018,
            (LIME, 1.0))
    c.ring(0.86, 0.14, 0.085, 0.0, (YELLOW, 1.0))


# ══ R71〜R80 幾何 ═══════════════════════════════════════════════════
@design("R71", "積み木")
def r71(c):
    tile(c, SKY, BLUE, edge=None)
    for i in range(4):
        x, y = 0.16 + i * 0.18, 0.78 - i * 0.16
        c.polygon([(x, y), (x + 0.16, y - 0.09), (x + 0.32, y),
                   (x + 0.16, y + 0.09)], (CREAM, 1.0))
        c.polygon([(x, y), (x + 0.16, y + 0.09), (x + 0.16, y + 0.20),
                   (x, y + 0.11)], (DEEP, 1.0))
        c.polygon([(x + 0.16, y + 0.09), (x + 0.32, y), (x + 0.32, y + 0.11),
                   (x + 0.16, y + 0.20)], (INDIGO, 1.0))


@design("R72", "上昇する螺旋")
def r72(c):
    tile(c, INK, PLUM, edge=None)
    p = []
    for t in range(61):
        u = t / 60.0
        rr = 0.085 + u * 0.145
        p.append((0.14 + u * 0.72 + rr * math.cos(u * 11.0),
                  0.88 - u * 0.76 + rr * 0.42 * math.sin(u * 11.0)))
    c.stroke(p, 0.042, ((VIOLET, 1.0), (PINK, 1.0), (0.14, 0.88), (0.90, 0.14)))


@design("R73", "フィボナッチ")
def r73(c):
    tile(c, CREAM, BONE, edge=None)
    x, y, s = 0.86, 0.86, 0.055
    for i in range(6):
        c.ring(x, y, s, s - 0.024, (RUST, 1.0),
               a0=math.radians(180), a1=math.radians(270))
        x -= s * 0.30
        y -= s * 0.30
        s *= 1.42


@design("R74", "デルタ")
def r74(c):
    tile(c, GREEN, TEAL, edge=None)
    c.polygon([(0.50, 0.10), (0.90, 0.78), (0.10, 0.78)], (INK, 1.0))
    c.polygon([(0.50, 0.34), (0.74, 0.70), (0.26, 0.70)], (GREEN, 1.0))
    c.round_rect(0.10, 0.84, 0.90, 0.90, 0.02, (INK, 1.0))


@design("R75", "入れ子のシェブロン")
def r75(c):
    tile(c, ORANGE, AMBER, edge=None)
    for i in range(5):
        d = i * 0.105
        c.stroke([(0.06 + d * 0.5, 0.90 - d * 0.2), (0.50, 0.62 - d),
                  (0.94 - d * 0.5, 0.90 - d * 0.2)], 0.048,
                 (DEEP if i % 2 else CREAM, 1.0))


@design("R76", "傾いた格子")
def r76(c):
    tile(c, NIGHT, INK, edge=None)
    for i in range(-6, 14):
        c.stroke([(i * 0.10, 1.06), (i * 0.10 + 0.55, -0.06)], 0.014,
                 (TEAL, 0.9))
    for k in range(9):
        y = k * 0.13
        c.stroke([(-0.06, y + 0.10), (1.06, y - 0.20)], 0.014, (TEAL, 0.9))


@design("R77", "パース線")
def r77(c):
    tile(c, INDIGO, (0x1E, 0x18, 0x70), edge=None)
    for i in range(11):
        a = -95 + i * 9.5
        c.stroke(radial(0.88, 0.12, a, 0.0, 1.6), 0.014, (LIME, 0.8))
    for k in range(1, 7):
        r = k * k * 0.045
        c.ring(0.88, 0.12, r + 0.008, r - 0.008, (LIME, 0.8))


@design("R78", "六角の積み上げ")
def r78(c):
    tile(c, CREAM, BONE, edge=None)
    for i in range(5):
        for k in range(i + 1):
            cx = 0.14 + i * 0.185
            cy = 0.86 - k * 0.155
            hexa = [(cx + 0.082 * math.cos(math.radians(a + 30)),
                     cy + 0.082 * math.sin(math.radians(a + 30)))
                    for a in range(0, 360, 60)]
            c.polygon(hexa, (VIOLET if (i + k) % 2 else INDIGO, 1.0))


@design("R79", "菱形の梯子")
def r79(c):
    tile(c, PLUM, INK, edge=None)
    for i in range(6):
        t = i / 5.0
        cx, cy = 0.14 + t * 0.72, 0.88 - t * 0.76
        s = 0.055 + t * 0.045
        c.polygon([(cx, cy - s), (cx + s, cy), (cx, cy + s), (cx - s, cy)],
                  (PINK if i % 2 else CORAL, 1.0))


@design("R80", "軌道")
def r80(c):
    tile(c, INK, NIGHT, edge=None)
    for k, col in enumerate((DEEP, BLUE, SKY)):
        c.ring(0.14, 0.92, 0.34 + k * 0.24, 0.32 + k * 0.24, (col, 1.0),
               a0=math.radians(255), a1=math.radians(350))
    c.ring(0.82, 0.30, 0.075, 0.0, (YELLOW, 1.0))


# ══ R81〜R90 印刷 ═══════════════════════════════════════════════════
@design("R81", "リソグラフ")
def r81(c):
    tile(c, (0xF2, 0xEC, 0xDE), (0xE2, 0xDA, 0xC8), edge=None)
    c.stroke(shift(RISE, -0.018, -0.018), 0.115, ((0x00, 0xA5, 0xE0), 0.94),
             mode="multiply")
    c.stroke(shift(RISE, 0.018, 0.018), 0.115, ((0xFF, 0x48, 0x8B), 0.94),
             mode="multiply")


@design("R82", "CMYのズレ")
def r82(c):
    tile(c, (0xFB, 0xFB, 0xFD), (0xEC, 0xEC, 0xF2), edge=None)
    for dx, dy, col in ((-0.030, 0.012, (0x00, 0xC8, 0xE8)),
                        (0.030, -0.010, (0xFF, 0x1F, 0x9C)),
                        (0.004, 0.030, (0xFF, 0xE3, 0x00))):
        c.stroke(shift(RISE, dx, dy), 0.105, (col, 1.0), mode="multiply")


@design("R83", "網点")
def r83(c):
    tile(c, YELLOW, YELLOW, edge=None)
    n = 14
    near = ic.near_polyline(RISE, 0.30)
    for iy in range(n):
        for ix in range(n):
            x, y = (ix + 0.5) / n, (iy + 0.5) / n
            d = dist_to_polyline(RISE, x, y)
            r = max(0.0, 0.048 * (1.0 - d * 4.0))
            if r > 0.004 or near(x, y):
                c.ring(x, y, max(r, 0.006), 0.0, (RUST, 1.0))


@design("R84", "銅版のハッチング")
def r84(c):
    tile(c, BONE, CREAM, edge=None)
    for i in range(30):
        x = -0.35 + i * 0.055
        c.stroke([(x, 1.06), (x + 0.42, -0.06)], 0.009, (INK, 0.85))
    near = ic.near_polyline(RISE, 0.19)
    c.shape((0, 0, 1, 1), near, (CORAL, 1.0))


@design("R85", "かすれた刷り")
def r85(c):
    tile(c, TEAL, (0x08, 0x74, 0x70), edge=None)
    c.stroke(RISE, 0.145, (CREAM, 1.0))
    for i in range(34):
        t = (i * 0.0294) % 1.0
        p = rise(n=2, x0=0.08 + t * 0.80, x1=0.14 + t * 0.80,
                 y0=0.86 - t * 0.70, y1=0.83 - t * 0.70, bow=0.0)
        c.stroke(p, 0.010 + (i % 3) * 0.008, (TEAL, 0.9))


@design("R86", "青焼き")
def r86(c):
    tile(c, (0x0E, 0x36, 0x82), (0x08, 0x24, 0x5E), edge=None)
    for k in range(11):
        c.stroke([(k * 0.10, -0.05), (k * 0.10, 1.05)], 0.006, (WHITE, 0.28))
        c.stroke([(-0.05, k * 0.10), (1.05, k * 0.10)], 0.006, (WHITE, 0.28))
    c.stroke(RISE, 0.026, (WHITE, 1.0))
    for x, y in RISE:
        c.ring(x, y, 0.030, 0.018, (WHITE, 1.0))


@design("R87", "空押し")
def r87(c):
    tile(c, SAND, (0xD8, 0xB4, 0x72), edge=None)
    c.stroke(shift(RISE, 0.012, 0.012), 0.120, ((0xFF, 0xFF, 0xFF), 0.55))
    c.stroke(shift(RISE, -0.010, -0.010), 0.120, ((0x6A, 0x50, 0x1E), 0.45))
    c.stroke(RISE, 0.120, (SAND, 1.0))


@design("R88", "デュオトーン")
def r88(c):
    tile(c, (0x1A, 0x0E, 0x3E), (0x0A, 0x06, 0x1E), edge=None)
    n = 26
    for i in range(n):
        y = i / n
        t = 1.0 - y
        c.round_rect(0.0, y, min(1.0, 0.10 + t * 1.15), y + 1.0 / n, 0.0,
                     ((MAGENTA, 1.0), (YELLOW, 1.0), (0.0, 1.0), (0.0, 0.0)))


@design("R89", "モアレ")
def r89(c):
    tile(c, CREAM, CREAM, edge=None)
    for i in range(34):
        x = -0.3 + i * 0.045
        c.stroke([(x, 1.06), (x + 0.5, -0.06)], 0.011, (INDIGO, 0.9))
    for i in range(34):
        x = -0.3 + i * 0.047
        c.stroke([(x, 1.06), (x + 0.58, -0.06)], 0.011, (CORAL, 0.55))


@design("R90", "新聞の粗い網点")
def r90(c):
    tile(c, (0xE8, 0xE4, 0xDA), (0xD8, 0xD2, 0xC4), edge=None)
    n = 11
    for iy in range(n):
        for ix in range(n):
            x, y = (ix + 0.5) / n, (iy + 0.5) / n
            t = max(0.0, min(1.0, (x + (1 - y)) / 2 - 0.15))
            if t > 0.02:
                c.ring(x, y, 0.048 * t, 0.0, (INK, 1.0))


# ══ R91〜R100 もの ══════════════════════════════════════════════════
@design("R91", "ロケット")
def r91(c):
    tile(c, (0x08, 0x0E, 0x28), INK, edge=None)
    for w, a in ((0.20, 0.16), (0.11, 0.30)):
        c.stroke([(0.04, 0.96), (0.58, 0.42)], w, (AMBER, a))
    body = [(0.00, -0.16), (0.44, -0.16), (0.72, -0.09), (0.92, 0.0),
            (0.72, 0.09), (0.44, 0.16), (0.00, 0.16)]
    c.polygon(xform(body, 0.62, -45, 0.50, 0.50), (CREAM, 1.0))
    c.polygon(xform([(0.10, -0.16), (0.30, -0.40), (0.34, -0.14)], 0.62, -45,
                    0.50, 0.50), (CORAL, 1.0))
    c.polygon(xform([(0.10, 0.16), (0.30, 0.40), (0.34, 0.14)], 0.62, -45,
                    0.50, 0.50), (CORAL, 1.0))


@design("R92", "凧")
def r92(c):
    tile(c, SKY, BLUE, edge=None)
    c.polygon([(0.70, 0.10), (0.92, 0.34), (0.70, 0.56), (0.48, 0.34)],
              (CORAL, 1.0))
    c.stroke([(0.70, 0.10), (0.70, 0.56)], 0.012, (CREAM, 1.0))
    c.stroke([(0.48, 0.34), (0.92, 0.34)], 0.012, (CREAM, 1.0))
    c.stroke([(0.70, 0.56), (0.44, 0.74), (0.26, 0.80), (0.08, 0.94)], 0.018,
             (CREAM, 1.0))


@design("R93", "気球")
def r93(c):
    tile(c, (0xFF, 0xE3, 0xB0), SAND, edge=None)
    c.ring(0.62, 0.34, 0.245, 0.0, (CORAL, 1.0))
    c.polygon([(0.62 - 0.245, 0.36), (0.62 + 0.245, 0.36), (0.62, 0.66)],
              (CORAL, 1.0))
    c.stroke([(0.55, 0.63), (0.58, 0.74)], 0.012, (RUST, 1.0))
    c.stroke([(0.69, 0.63), (0.66, 0.74)], 0.012, (RUST, 1.0))
    c.round_rect(0.555, 0.74, 0.685, 0.83, 0.015, (RUST, 1.0))
    c.stroke([(0.06, 0.96), (0.30, 0.86), (0.48, 0.80)], 0.014, (RUST, 1.0))


@design("R94", "梯子")
def r94(c):
    tile(c, DEEP, NIGHT, edge=None)
    a = [(0.04, 0.98), (0.72, 0.02)]
    b = [(0.28, 1.06), (0.96, 0.10)]
    c.stroke(a, 0.038, (AMBER, 1.0))
    c.stroke(b, 0.038, (AMBER, 1.0))
    for i in range(7):
        t = i / 6.0
        c.stroke([(a[0][0] + (a[1][0] - a[0][0]) * t,
                   a[0][1] + (a[1][1] - a[0][1]) * t),
                  (b[0][0] + (b[1][0] - b[0][0]) * t,
                   b[0][1] + (b[1][1] - b[0][1]) * t)], 0.030, (AMBER, 1.0))


@design("R95", "エスカレーター")
def r95(c):
    tile(c, BONE, CREAM, edge=None)
    c.polygon([(0.02, 0.98), (0.98, 0.16), (0.98, 0.34), (0.02, 1.06)],
              (INDIGO, 1.0))
    for i in range(8):
        t = i / 7.0
        x, y = 0.06 + t * 0.86, 0.95 - t * 0.80
        c.round_rect(x, y - 0.055, x + 0.075, y, 0.008, (SKY, 1.0))
    c.stroke([(0.02, 0.86), (0.98, 0.04)], 0.026, (CORAL, 1.0))


@design("R96", "温度計")
def r96(c):
    tile(c, CREAM, BONE, edge=None)
    c.round_rect(0.10, 0.60, 0.90, 0.76, 0.08, ((0xD8, 0xD2, 0xC4), 1.0))
    c.round_rect(0.12, 0.62, 0.74, 0.74, 0.06,
                 ((AMBER, 1.0), (CORAL, 1.0), (0.12, 0.0), (0.74, 0.0)))
    c.ring(0.14, 0.68, 0.115, 0.0, (CORAL, 1.0))
    for i in range(8):
        x = 0.18 + i * 0.095
        c.stroke([(x, 0.52), (x, 0.58)], 0.014, (INK, 0.75))


@design("R97", "計器の針")
def r97(c):
    tile(c, INK, NIGHT, edge=None)
    for i, col in enumerate((CORAL, AMBER, YELLOW, LIME, GREEN)):
        c.ring(0.5, 0.78, 0.44, 0.34, (col, 1.0),
               a0=math.radians(180 + i * 36), a1=math.radians(180 + (i + 1) * 36))
    c.stroke([(0.5, 0.78), (0.80, 0.42)], 0.036, (CREAM, 1.0))
    c.ring(0.5, 0.78, 0.055, 0.0, (CREAM, 1.0))


@design("R98", "エレベーター")
def r98(c):
    tile(c, NIGHT, INK, edge=None)
    c.round_rect(0.30, 0.06, 0.70, 0.94, 0.02, ((0x1E, 0x2C, 0x50), 1.0))
    c.round_rect(0.34, 0.12, 0.66, 0.44, 0.02, (SKY, 1.0))
    for i in range(5):
        y = 0.86 - i * 0.10
        c.ring(0.20, y, 0.030, 0.0, (AMBER if i > 2 else (0x2A, 0x38, 0x5E), 1.0))
    c.polygon(head((0.80, 0.14), -90, 0.16), (AMBER, 1.0))


@design("R99", "山頂の旗")
def r99(c):
    tile(c, (0xFF, 0xD9, 0x9E), SAND, edge=None)
    c.polygon([(-0.05, 1.05), (0.60, 0.22), (1.05, 1.05)], (INDIGO, 1.0))
    c.polygon([(0.60, 0.22), (0.78, 0.55), (0.42, 0.55)], (CREAM, 1.0))
    c.stroke([(0.60, 0.22), (0.60, -0.02)], 0.020, (RUST, 1.0))
    c.polygon([(0.60, 0.00), (0.88, 0.09), (0.60, 0.18)], (CORAL, 1.0))


@design("R100", "稜線")
def r100(c):
    tile(c, (0xFF, 0xC8, 0x7A), (0xFF, 0x9E, 0x4A), edge=None)
    c.ring(0.30, 0.30, 0.135, 0.0, (CREAM, 1.0))
    for i, col in enumerate((VIOLET, INDIGO, DEEP)):
        p = rise(n=6, x0=-0.05, x1=1.05, y0=1.02 - i * 0.03,
                 y1=0.55 - i * 0.16, bow=0.07)
        c.polygon(p + [(1.05, 1.05), (-0.05, 1.05)], (col, 1.0))


RISING = DESIGNS
ic.ALL.extend(RISING)


def main():
    os.makedirs(ic.OUTDIR, exist_ok=True)
    if "--ico" in sys.argv:
        key = sys.argv[sys.argv.index("--ico") + 1].upper()
        try:
            path, name, removed = ic.apply_icon(key)
        except KeyError:
            print(f"そんな案はありません: {key}")
            return 1
        print(f"{key}（{name}）に切り替えました")
        print(f"  {path}  ({os.path.getsize(path) // 1024}KB)")
        for r in removed:
            print(f"  前の {r} を削除")
        print("\n最後にショートカットを作り直してください:\n  ショートカットを作る.bat")
        return 0

    for key, name, fn in RISING:
        print(f"  {key} {name}")
    p = os.path.join(ic.OUTDIR, "rising.png")
    with open(p, "wb") as f:
        f.write(ic.png_bytes(ic.build_grid(RISING, cols=10).px))
    print(f"一覧シート: {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
