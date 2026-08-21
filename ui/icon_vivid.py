# -*- coding: utf-8 -*-
"""極彩色のアイコン案50種。デスクトップで埋もれないことだけを狙う。

これまでの案（icon_candidates.py の墨・生成り、icon_rising.py の中間色）は
落ち着いて見えるぶん、他のアイコンに紛れて見つけにくかった。
このファイルは**彩度と明度差を最大に振った案だけ**を集める。

守っていること:
  - 右肩上がりだけを描く。V字（下げてから上げる）は損益がしゃがんで見えるので使わない
    （icon_rising.py の方針をそのまま引き継ぐ）
  - 図形は3〜5個まで。16pxで潰れるとデスクトップでは何も伝わらない
  - 地と紋は必ず補色か明度差2段以上。同系色でまとめない（目立たなくなる）

案は5群×10案。群ごとに「目立たせ方」を変える。
    V1〜V10    白抜き   極彩の地に白の紋。いちばん強い対比
    V11〜V20   補色     二色を正面からぶつける
    V21〜V30   多色     虹・階調。色数そのもので目を引く
    V31〜V40   ネオン   暗い地に発光色。輪郭がにじんで光る
    V41〜V50   ポップ   面で塗り分ける。輪郭線を持たない

描画の仕組みは icon_candidates.py のものを使う（あちらが本体、ここは案の定義）。

実行:
    python ui/icon_vivid.py             一覧シートを書き出す
    python ui/icon_vivid.py --png       1案ずつ256pxでも書き出す
    python ui/icon_vivid.py --ico V23   案を確定してアイコンにする
出力:
    ui/icon_preview/vivid.png           一覧シート
"""
import math
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

import icon_candidates as ic  # noqa: E402

tile, rr_inside, qbez = ic.tile, ic.rr_inside, ic.qbez

# ── 色 ──────────────────────────────────────────────────────────────
# 彩度を落とさない。くすませると「上品だが見つけられない」アイコンになる。
INK = (0x08, 0x0A, 0x14)
NAVY = (0x10, 0x1B, 0x48)
PLUM = (0x2A, 0x06, 0x3E)
BLUE = (0x1B, 0x5A, 0xF0)
AZURE = (0x18, 0x9E, 0xFF)
CYAN = (0x0A, 0xE2, 0xF2)
TEAL = (0x00, 0xC6, 0xAC)
GREEN = (0x1E, 0xD7, 0x60)
LIME = (0xBE, 0xF7, 0x18)
YELLOW = (0xFF, 0xE2, 0x1B)
AMBER = (0xFF, 0xB2, 0x0A)
ORANGE = (0xFF, 0x70, 0x10)
CORAL = (0xFF, 0x4D, 0x4D)
PINK = (0xFF, 0x2D, 0x8E)
MAGENTA = (0xE4, 0x22, 0xD6)
VIOLET = (0x8B, 0x2F, 0xF0)
INDIGO = (0x4A, 0x2A, 0xE0)
WHITE = (0xFF, 0xFF, 0xFF)
CREAM = (0xFF, 0xF6, 0xE8)

RAINBOW = (VIOLET, INDIGO, BLUE, CYAN, GREEN, LIME, YELLOW, AMBER, ORANGE, PINK)
HOT = (PINK, CORAL, ORANGE, AMBER, YELLOW)
COOL = (VIOLET, INDIGO, BLUE, AZURE, CYAN)


# ── 骨格。すべて x が増えると y が減る（＝右上がり）──────────────────
def rise(n=5, x0=0.08, x1=0.94, y0=0.84, y1=0.15, bow=0.06):
    """右上がりの折れ線。bow で下に膨らませ、加速して見せる。"""
    out = []
    for i in range(n):
        t = i / (n - 1)
        out.append((x0 + (x1 - x0) * t,
                    y0 + (y1 - y0) * t + bow * math.sin(math.pi * t)))
    return out


RISE = rise()


def head(tip, deg, size, spread=0.42):
    """進行方向 deg（画面座標・上向きは負）に向いた三角の矢尻。"""
    a = math.radians(deg)
    return [tip] + [(tip[0] - size * math.cos(a + d),
                     tip[1] - size * math.sin(a + d)) for d in (spread, -spread)]


def shift(pts, dx, dy):
    return [(x + dx, y + dy) for x, y in pts]


def bars(n=5, x0=0.11, x1=0.89, ybot=0.86, ytop=0.16, gap=0.32):
    """右へ行くほど高くなる棒。(中心x, 上端, 幅) を返す。"""
    w = (x1 - x0) / n
    for i in range(n):
        yield (x0 + w * (i + 0.5), ybot - (ybot - ytop) * (i + 1) / n, w * (1 - gap))


def wash(c, paint):
    """タイル全面を塗り直す。tile() のあとに呼ぶと角丸で切られる。"""
    c.shape((0.0, 0.0, 1.0, 1.0), lambda x, y: True, paint)


def lin(c0, c1, p0=(0.0, 0.0), p1=(1.0, 1.0)):
    """線形グラデーションの塗り指定を短く書くための糖衣。"""
    return ((c0, 1.0), (c1, 1.0), p0, p1)


def rad(c0, c1, center=(0.5, 0.5), r=0.75):
    return ("r", (c0, 1.0), (c1, 1.0), center, r)


def bands(c, cols, horizontal=False):
    """色数ぶんの帯で埋める。2色しか扱えないグラデの代わりに虹を作る。"""
    n = len(cols)
    for i, col in enumerate(cols):
        a, b = i / n, (i + 1) / n
        if horizontal:
            c.shape((0.0, a, 1.0, b), lambda x, y, a=a, b=b: a <= y < b, (col, 1.0))
        else:
            c.shape((a, 0.0, b, 1.0), lambda x, y, a=a, b=b: a <= x < b, (col, 1.0))


def glow(c, pts, width, col, layers=4, spread=2.6):
    """同じ線を太→細に重ねてにじませる。ネオン群の発光はこれで作る。"""
    for i in range(layers, 0, -1):
        c.stroke(pts, width * (1 + spread * i / layers),
                 (col, 0.13 + 0.05 * (layers - i)))
    c.stroke(pts, width, (col, 1.0))


def glow_ring(c, cx, cy, r, w, col, layers=4, spread=2.6):
    for i in range(layers, 0, -1):
        e = w * spread * i / layers / 2
        c.ring(cx, cy, r + w / 2 + e, r - w / 2 - e, (col, 0.12))
    c.ring(cx, cy, r + w / 2, r - w / 2, (col, 1.0))


DESIGNS = []


def design(key, name):
    def deco(fn):
        DESIGNS.append((key, name, fn))
        return fn
    return deco


# ══ V1〜V10 白抜き ═════════════════════════════════════════════════
# 極彩の地に白。アイコンの対比としてはこれ以上強いものがない。
@design("V1", "白矢／熱")
def v1(c):
    tile(c, PINK, ORANGE, edge=None)
    c.stroke(RISE, 0.145, (WHITE, 1.0))
    c.polygon(head(RISE[-1], -38, 0.30), (WHITE, 1.0))


@design("V2", "白矢／寒")
def v2(c):
    tile(c, VIOLET, AZURE, edge=None)
    c.stroke(RISE, 0.145, (WHITE, 1.0))
    c.polygon(head(RISE[-1], -38, 0.30), (WHITE, 1.0))


@design("V3", "白棒／桃橙")
def v3(c):
    tile(c, MAGENTA, ORANGE, edge=None)
    for cx, top, w in bars():
        c.bar(cx, top, 0.88, w, (WHITE, 1.0))


@design("V4", "白棒／碧")
def v4(c):
    tile(c, TEAL, BLUE, edge=None)
    for cx, top, w in bars():
        c.bar(cx, top, 0.88, w, (WHITE, 1.0))


@design("V5", "白段／黄橙")
def v5(c):
    tile(c, YELLOW, ORANGE, edge=None)
    for i in range(4):
        x, y = 0.10 + i * 0.21, 0.80 - i * 0.19
        c.round_rect(x, y, x + 0.19, 0.90, 0.03, (WHITE, 1.0))


@design("V6", "白の太矢")
def v6(c):
    tile(c, CORAL, PINK, edge=None)
    c.polygon([(0.10, 0.86), (0.62, 0.30), (0.62, 0.48), (0.10, 0.86)], (WHITE, 0.0))
    c.stroke([(0.12, 0.84), (0.70, 0.26)], 0.19, (WHITE, 1.0))
    c.polygon(head((0.84, 0.16), -45, 0.34), (WHITE, 1.0))


@design("V7", "白丸の階段")
def v7(c):
    tile(c, INDIGO, MAGENTA, edge=None)
    for i in range(5):
        c.ring(0.14 + i * 0.18, 0.82 - i * 0.16, 0.085, 0.0, (WHITE, 1.0))


@design("V8", "白三角／緑")
def v8(c):
    tile(c, GREEN, LIME, edge=None)
    c.polygon([(0.08, 0.88), (0.92, 0.88), (0.92, 0.14)], (WHITE, 1.0))


@design("V9", "白の稲妻")
def v9(c):
    tile(c, VIOLET, PINK, edge=None)
    c.polygon([(0.52, 0.06), (0.24, 0.54), (0.44, 0.54),
               (0.34, 0.94), (0.72, 0.42), (0.50, 0.42), (0.64, 0.06)], (WHITE, 1.0))


@design("V10", "白の二重線")
def v10(c):
    tile(c, BLUE, CYAN, edge=None)
    c.stroke(rise(y0=0.90, y1=0.34), 0.105, (WHITE, 0.45))
    c.stroke(rise(y0=0.76, y1=0.16), 0.105, (WHITE, 1.0))


# ══ V11〜V20 補色 ══════════════════════════════════════════════════
# 反対色を正面からぶつける。どちらも彩度を落とさないのが要点。
@design("V11", "橙地・青矢")
def v11(c):
    tile(c, ORANGE, AMBER, edge=None)
    c.stroke(RISE, 0.150, (INDIGO, 1.0))
    c.polygon(head(RISE[-1], -38, 0.31), (INDIGO, 1.0))


@design("V12", "青地・橙矢")
def v12(c):
    tile(c, BLUE, INDIGO, edge=None)
    c.stroke(RISE, 0.150, (AMBER, 1.0))
    c.polygon(head(RISE[-1], -38, 0.31), (AMBER, 1.0))


@design("V13", "桃地・翠棒")
def v13(c):
    tile(c, PINK, MAGENTA, edge=None)
    for cx, top, w in bars():
        c.bar(cx, top, 0.88, w, (LIME, 1.0))


@design("V14", "翠地・桃棒")
def v14(c):
    tile(c, GREEN, TEAL, edge=None)
    for cx, top, w in bars():
        c.bar(cx, top, 0.88, w, (PINK, 1.0))


@design("V15", "黄地・紫段")
def v15(c):
    tile(c, YELLOW, LIME, edge=None)
    for i in range(4):
        x, y = 0.10 + i * 0.21, 0.80 - i * 0.19
        c.round_rect(x, y, x + 0.19, 0.90, 0.03, (VIOLET, 1.0))


@design("V16", "紫地・黄段")
def v16(c):
    tile(c, VIOLET, INDIGO, edge=None)
    for i in range(4):
        x, y = 0.10 + i * 0.21, 0.80 - i * 0.19
        c.round_rect(x, y, x + 0.19, 0.90, 0.03, (YELLOW, 1.0))


def _hockey(c, col, r=0.80, w=0.155):
    """左上を中心にした四分円。寝かせた出だしから終盤で立ち上がる。

    円弧にすると折れ線より速度の変化がなめらかに出る。中心を左上に置くのが要点で、
    右下に置くと同じ弧が「伸び悩む形」に反転してしまう。
    """
    c.ring(0.05, 0.05, r + w / 2, r - w / 2, (col, 1.0),
           a0=math.radians(8), a1=math.radians(90))
    tip = (0.05 + r * math.cos(math.radians(8)), 0.05 + r * math.sin(math.radians(8)))
    c.polygon(head(tip, -82, 0.30), (col, 1.0))


@design("V17", "赤地・青緑の弧")
def v17(c):
    tile(c, CORAL, ORANGE, edge=None)
    _hockey(c, CYAN)


@design("V18", "青緑地・赤の弧")
def v18(c):
    tile(c, TEAL, CYAN, edge=None)
    _hockey(c, CORAL)


@design("V19", "紫地・黄の三角")
def v19(c):
    tile(c, INDIGO, PLUM, edge=None)
    c.polygon([(0.08, 0.88), (0.92, 0.88), (0.92, 0.14)], (YELLOW, 1.0))


@design("V20", "黄地・紫の三角")
def v20(c):
    tile(c, YELLOW, AMBER, edge=None)
    c.polygon([(0.08, 0.88), (0.92, 0.88), (0.92, 0.14)], (VIOLET, 1.0))


# ══ V21〜V30 多色 ══════════════════════════════════════════════════
# 色数そのもので目を引く群。形は単純にしないと騒がしくなる。
@design("V21", "虹の帯・白矢")
def v21(c):
    tile(c, INK, INK, edge=None)
    bands(c, RAINBOW)
    c.stroke(RISE, 0.155, (WHITE, 1.0))
    c.polygon(head(RISE[-1], -38, 0.32), (WHITE, 1.0))


@design("V22", "虹の帯・墨矢")
def v22(c):
    tile(c, INK, INK, edge=None)
    bands(c, RAINBOW)
    c.stroke(RISE, 0.155, (INK, 1.0))
    c.polygon(head(RISE[-1], -38, 0.32), (INK, 1.0))


@design("V23", "虹の棒")
def v23(c):
    tile(c, INK, NAVY, edge=None)
    for (cx, top, w), col in zip(bars(), (VIOLET, BLUE, CYAN, LIME, YELLOW)):
        c.bar(cx, top, 0.88, w, (col, 1.0))


@design("V24", "熱の棒")
def v24(c):
    tile(c, PLUM, INK, edge=None)
    for (cx, top, w), col in zip(bars(), HOT[::-1]):
        c.bar(cx, top, 0.88, w, (col, 1.0))


@design("V25", "虹の階段")
def v25(c):
    tile(c, INK, NAVY, edge=None)
    for i, col in enumerate((VIOLET, BLUE, CYAN, GREEN, YELLOW)):
        x, y = 0.08 + i * 0.172, 0.84 - i * 0.155
        c.round_rect(x, y, x + 0.165, 0.90, 0.025, (col, 1.0))


@design("V26", "虹の丸")
def v26(c):
    tile(c, INK, PLUM, edge=None)
    for i, col in enumerate((MAGENTA, VIOLET, BLUE, CYAN, LIME)):
        c.ring(0.14 + i * 0.18, 0.82 - i * 0.16, 0.088, 0.0, (col, 1.0))


@design("V27", "虹の斜め帯")
def v27(c):
    tile(c, INK, INK, edge=None)
    for i, col in enumerate(RAINBOW):
        t = i / len(RAINBOW)
        c.polygon([(t - 0.35, 1.05), (t + 0.10 - 0.35, 1.05),
                   (t + 0.10 + 0.55, -0.05), (t + 0.55, -0.05)], (col, 1.0))


@design("V28", "虹の扇")
def v28(c):
    tile(c, INK, NAVY, edge=None)
    for i, col in enumerate(RAINBOW[:8]):
        c.ring(0.06, 0.96, 1.20, 0.0, (col, 1.0),
               a0=math.radians(270 + i * 10.5), a1=math.radians(270 + (i + 1) * 10.5))


@design("V29", "色相の階調")
def v29(c):
    tile(c, INK, INK, edge=None)
    n = 24
    for i in range(n):
        t = i / (n - 1)
        col = RAINBOW[min(len(RAINBOW) - 1, int(t * len(RAINBOW)))]
        x = 0.03 + t * 0.94
        c.bar(x, 0.88 - (0.10 + 0.72 * t), 0.90, 0.030, (col, 1.0))


@design("V30", "虹の矢だけ")
def v30(c):
    tile(c, INK, NAVY, edge=None)
    for i, col in enumerate((VIOLET, BLUE, CYAN, GREEN, YELLOW, ORANGE, PINK)):
        d = (6 - i) * 0.042
        c.stroke(shift(RISE, -d, d), 0.10, (col, 1.0))


# ══ V31〜V40 ネオン ════════════════════════════════════════════════
# 暗い地に発光色。夜のデスクトップでも壁紙に沈まない。
@design("V31", "ネオン矢／青緑")
def v31(c):
    tile(c, INK, NAVY, edge=None)
    glow(c, RISE, 0.100, CYAN)
    c.polygon(head(RISE[-1], -38, 0.26), (CYAN, 1.0))


@design("V32", "ネオン矢／桃")
def v32(c):
    tile(c, INK, PLUM, edge=None)
    glow(c, RISE, 0.100, PINK)
    c.polygon(head(RISE[-1], -38, 0.26), (PINK, 1.0))


@design("V33", "ネオン矢／翠")
def v33(c):
    tile(c, INK, (0x06, 0x1E, 0x14), edge=None)
    glow(c, RISE, 0.100, LIME)
    c.polygon(head(RISE[-1], -38, 0.26), (LIME, 1.0))


@design("V34", "ネオン環")
def v34(c):
    tile(c, INK, NAVY, edge=None)
    glow_ring(c, 0.50, 0.50, 0.34, 0.075, CYAN)
    c.stroke([(0.30, 0.66), (0.70, 0.34)], 0.075, (PINK, 1.0))
    c.polygon(head((0.74, 0.30), -38, 0.17), (PINK, 1.0))


@design("V35", "ネオン棒")
def v35(c):
    tile(c, INK, PLUM, edge=None)
    for (cx, top, w), col in zip(bars(), (INDIGO, VIOLET, MAGENTA, PINK, CORAL)):
        for k in range(3, 0, -1):
            c.bar(cx, top - k * 0.012, 0.88, w + k * 0.024, (col, 0.16))
        c.bar(cx, top, 0.88, w, (col, 1.0))


@design("V36", "ネオンの二重矢")
def v36(c):
    tile(c, INK, NAVY, edge=None)
    glow(c, rise(y0=0.92, y1=0.40), 0.075, VIOLET, layers=3)
    glow(c, rise(y0=0.74, y1=0.14), 0.075, CYAN, layers=3)


@design("V37", "走査線")
def v37(c):
    tile(c, INK, NAVY, edge=None)
    for i in range(13):
        y = 0.06 + i * 0.072
        c.stroke([(0.0, y), (1.0, y)], 0.012, (CYAN, 0.16))
    glow(c, RISE, 0.095, LIME)


@design("V38", "格子と矢")
def v38(c):
    tile(c, INK, PLUM, edge=None)
    for i in range(7):
        t = 0.07 + i * 0.145
        c.stroke([(t, 0.0), (t, 1.0)], 0.008, (MAGENTA, 0.22))
        c.stroke([(0.0, t), (1.0, t)], 0.008, (MAGENTA, 0.22))
    glow(c, RISE, 0.095, YELLOW)


@design("V39", "夜明け")
def v39(c):
    tile(c, PLUM, INK, edge=None)
    for k in range(4, 0, -1):
        c.ring(0.50, 0.94, 0.42 + k * 0.045, 0.0, (AMBER, 0.13))
    c.ring(0.50, 0.94, 0.42, 0.0, (AMBER, 1.0))
    for i, col in enumerate((CORAL, ORANGE, AMBER)):
        c.stroke([(0.0, 0.80 - i * 0.10), (1.0, 0.62 - i * 0.14)], 0.045, (col, 1.0))


@design("V40", "ネオンの段")
def v40(c):
    tile(c, INK, NAVY, edge=None)
    for i, col in enumerate((INDIGO, BLUE, CYAN, LIME)):
        x, y = 0.10 + i * 0.21, 0.80 - i * 0.19
        for k in range(3, 0, -1):
            c.round_rect(x - k * 0.010, y - k * 0.010, x + 0.19 + k * 0.010,
                         0.90, 0.03, (col, 0.15))
        c.round_rect(x, y, x + 0.19, 0.90, 0.03, (col, 1.0))


# ══ V41〜V50 ポップ ════════════════════════════════════════════════
# 面で塗り分ける。輪郭線を持たないぶん小さくしても形が残る。
@design("V41", "対角の塗り分け")
def v41(c):
    tile(c, YELLOW, YELLOW, edge=None)
    c.polygon([(-0.05, 1.05), (1.05, -0.05), (1.05, 1.05)], (VIOLET, 1.0))
    c.stroke(RISE, 0.115, (WHITE, 1.0))


@design("V42", "四分割")
def v42(c):
    tile(c, INK, INK, edge=None)
    for (x, y), col in zip(((0, 0), (0.5, 0), (0, 0.5), (0.5, 0.5)),
                           (PINK, YELLOW, CYAN, LIME)):
        c.shape((x, y, x + 0.5, y + 0.5),
                lambda px, py, x=x, y=y: x <= px < x + 0.5 and y <= py < y + 0.5,
                (col, 1.0))
    c.stroke(RISE, 0.135, (INK, 1.0))
    c.polygon(head(RISE[-1], -38, 0.28), (INK, 1.0))


@design("V43", "半分ずつ")
def v43(c):
    tile(c, INK, INK, edge=None)
    c.shape((0, 0, 1, 1), lambda x, y: True, (MAGENTA, 1.0))
    c.polygon([(-0.05, 1.05), (1.05, -0.05), (-0.05, -0.05)], (CYAN, 1.0))
    for cx, top, w in bars(n=4):
        c.bar(cx, top, 0.88, w, (WHITE, 1.0))


@design("V44", "厚みのある矢")
def v44(c):
    tile(c, AZURE, BLUE, edge=None)
    c.stroke(shift(RISE, 0.035, 0.045), 0.145, (INDIGO, 1.0))
    c.polygon(head(shift(RISE, 0.035, 0.045)[-1], -38, 0.30), (INDIGO, 1.0))
    c.stroke(RISE, 0.145, (YELLOW, 1.0))
    c.polygon(head(RISE[-1], -38, 0.30), (YELLOW, 1.0))


@design("V45", "厚みのある棒")
def v45(c):
    tile(c, LIME, GREEN, edge=None)
    for cx, top, w in bars():
        c.bar(cx + 0.020, top + 0.030, 0.90, w, (PLUM, 1.0))
        c.bar(cx, top, 0.88, w, (WHITE, 1.0))


@design("V46", "丸を重ねる")
def v46(c):
    tile(c, CREAM, (0xFF, 0xE6, 0xC0), edge=None)
    for i, col in enumerate((VIOLET, PINK, CORAL, AMBER, LIME)):
        c.ring(0.15 + i * 0.175, 0.80 - i * 0.145, 0.135, 0.0, (col, 0.88))


@design("V47", "紙を貼る")
def v47(c):
    tile(c, CREAM, (0xFF, 0xE6, 0xC0), edge=None)
    for i, col in enumerate((CYAN, LIME, YELLOW, ORANGE, PINK)):
        x = 0.07 + i * 0.175
        top = 0.80 - i * 0.145
        c.polygon([(x, top), (x + 0.155, top - 0.030),
                   (x + 0.155, 0.90), (x, 0.90)], (col, 1.0))


@design("V48", "太い山")
def v48(c):
    tile(c, YELLOW, AMBER, edge=None)
    for i, col in enumerate((CORAL, MAGENTA, VIOLET)):   # 高い山から先に描く
        p = rise(n=6, x0=-0.05, x1=1.05, y0=1.02 - (2 - i) * 0.02,
                 y1=0.22 + i * 0.20, bow=0.06)
        c.polygon(p + [(1.05, 1.05), (-0.05, 1.05)], (col, 1.0))


@design("V49", "点で描く")
def v49(c):
    tile(c, INDIGO, VIOLET, edge=None)
    for i in range(9):
        t = i / 8
        x = 0.10 + t * 0.80
        y = 0.84 - t * 0.66
        col = RAINBOW[int(t * (len(RAINBOW) - 1))]
        c.ring(x, y, 0.045 + 0.035 * t, 0.0, (col, 1.0))


@design("V50", "旗")
def v50(c):
    tile(c, CYAN, AZURE, edge=None)
    c.stroke([(0.24, 0.92), (0.24, 0.10)], 0.055, (WHITE, 1.0))
    c.polygon([(0.24, 0.12), (0.86, 0.30), (0.24, 0.50)], (PINK, 1.0))
    c.polygon([(0.24, 0.50), (0.74, 0.63), (0.24, 0.78)], (YELLOW, 1.0))


VIVID = DESIGNS
ic.ALL.extend(VIVID)


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

    if "--png" in sys.argv:
        for key, name, fn in VIVID:
            p = os.path.join(ic.OUTDIR, f"{key}.png")
            with open(p, "wb") as f:
                f.write(ic.png_bytes(ic.render(fn, 256)))
        print(f"{len(VIVID)}件を256pxで書き出しました: {ic.OUTDIR}")

    for key, name, fn in VIVID:
        print(f"  {key} {name}")
    p = os.path.join(ic.OUTDIR, "vivid.png")
    with open(p, "wb") as f:
        f.write(ic.png_bytes(ic.build_grid(VIVID, cols=10).px))
    print(f"一覧シート: {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
