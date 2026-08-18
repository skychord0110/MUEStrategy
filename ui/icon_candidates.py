# -*- coding: utf-8 -*-
"""アイコンの候補を描き出して見比べるためのスクリプト。

外部ライブラリは使わない（標準ライブラリだけでPNGを書く）。
形はスーパーサンプリング（各辺を数倍で描いてから縮小）でアンチエイリアスをかける。
make_icon.py がドット打ちだったのに対し、こちらは輪郭がなめらかになる。

実行:
    python ui/icon_candidates.py            比較シートを書き出す
    python ui/icon_candidates.py --ico A    案Aを ui/control_panel.ico として確定
出力:
    ui/icon_preview/candidates.png          比較シート
    ui/icon_preview/<案>.png                各案の256px
"""
import glob
import math
import os
import re
import shutil
import struct
import sys
import zlib

BASE = os.path.dirname(os.path.abspath(__file__))
OUTDIR = os.path.join(BASE, "icon_preview")
ICO = os.path.join(BASE, "control_panel.ico")
ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)

# コントロールパネルの配色に合わせる（ui/theme.py）
# 彩度を落とした墨・生成り・くすんだ金。差し色は一案につき一色だけ使う
INK = (0x11, 0x14, 0x19)        # 墨（青みのある黒）
INK_HI = (0x1C, 0x21, 0x29)
BONE = (0xEE, 0xEA, 0xE1)       # 生成り
BONE_D = (0xDC, 0xD6, 0xC9)
IVORY = (0xF4, 0xF1, 0xEA)
SUMI = (0x23, 0x27, 0x2D)
GOLD = (0xC8, 0xA5, 0x6A)       # くすんだ金。差し色はこれ一色
INDIGO_J = (0x2C, 0x3E, 0x5C)   # 藍
MIST = (0xA6, 0xB2, 0xC0)
WHITE = (0xFF, 0xFF, 0xFF)
SHEET_BG = (0x14, 0x14, 0x14)
SHEET_CELL = (0x1C, 0x1C, 0x1C)
SHEET_LINE = (0x33, 0x33, 0x33)
SHEET_TEXT = (0x9A, 0x9A, 0x9A)


# --------------------------------------------------------------------------
# 描画
# --------------------------------------------------------------------------
def rr_inside(x0, y0, x1, y1, r):
    """角丸長方形の内外判定を返す。塗りにも切り抜きにも使う。"""
    def inside(x, y):
        if not (x0 <= x <= x1 and y0 <= y <= y1):
            return False
        if x0 + r <= x <= x1 - r or y0 + r <= y <= y1 - r:
            return True
        cx = min(max(x, x0 + r), x1 - r)
        cy = min(max(y, y0 + r), y1 - r)
        dx, dy = x - cx, y - cy
        return dx * dx + dy * dy <= r * r
    return inside


def qbez(p0, p1, p2, n=28):
    """2次ベジェを折れ線に落とす。"""
    out = []
    for i in range(n + 1):
        t = i / n
        u = 1 - t
        out.append((u * u * p0[0] + 2 * u * t * p1[0] + t * t * p2[0],
                    u * u * p0[1] + 2 * u * t * p1[1] + t * t * p2[1]))
    return out


def xform(pts, scale, deg, tx, ty):
    """拡大・回転・平行移動。弾丸のように傾けて置く図形用。"""
    a = math.radians(deg)
    c, s = math.cos(a), math.sin(a)
    return [(tx + (x * c - y * s) * scale, ty + (x * s + y * c) * scale)
            for x, y in pts]


def swell(pts, k):
    """重心から k 倍に広げる。後光を描くのに使う。"""
    cx = sum(p[0] for p in pts) / len(pts)
    cy = sum(p[1] for p in pts) / len(pts)
    return [(cx + (x - cx) * k, cy + (y - cy) * k) for x, y in pts]


class Canvas:
    """0〜1の座標系で図形を置ける小さなキャンバス。

    実際には n*ss の解像度で描いておき、最後に ss×ss を平均して n×n にする。
    これで斜め線や円のジャギーが取れる。
    """

    def __init__(self, n, ss=4):
        self.n = n
        self.ss = ss
        self.w = n * ss
        self.buf = [0.0] * (self.w * self.w * 4)
        self.clip = None        # 図柄をタイルの角丸で切り落とすため

    def clip_round_rect(self, x0, y0, x1, y1, r):
        """以降の描画をこの角丸の内側に限る。

        これがあると図柄をタイルの縁まで走らせられる（はみ出た分は角丸で切れる）。
        小さいアイコンで効くのは、中央に小さく置いた紋章より縁まで届く形。
        """
        self.clip = rr_inside(x0, y0, x1, y1, r)

    # -- 塗り指定 ----------------------------------------------------------
    def _paint(self, paint):
        """塗りを (x, y) -> (r, g, b, a) の関数にする。

        受け付ける形は3つ。
          (色, 不透明度)                          単色
          (c0, c1, p0, p1)                        線形グラデーション
          ("r", c0, c1, 中心, 半径)               放射グラデーション
        c0/c1 はいずれも (色, 不透明度)。
        """
        if paint[0] == "r":                       # 放射グラデーション
            _, c0, c1, (cx, cy), rad = paint
            (r0, g0, b0), a0 = c0
            (r1, g1, b1), a1 = c1
            inv = 1.0 / (rad or 1e-9)

            def rf(x, y):
                t = math.hypot(x - cx, y - cy) * inv
                t = 0.0 if t < 0 else (1.0 if t > 1 else t)
                return (r0 + (r1 - r0) * t, g0 + (g1 - g0) * t,
                        b0 + (b1 - b0) * t, a0 + (a1 - a0) * t)
            return rf
        if len(paint) == 2:                       # (色, 不透明度)
            (r, g, b), a = paint
            return lambda x, y: (r, g, b, a)
        c0, c1, p0, p1 = paint                    # グラデーション
        (r0, g0, b0), a0 = c0
        (r1, g1, b1), a1 = c1
        dx, dy = p1[0] - p0[0], p1[1] - p0[1]
        d2 = dx * dx + dy * dy or 1.0

        def f(x, y):
            t = ((x - p0[0]) * dx + (y - p0[1]) * dy) / d2
            t = 0.0 if t < 0 else (1.0 if t > 1 else t)
            return (r0 + (r1 - r0) * t, g0 + (g1 - g0) * t,
                    b0 + (b1 - b0) * t, a0 + (a1 - a0) * t)
        return f

    def _blend(self, ix, iy, col, mode="over"):
        """1画素を重ねる。

        mode="multiply" は下の色と掛け合わせる（インクを重ね刷りしたときの見え方）。
        "screen" は光を足す。どちらも下地が不透明であることを前提にしている
        （このファイルではタイルを最初に敷くので常に成り立つ）。
        """
        r, g, b, a = col
        if a <= 0:
            return
        i = (iy * self.w + ix) * 4
        buf = self.buf
        if mode != "over" and buf[i + 3] > 0.999:
            dr, dg, db = buf[i], buf[i + 1], buf[i + 2]
            if mode == "multiply":
                r, g, b = r * dr / 255.0, g * dg / 255.0, b * db / 255.0
            else:                                   # screen
                r = 255.0 - (255.0 - r) * (255.0 - dr) / 255.0
                g = 255.0 - (255.0 - g) * (255.0 - dg) / 255.0
                b = 255.0 - (255.0 - b) * (255.0 - db) / 255.0
        ia = 1.0 - a
        buf[i] = r * a + buf[i] * ia
        buf[i + 1] = g * a + buf[i + 1] * ia
        buf[i + 2] = b * a + buf[i + 2] * ia
        buf[i + 3] = a + buf[i + 3] * ia

    def shape(self, box, inside, paint, mode="over"):
        """任意の内外判定で塗る。メタボールのような数式で決まる形に使う。"""
        self._scan(box, inside, paint, mode)

    def _scan(self, box, inside, paint, mode="over"):
        """bboxの範囲だけ走査して inside(x, y) が真の画素を塗る。"""
        f = self._paint(paint)
        clip = self.clip
        w = self.w
        x0 = max(0, int(box[0] * w) - 1)
        y0 = max(0, int(box[1] * w) - 1)
        x1 = min(w, int(box[2] * w) + 2)
        y1 = min(w, int(box[3] * w) + 2)
        inv = 1.0 / w
        for iy in range(y0, y1):
            y = (iy + 0.5) * inv
            for ix in range(x0, x1):
                x = (ix + 0.5) * inv
                if inside(x, y) and (clip is None or clip(x, y)):
                    self._blend(ix, iy, f(x, y), mode)

    # -- 図形 --------------------------------------------------------------
    def round_rect(self, x0, y0, x1, y1, r, paint, mode="over"):
        self._scan((x0, y0, x1, y1), rr_inside(x0, y0, x1, y1, r), paint, mode)

    def taper(self, p0, p1, p2, w0, w1, paint, n=28, mode="over"):
        """先細りする曲線。角や刃のように太さが変わる形に使う。"""
        pts = qbez(p0, p1, p2, n)
        left, right = [], []
        for i, (x, y) in enumerate(pts):
            if i == 0:
                dx, dy = pts[1][0] - x, pts[1][1] - y
            elif i == n:
                dx, dy = x - pts[-2][0], y - pts[-2][1]
            else:
                dx = pts[i + 1][0] - pts[i - 1][0]
                dy = pts[i + 1][1] - pts[i - 1][1]
            L = math.hypot(dx, dy) or 1e-9
            h = (w0 + (w1 - w0) * (i / n)) / 2.0
            nx, ny = -dy / L * h, dx / L * h
            left.append((x + nx, y + ny))
            right.append((x - nx, y - ny))
        self.polygon(left + right[::-1], paint, mode)

    def ring(self, cx, cy, r_out, r_in, paint, a0=None, a1=None, mode="over"):
        """円環。a0/a1（ラジアン、右向き0・時計回り）を渡すと円弧になる。"""
        def inside(x, y):
            dx, dy = x - cx, y - cy
            d2 = dx * dx + dy * dy
            if not (r_in * r_in <= d2 <= r_out * r_out):
                return False
            if a0 is None:
                return True
            ang = math.atan2(dy, dx) % (2 * math.pi)
            lo = a0 % (2 * math.pi)
            hi = a1 % (2 * math.pi)
            return lo <= ang <= hi if lo <= hi else (ang >= lo or ang <= hi)
        self._scan((cx - r_out, cy - r_out, cx + r_out, cy + r_out), inside, paint, mode)

    def stroke(self, pts, width, paint, cap="round", mode="over"):
        """折れ線。線分への最短距離で判定するので継ぎ目が自然に丸くつながる。"""
        h = width / 2.0
        segs = []
        for (ax, ay), (bx, by) in zip(pts, pts[1:]):
            dx, dy = bx - ax, by - ay
            segs.append((ax, ay, dx, dy, dx * dx + dy * dy or 1e-9))

        def inside(x, y):
            for ax, ay, dx, dy, dd in segs:
                t = ((x - ax) * dx + (y - ay) * dy) / dd
                if cap == "butt":
                    if t < 0 or t > 1:
                        continue
                else:
                    t = 0.0 if t < 0 else (1.0 if t > 1 else t)
                px, py = x - (ax + dx * t), y - (ay + dy * t)
                if px * px + py * py <= h * h:
                    return True
            return False
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        self._scan((min(xs) - h, min(ys) - h, max(xs) + h, max(ys) + h), inside, paint, mode)

    def polygon(self, pts, paint, mode="over"):
        def inside(x, y):
            c = False
            j = len(pts) - 1
            for i in range(len(pts)):
                xi, yi = pts[i]
                xj, yj = pts[j]
                if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
                    c = not c
                j = i
            return c
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        self._scan((min(xs), min(ys), max(xs), max(ys)), inside, paint, mode)

    def bar(self, cx, top, bot, width, paint, r=None):
        """角丸の縦棒（ローソク足の実体・棒グラフ用）。"""
        h = width / 2.0
        r = min(h, (bot - top) / 2.0) if r is None else r
        self.round_rect(cx - h, top, cx + h, bot, r, paint)

    # -- 取り出し ----------------------------------------------------------
    def resolve(self):
        """ss×ss を平均して n×n の 0〜255 RGBA にする。

        バッファは色を不透明度で掛けた状態（乗算済み）で持っている。
        重ね合わせの計算はそれが都合よいが、PNGは掛ける前の色を書くので割り戻す。
        不透明度はバッファ上は0〜1なので、ここで0〜255に直す。
        """
        n, ss, w, buf = self.n, self.ss, self.w, self.buf
        inv = 1.0 / (ss * ss)
        out = []
        for y in range(n):
            row = []
            for x in range(n):
                r = g = b = a = 0.0
                for sy in range(y * ss, y * ss + ss):
                    base = (sy * w + x * ss) * 4
                    for k in range(ss):
                        i = base + k * 4
                        r += buf[i]
                        g += buf[i + 1]
                        b += buf[i + 2]
                        a += buf[i + 3]
                if a <= 0.0:
                    row.append((0, 0, 0, 0))
                    continue
                s = 1.0 / a                      # 乗算済みを割り戻す
                row.append((min(255, int(r * s + 0.5)), min(255, int(g * s + 0.5)),
                            min(255, int(b * s + 0.5)),
                            min(255, int(a * inv * 255.0 + 0.5))))
            out.append(row)
        return out


def tile(c, top=INK_HI, bot=INK, r=0.215, edge=(WHITE, 0.085), ew=0.008):
    """共通の下地。

    地の明暗差はごくわずかに留める（斜めの光が当たっている程度）。
    縁は髪の毛ほどの罫線を一本。太い縁取りや彩度の高いグラデーションは、
    小さくしたときに図柄より先に目に入ってしまい、紋が負ける。
    """
    grad = ((top, 1.0), (bot, 1.0), (0.06, 0.0), (0.80, 1.0))
    c.round_rect(0.0, 0.0, 1.0, 1.0, r, grad)
    if edge:
        c.round_rect(0.0, 0.0, 1.0, 1.0, r, edge)
        c.round_rect(ew, ew, 1.0 - ew, 1.0 - ew, r - ew, grad)
    c.clip_round_rect(0.0, 0.0, 1.0, 1.0, r)


def radial(cx, cy, deg, r0, r1):
    """中心から放射方向の線分。目盛りを刻むのに使う。"""
    a = math.radians(deg)
    return [(cx + r0 * math.cos(a), cy + r0 * math.sin(a)),
            (cx + r1 * math.cos(a), cy + r1 * math.sin(a))]


# --------------------------------------------------------------------------
# 候補
# --------------------------------------------------------------------------
def design_a(c):
    """A 一線 — 折れた線を一本だけ。左右を非対称にして緊張を作る。"""
    tile(c)
    c.stroke([(0.250, 0.298), (0.482, 0.722), (0.772, 0.244)], 0.050, (IVORY, 0.93))
    c.ring(0.772, 0.244, 0.056, 0.0, (GOLD, 1.0))


def design_b(c):
    """B 円相 — 環の切れ目から線が外へ抜ける。

    線を環の端から端まで通すと禁止マーク（⊘）に見えてしまうため、
    右上の四半分だけに留める。
    """
    tile(c)
    c.ring(0.5, 0.5, 0.302, 0.260, (IVORY, 0.88),
           a0=math.radians(340), a1=math.radians(298))
    c.stroke(radial(0.5, 0.5, -41, 0.052, 0.472), 0.042, (GOLD, 1.0))


def design_c(c):
    """C 目盛 — 高さの違う細い縦線。最後の一本だけ金にする。"""
    tile(c, BONE, BONE_D, edge=(SUMI, 0.10))
    base = 0.742
    c.stroke([(0.212, base), (0.788, base)], 0.016, (SUMI, 0.30))
    for i, (x, h) in enumerate(zip((0.262, 0.380, 0.498, 0.616, 0.734),
                                   (0.60, 0.42, 0.72, 0.34, 1.00))):
        c.stroke([(x, base - 0.014), (x, base - 0.014 - h * 0.430)], 0.046,
                 (GOLD, 1.0) if i == 4 else (SUMI, 0.82))


def design_d(c):
    """D 座標 — 縁まで届く細い十字と、その交点に置いた小さな環。"""
    tile(c)
    c.stroke([(0.638, -0.04), (0.638, 1.04)], 0.026, (MIST, 0.42))
    c.stroke([(-0.04, 0.362), (1.04, 0.362)], 0.026, (MIST, 0.42))
    c.ring(0.638, 0.362, 0.104, 0.082, (IVORY, 0.86))
    c.ring(0.638, 0.362, 0.038, 0.0, (GOLD, 1.0))


def design_e(c):
    """E 地平 — 水平線の上に細い弧。差し色は日の位置を示す点だけ。"""
    tile(c, BONE, BONE_D, edge=(SUMI, 0.10))
    c.ring(0.500, 0.628, 0.258, 0.222, (INDIGO_J, 0.88),
           a0=math.radians(180), a1=math.radians(360))
    c.stroke([(0.176, 0.628), (0.824, 0.628)], 0.024, (SUMI, 0.52))
    c.ring(0.500, 0.628, 0.052, 0.0, (GOLD, 1.0))


def design_f(c):
    """F ダイヤル — 計器の目盛環。制御盤という道具そのものを表す。"""
    tile(c)
    c.ring(0.5, 0.5, 0.366, 0.348, (MIST, 0.38))
    for d in (0, 90, 180, 270):
        c.stroke(radial(0.5, 0.5, d, 0.300, 0.340), 0.020, (MIST, 0.38))
    c.ring(0.5, 0.5, 0.232, 0.196, (IVORY, 0.88))
    c.stroke(radial(0.5, 0.5, -52, 0.252, 0.336), 0.038, (GOLD, 1.0))
    c.ring(0.5, 0.5, 0.030, 0.0, (IVORY, 0.70))


DESIGNS = [
    ("A", "一線", design_a),
    ("B", "円相", design_b),
    ("C", "目盛", design_c),
    ("D", "座標", design_d),
    ("E", "地平", design_e),
    ("F", "ダイヤル", design_f),
]


# --------------------------------------------------------------------------
# A（一線）と B（円相）の混合
# --------------------------------------------------------------------------
"""Aの折れ線は左腕が短く右腕が長い。この比率はチェックマークそのものなので、
環で囲むと「完了マーク（✓を丸で囲んだもの）」に読めてしまう。
以下の3案はいずれもその読みを外してある。M1は両腕を環の外まで伸ばし、
M2は環を浅い弧に留め、M3は腕の長さを揃えて左右対称にした。"""


def design_m1(c):
    """M1 突き抜ける — 折れ線が環を内から外へ貫き、環はその位置で途切れる。

    線が環と交わる角度を解いて、そこだけ環を空けてある。
    交点の位置は線の傾きで決まるので、他の角度では噛み合わない。
    """
    tile(c)
    c.ring(0.5, 0.5, 0.302, 0.262, (IVORY, 0.78),
           a0=math.radians(226), a1=math.radians(316))
    c.ring(0.5, 0.5, 0.302, 0.262, (IVORY, 0.78),
           a0=math.radians(338), a1=math.radians(204))
    c.stroke([(0.208, 0.238), (0.492, 0.700), (0.826, 0.216)], 0.046, (IVORY, 0.93))
    c.ring(0.826, 0.216, 0.052, 0.0, (GOLD, 1.0))


def design_m2(c):
    """M2 受ける — 浅い弧が折れ線を下から支える。弧を短くして笑い顔に見せない。"""
    tile(c)
    c.ring(0.500, 0.548, 0.300, 0.262, (IVORY, 0.70),
           a0=math.radians(32), a1=math.radians(148))
    c.stroke([(0.276, 0.318), (0.492, 0.652), (0.796, 0.246)], 0.046, (IVORY, 0.93))
    c.ring(0.796, 0.246, 0.052, 0.0, (GOLD, 1.0))


def design_m3(c):
    """M3 収める — 閉じた環に左右対称のVを内接させる。印章のように静か。

    対称にすることで✓ではなくVとして読ませ、崩すのは金の点ひとつだけにする。
    """
    tile(c)
    c.ring(0.5, 0.5, 0.318, 0.288, (IVORY, 0.68))
    c.stroke([(0.322, 0.386), (0.500, 0.686), (0.678, 0.386)], 0.046, (IVORY, 0.93))
    c.ring(0.678, 0.386, 0.048, 0.0, (GOLD, 1.0))


MIXES = [
    ("M1", "抜ける", design_m1),
    ("M2", "受ける", design_m2),
    ("M3", "収める", design_m3),
]


# --------------------------------------------------------------------------
# B（円相）の色違い。形は完全に同じで配色だけを変える
# --------------------------------------------------------------------------
SILVER = (0xC2, 0xCC, 0xD6)     # 銀鼠
SEIJI = (0x8F, 0xB6, 0xA6)      # 青磁
SHAKUDO = (0xB4, 0x6E, 0x4C)    # 赤銅
AI = (0x22, 0x32, 0x4C)         # 藍
AI_D = (0x12, 0x1C, 0x2E)


def enso(top, bot, edge, ring, line, ring_a=0.88):
    """円相を配色だけ差し替えて作る。形・寸法は design_b と完全に同じ。"""
    def draw(c):
        tile(c, top, bot, edge=edge)
        c.ring(0.5, 0.5, 0.302, 0.260, (ring, ring_a),
               a0=math.radians(340), a1=math.radians(298))
        c.stroke(radial(0.5, 0.5, -41, 0.052, 0.472), 0.042, (line, 1.0))
    return draw


B_COLORS = [
    ("B1", "金／墨", enso(INK_HI, INK, (WHITE, 0.085), IVORY, GOLD)),
    ("B2", "銀鼠／墨", enso(INK_HI, INK, (WHITE, 0.085), IVORY, SILVER)),
    ("B3", "金／藍", enso(AI, AI_D, (WHITE, 0.10), IVORY, GOLD)),
    ("B4", "藍／生成り", enso(BONE, BONE_D, (SUMI, 0.10), SUMI, INDIGO_J, 0.80)),
    ("B5", "青磁／墨", enso(INK_HI, INK, (WHITE, 0.085), IVORY, SEIJI)),
    ("B6", "赤銅／墨", enso(INK_HI, INK, (WHITE, 0.085), IVORY, SHAKUDO)),
]

# --------------------------------------------------------------------------
# 明るく目立たせる案。地を彩度の高い色で埋め、印刷のズレ・網点・迷彩など
# 「アイコンらしくない手法」を持ち込んで、他と並んだときに埋もれないようにする。
# --------------------------------------------------------------------------
PAPER = (0xF4, 0xF0, 0xE6)
INKBLACK = (0x0B, 0x0B, 0x0D)
RISO_CYAN = (0x00, 0xA5, 0xE0)
RISO_PINK = (0xFF, 0x48, 0x8B)
FLUO_PINK = (0xFF, 0x2D, 0x7A)
FLUO_YELLOW = (0xFF, 0xE0, 0x00)
FLUO_ORANGE = (0xFF, 0x5A, 0x1E)
LIME = (0xCB, 0xF5, 0x0A)
DEEP_GREEN = (0x14, 0x46, 0x28)
VERMILION = (0xE0, 0x38, 0x2C)   # 朱
GUNJO = (0x1E, 0x3E, 0xA8)       # 群青
KIN = (0xF0, 0xC2, 0x4E)         # 金
TURQ = (0x12, 0xD6, 0xC4)
MAGENTA = (0xE8, 0x27, 0xC0)

# 全案で共通の折れ線（Aの紋）。同じ形を違う手法で見せて差を分かりやすくする
VMARK = [(0.352, 0.406), (0.494, 0.646), (0.662, 0.360)]


def _enso_arc(c, cx, cy, col, ro=0.302, ri=0.258, mode="over", alpha=1.0):
    c.ring(cx, cy, ro, ri, (col, alpha),
           a0=math.radians(340), a1=math.radians(298), mode=mode)


def design_n1(c):
    """N1 リソグラフ — 2色を版ズレさせて刷る。重なりだけ濃くなる。"""
    tile(c, PAPER, (0xE4, 0xDE, 0xCE), edge=None)
    _enso_arc(c, 0.482, 0.482, RISO_CYAN, mode="multiply", alpha=0.94)
    _enso_arc(c, 0.520, 0.522, RISO_PINK, mode="multiply", alpha=0.94)
    c.stroke(radial(0.482, 0.482, -41, 0.05, 0.47), 0.044,
             (RISO_CYAN, 0.94), mode="multiply")
    c.stroke(radial(0.520, 0.522, -41, 0.05, 0.47), 0.044,
             (RISO_PINK, 0.94), mode="multiply")


def design_n2(c):
    """N2 ダズル — 蛍光色と黒の斜め縞。船舶の迷彩から。視線を掴んで離さない。"""
    tile(c, FLUO_YELLOW, (0xFF, 0xC2, 0x00), edge=None)
    for i in range(-4, 13):
        x = i * 0.168
        c.polygon([(x, -0.08), (x + 0.084, -0.08),
                   (x + 0.084 - 0.62, 1.08), (x - 0.62, 1.08)], (INKBLACK, 1.0))
    c.ring(0.5, 0.5, 0.322, 0.0, (FLUO_YELLOW, 1.0))
    c.stroke(VMARK, 0.064, (INKBLACK, 1.0))


def design_n3(c):
    """N3 熱分布 — サーモグラフィの配色。中心に向かって黒→紫→赤→黄。"""
    tile(c, (0x16, 0x06, 0x2E), (0x05, 0x02, 0x12), edge=None)
    for rad, col in ((0.98, (0x3E, 0x10, 0x92)), (0.74, (0xC6, 0x18, 0x6E)),
                     (0.54, (0xFF, 0x4E, 0x12)), (0.36, (0xFF, 0xB4, 0x1E)),
                     (0.19, (0xFF, 0xF4, 0x72))):
        c.ring(0.44, 0.58, rad, 0.0,
               ("r", (col, 1.0), (col, 0.0), (0.44, 0.58), rad))
    _enso_arc(c, 0.5, 0.5, (0x0A, 0x03, 0x12), alpha=0.90)
    c.stroke(radial(0.5, 0.5, -41, 0.05, 0.47), 0.042, ((0x0A, 0x03, 0x12), 0.90))


def design_n4(c):
    """N4 オーロラ — 色の異なる光をscreenで重ねる。混ざった場所が明るくなる。"""
    tile(c, (0x52, 0x1E, 0xA8), (0x1A, 0x0C, 0x4A), edge=None)
    for cx, cy, rad, col in ((0.18, 0.20, 0.66, (0x00, 0xE8, 0xD2)),
                             (0.86, 0.16, 0.60, (0xFF, 0x3D, 0x9E)),
                             (0.74, 0.90, 0.68, (0x2E, 0x8B, 0xFF)),
                             (0.30, 0.92, 0.52, (0xC8, 0xFF, 0x3D))):
        c.ring(cx, cy, rad, 0.0,
               ("r", (col, 0.88), (col, 0.0), (cx, cy), rad), mode="screen")
    c.stroke(VMARK, 0.058, (WHITE, 0.96))
    c.ring(VMARK[2][0], VMARK[2][1], 0.052, 0.0, (WHITE, 1.0))


def design_n5(c):
    """N5 網点 — 印刷の網点を拡大して図柄にする。ポップアートの手つき。"""
    tile(c, FLUO_ORANGE, (0xE8, 0x3A, 0x0A), edge=None)
    n = 9
    for iy in range(n):
        for ix in range(n):
            x, y = (ix + 0.5) / n, (iy + 0.5) / n
            c.ring(x, y, 0.010 + 0.052 * (x * 0.55 + y * 0.45), 0.0,
                   ((0x20, 0x0A, 0x02), 1.0))
    c.ring(0.5, 0.5, 0.336, 0.0, (PAPER, 1.0))
    c.stroke(VMARK, 0.064, (FLUO_ORANGE, 1.0))


def design_n6(c):
    """N6 市松 — 朱と群青の市松に金の円相。和の極彩色をそのまま持ち込む。"""
    tile(c, VERMILION, VERMILION, edge=None)
    for iy in range(4):
        for ix in range(4):
            if (ix + iy) % 2:
                c.round_rect(ix / 4, iy / 4, (ix + 1) / 4, (iy + 1) / 4, 0.0,
                             (GUNJO, 1.0))
    _enso_arc(c, 0.5, 0.5, KIN, ro=0.318, ri=0.262)
    c.stroke(radial(0.5, 0.5, -41, 0.055, 0.482), 0.048, (KIN, 1.0))


def design_n7(c):
    """N7 色ズレ — CMYの3版がズレたまま刷られた状態。重なりで黒に沈む。"""
    tile(c, (0xFB, 0xFB, 0xFD), (0xEA, 0xEA, 0xF2), edge=None)
    for dx, dy, col in ((-0.032, 0.014, (0x00, 0xC8, 0xE8)),
                        (0.032, -0.012, (0xFF, 0x1F, 0x9C)),
                        (0.004, 0.032, (0xFF, 0xE3, 0x00))):
        c.stroke([(x + dx, y + dy) for x, y in VMARK], 0.092, (col, 1.0),
                 mode="multiply")


def design_n8(c):
    """N8 等高線 — 中心を少しずつずらした環。地図の等高線のような密度差が出る。"""
    tile(c, LIME, (0xA6, 0xD6, 0x00), edge=None)
    for i, rr in enumerate((0.470, 0.402, 0.334, 0.266, 0.198, 0.130)):
        c.ring(0.50 + 0.030 * i, 0.52 - 0.024 * i, rr, rr - 0.028,
               (DEEP_GREEN, 1.0))
    c.ring(0.68, 0.376, 0.055, 0.0, (VERMILION, 1.0))


def design_n9(c):
    """N9 シール — 白フチと影を付けて、貼り付けた切り抜きシールに見せる。

    タイルを敷かずに自分で組む唯一の案。輪郭が二重になるぶん、
    暗いデスクトップでも輪郭が消えない。
    """
    c.round_rect(0.055, 0.085, 0.982, 0.982, 0.210, (INKBLACK, 0.30))
    c.round_rect(0.018, 0.018, 0.945, 0.945, 0.210, (WHITE, 1.0))
    c.round_rect(0.078, 0.078, 0.885, 0.885, 0.155, (FLUO_PINK, 1.0))
    c.clip_round_rect(0.078, 0.078, 0.885, 0.885, 0.155)
    mark = [(x * 0.80 + 0.10, y * 0.80 + 0.09) for x, y in VMARK]
    c.stroke([(x + 0.016, y + 0.020) for x, y in mark], 0.070, (INKBLACK, 0.35))
    c.stroke(mark, 0.070, (WHITE, 1.0))


def design_n10(c):
    """N10 メタボール — 3つの球が近づくと融合する。数式で決まる有機的な形。"""
    tile(c, (0xF2, 0xF6, 0xFF), (0xDC, 0xE6, 0xF8), edge=None)
    blobs = ((0.360, 0.560, 0.262), (0.620, 0.395, 0.238), (0.705, 0.720, 0.186))

    def field(x, y):
        s = 0.0
        for bx, by, br in blobs:
            s += br * br / ((x - bx) ** 2 + (y - by) ** 2 + 1e-6)
        return s >= 1.0

    c.shape((0.02, 0.02, 0.99, 0.99), field,
            ((TURQ, 1.0), (MAGENTA, 1.0), (0.22, 0.74), (0.82, 0.30)))
    c.ring(0.705, 0.720, 0.062, 0.0, (WHITE, 1.0))


def _riso(w=0.044, scale=1.00, off=0.0195, key=None):
    """N1（リソグラフ）を寸法だけ変えて作る。図柄・配色・角度は共通。

    小さくしたとき薄れる原因は2つある。線が細いことと、版ズレ量が
    1px未満になって2色が混ざるだけになること。w と off を別々に
    動かせるようにして、どちらが効くか見比べられるようにしてある。
    key を渡すと墨版（3枚目）を中央に敷く。
    """
    mid, span, r0 = 0.280 * scale, 0.470 * scale, 0.050 * scale

    def draw(c):
        tile(c, PAPER, (0xE4, 0xDE, 0xCE), edge=None)
        plates = []
        if key:
            plates.append((key, 0.0))            # 墨版は中央
        plates += [(RISO_CYAN, -off), (RISO_PINK, off)]
        for col, d in plates:
            cx, cy = 0.5 + d, 0.5 + d * 1.02
            c.ring(cx, cy, mid + w / 2, mid - w / 2, (col, 0.94),
                   a0=math.radians(340), a1=math.radians(298), mode="multiply")
            c.stroke(radial(cx, cy, -41, r0, span), w, (col, 0.94),
                     mode="multiply")
    return draw


# N1の調整版。太さ・大きさ・版数と、効かせる要素を1案ずつ変えている
N1_VARIANTS = [
    ("N1", "元のまま", design_n1),
    ("N1A", "線を太く", _riso(w=0.055)),
    ("N1B", "太く＋紋を拡大", _riso(w=0.060, scale=1.12, off=0.022)),
    ("N1C", "墨版を追加", _riso(w=0.055, scale=1.06, off=0.021,
                                key=(0x2E, 0x33, 0x42))),
]


NEW = [
    ("N1", "リソグラフ", design_n1),
    ("N2", "ダズル", design_n2),
    ("N3", "熱分布", design_n3),
    ("N4", "オーロラ", design_n4),
    ("N5", "網点", design_n5),
    ("N6", "市松", design_n6),
    ("N7", "色ズレ", design_n7),
    ("N8", "等高線", design_n8),
    ("N9", "シール", design_n9),
    ("N10", "メタボール", design_n10),
]



def render(fn, n):
    c = Canvas(n, ss=4 if n <= 96 else 3)
    fn(c)
    return c.resolve()


# --------------------------------------------------------------------------
# 出力
# --------------------------------------------------------------------------
def png_bytes(px):
    h, w = len(px), len(px[0])
    raw = bytearray()
    for row in px:
        raw.append(0)
        for r, g, b, a in row:
            raw += bytes((r, g, b, a))

    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + chunk(b"IEND", b""))


def dib_bytes(px):
    """ICOに入れる非圧縮のDIB（BMP）を作る。

    ICOの各サイズはPNGでもDIBでもよいことになっているが、Windowsが
    PNGを確実に解釈するのは256pxだけで、それ未満をPNGで入れると
    デコードされずに化ける（実測: 32pxがノイズになった）。
    そのため256px以外はこちらの形式で書く。

    DIB特有の作法が3つある。
      - 高さはANDマスクぶんを含めて2倍で申告する
      - 画素は下の行から並べる
      - 色順はBGRA
    """
    h, w = len(px), len(px[0])
    header = struct.pack("<IiiHHIIiiII", 40, w, h * 2, 1, 32, 0, w * h * 4,
                         0, 0, 0, 0)
    xor = bytearray()
    for row in reversed(px):
        for r, g, b, a in row:
            xor += bytes((b, g, r, a))
    # 32bit画像では透過をアルファで表すのでANDマスクは全0。ただし省略はできず、
    # 1bppの各行を4バイト境界に揃えた大きさが必要
    and_mask = bytes(((w + 31) // 32) * 4 * h)
    return header + bytes(xor) + and_mask


def write_ico(fn, path=ICO):
    images = []
    for s in ICO_SIZES:
        px = render(fn, s)
        images.append((s, png_bytes(px) if s >= 256 else dib_bytes(px)))
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = len(header) + 16 * len(images)
    entries, blobs = b"", b""
    for size, data in images:
        w = h = 0 if size >= 256 else size
        entries += struct.pack("<BBBBHHII", w, h, 0, 0, 1, 32, len(data), offset)
        blobs += data
        offset += len(data)
    with open(path, "wb") as f:
        f.write(header + entries + blobs)
    return path


FONT = {
    "A": ("01110", "10001", "10001", "11111", "10001", "10001", "10001"),
    "B": ("11110", "10001", "10001", "11110", "10001", "10001", "11110"),
    "C": ("01110", "10001", "10000", "10000", "10000", "10001", "01110"),
    "D": ("11110", "10001", "10001", "10001", "10001", "10001", "11110"),
    "E": ("11111", "10000", "10000", "11110", "10000", "10000", "11111"),
    "F": ("11111", "10000", "10000", "11110", "10000", "10000", "10000"),
    "M": ("10001", "11011", "10101", "10101", "10001", "10001", "10001"),
    "N": ("10001", "11001", "10101", "10101", "10011", "10001", "10001"),
    "O": ("01110", "10001", "10001", "10001", "10001", "10001", "01110"),
    "H": ("10001", "10001", "10001", "11111", "10001", "10001", "10001"),
    "R": ("11110", "10001", "10001", "11110", "10100", "10010", "10001"),
    "0": ("01110", "10001", "10011", "10101", "11001", "10001", "01110"),
    "1": ("00100", "01100", "00100", "00100", "00100", "00100", "01110"),
    "2": ("01110", "10001", "00001", "00010", "00100", "01000", "11111"),
    "3": ("11111", "00010", "00100", "00010", "00001", "10001", "01110"),
    "4": ("00010", "00110", "01010", "10010", "11111", "00010", "00010"),
    "5": ("11111", "10000", "11110", "00001", "00001", "10001", "01110"),
    "6": ("00110", "01000", "10000", "11110", "10001", "10001", "01110"),
    "7": ("11111", "00001", "00010", "00100", "01000", "01000", "01000"),
    "8": ("01110", "10001", "10001", "01110", "10001", "10001", "01110"),
    "9": ("01110", "10001", "10001", "01111", "00001", "00010", "01100"),
}


class Sheet:
    """比較シート用の素朴なRGBキャンバス。"""

    def __init__(self, w, h, bg):
        self.w, self.h = w, h
        self.px = [[bg + (255,) for _ in range(w)] for _ in range(h)]

    def rect(self, x0, y0, x1, y1, col):
        for y in range(max(0, y0), min(self.h, y1)):
            for x in range(max(0, x0), min(self.w, x1)):
                self.px[y][x] = col + (255,)

    def paste(self, img, x0, y0, scale=1):
        """RGBAを合成する。scale>1 は最近傍拡大（実際のドットを見せるため）。"""
        for y, row in enumerate(img):
            for x, (r, g, b, a) in enumerate(row):
                if a == 0:
                    continue
                for sy in range(scale):
                    yy = y0 + y * scale + sy
                    if not 0 <= yy < self.h:
                        continue
                    for sx in range(scale):
                        xx = x0 + x * scale + sx
                        if not 0 <= xx < self.w:
                            continue
                        br, bg_, bb, _ = self.px[yy][xx]
                        f = a / 255.0
                        self.px[yy][xx] = (int(r * f + br * (1 - f)),
                                           int(g * f + bg_ * (1 - f)),
                                           int(b * f + bb * (1 - f)), 255)

    def text(self, s, x0, y0, col, scale=2):
        for ch in s:
            rows = FONT.get(ch.upper())
            if rows:
                for y, row in enumerate(rows):
                    for x, v in enumerate(row):
                        if v == "1":
                            self.rect(x0 + x * scale, y0 + y * scale,
                                      x0 + (x + 1) * scale, y0 + (y + 1) * scale, col)
            x0 += 6 * scale


def build_sheet(items, cols=3, big=176, smalls=None):
    m, gap = 20, 16
    smalls = smalls or ((48, 2), (32, 3), (16, 6))
    stack = sum(s * mg + 12 for s, mg in smalls) - 12
    cw = big + 190
    chh = max(big, stack) + 46
    rows = -(-len(items) // cols)
    w = m * 2 + cols * cw + (cols - 1) * gap
    h = m * 2 + rows * chh + (rows - 1) * gap
    sh = Sheet(w, h, SHEET_BG)

    for i, (key, name, fn) in enumerate(items):
        cx = m + (i % cols) * (cw + gap)
        cy = m + (i // cols) * (chh + gap)
        sh.rect(cx, cy, cx + cw, cy + chh, SHEET_CELL)
        sh.rect(cx, cy, cx + cw, cy + 1, SHEET_LINE)

        sh.paste(render(fn, big), cx + 20, cy + 28)
        sh.text(key, cx + 20, cy + 8, (0xEA, 0xEA, 0xEA), 2)

        # 右側は実寸と、そのドットを見せるための拡大
        x = cx + 20 + big + 22
        y = cy + 28
        for size, mag in smalls:
            img = render(fn, size)
            sh.paste(img, x, y, mag)
            sh.text(str(size), x + size * mag + 8, y + size * mag // 2 - 7,
                    SHEET_TEXT, 2)
            y += size * mag + 12
    return sh


# --------------------------------------------------------------------------
# 橙と紺の2色だけで組む15案。
# 色を固定するぶん、差は「形の作り方」で付ける。既存案と手法が重ならないよう、
# 1案につき生成の仕組みを1つずつ変えてある。
# --------------------------------------------------------------------------
ORANGE = (0xFF, 0x6A, 0x14)
ORANGE_HI = (0xFF, 0xA2, 0x4C)
ORANGE_DEEP = (0xC7, 0x40, 0x00)
NAVY = (0x0E, 0x1D, 0x42)
NAVY_D = (0x07, 0x11, 0x2C)
NAVY_HI = (0x24, 0x40, 0x7E)
CREAM = (0xFF, 0xF0, 0xDC)

# 15案で共通に使う折れ線。縁近くまで届く大きめの寸法にしてある
VBIG = [(0.185, 0.300), (0.500, 0.780), (0.815, 0.240)]


def dist_to_polyline(pts, x, y):
    """折れ線までの最短距離。縞のズレやディザの濃度を決めるのに使う。"""
    best = 1e9
    for (ax, ay), (bx, by) in zip(pts, pts[1:]):
        dx, dy = bx - ax, by - ay
        dd = dx * dx + dy * dy or 1e-9
        t = ((x - ax) * dx + (y - ay) * dy) / dd
        t = 0.0 if t < 0 else (1.0 if t > 1 else t)
        best = min(best, math.hypot(x - (ax + dx * t), y - (ay + dy * t)))
    return best


def near_polyline(pts, w):
    h = w / 2.0
    return lambda x, y: dist_to_polyline(pts, x, y) <= h


def _long_shadow(c, pts, width, col, reach=0.98, step=0.040):
    """右下へ伸びる長い影。同じ形をずらして重ね、隙間なく繋げる。"""
    n = int(reach / step)
    for i in range(n, 0, -1):
        d = i * step
        c.stroke([(x + d, y + d) for x, y in pts], width, (col, 1.0))


def design_o1(c):
    """O1 縞ズレ — 縞の位相が紋の内側だけ半分ずれる。形は線ではなく境目に出る。"""
    tile(c, NAVY, NAVY_D, edge=None)
    inside = near_polyline(VBIG, 0.235)
    per = 0.086

    def stripe(x, y):
        yy = y + (per * 0.5 if inside(x, y) else 0.0)
        return (yy % per) < per * 0.5
    c.shape((0.0, 0.0, 1.0, 1.0), stripe, (ORANGE, 1.0))


def design_o2(c):
    """O2 モアレ — 中心をずらした放射線と同心円を重ね、干渉縞を起こす。"""
    tile(c, ORANGE, ORANGE_DEEP, edge=None)
    for i in range(44):
        c.stroke(radial(0.40, 0.44, i * 180 / 44, -1.3, 1.3), 0.013, (NAVY, 0.85))
    for k in range(1, 19):
        r = k * 0.055
        c.ring(0.60, 0.56, r + 0.0085, r - 0.0085, (NAVY, 0.50))


def design_o3(c):
    """O3 長い影 — 紋から右下の縁まで影を伸ばす。奥行きが出て面積も稼げる。"""
    tile(c, ORANGE, ORANGE_HI, edge=None)
    _long_shadow(c, VBIG, 0.165, ORANGE_DEEP)
    c.stroke(VBIG, 0.165, (NAVY, 1.0))


def design_o4(c):
    """O4 押し出し — 紋を厚みのある立体として起こす。影ではなく側面。"""
    tile(c, NAVY, NAVY_D, edge=None)
    for i in range(16, 0, -1):
        d = i * 0.011
        c.stroke([(x + d, y + d * 0.62) for x, y in VBIG], 0.150,
                 (ORANGE_DEEP, 1.0))
    c.stroke(VBIG, 0.150, (ORANGE, 1.0))


BAYER4 = ((0, 8, 2, 10), (12, 4, 14, 6), (3, 11, 1, 9), (15, 7, 13, 5))


def design_o5(c):
    """O5 ディザ — 紋に近いほど点を密にする。網掛けだけで形を出す。"""
    tile(c, ORANGE, ORANGE, edge=None)
    n = 22
    for iy in range(n):
        for ix in range(n):
            x, y = (ix + 0.5) / n, (iy + 0.5) / n
            t = 1.0 - min(1.0, dist_to_polyline(VBIG, x, y) * 3.4)
            if t * 16.0 > BAYER4[iy % 4][ix % 4]:
                c.round_rect(ix / n, iy / n, (ix + 1) / n, (iy + 1) / n, 0.0,
                             (NAVY, 1.0))


def design_o6(c):
    """O6 ひび割れ — 一番近い種と2番目が拮抗する線＝境界だけを塗る。"""
    tile(c, ORANGE, ORANGE_DEEP, edge=None)
    seeds = ((0.16, 0.20), (0.60, 0.10), (0.90, 0.42), (0.74, 0.84),
             (0.32, 0.92), (0.04, 0.64), (0.46, 0.50), (0.96, 0.10))

    def crack(x, y):
        ds = sorted(math.hypot(x - sx, y - sy) for sx, sy in seeds)
        return ds[1] - ds[0] < 0.030
    c.shape((0.0, 0.0, 1.0, 1.0), crack, (NAVY, 1.0))
    c.stroke(VBIG, 0.130, (NAVY, 1.0))


def design_o7(c):
    """O7 反転 — 対角で地を二分し、紋も境界をまたいで白黒逆になる。"""
    tile(c, NAVY, NAVY, edge=None)
    c.polygon([(0.0, 1.0), (1.0, 0.0), (1.0, 1.0)], (ORANGE, 1.0))
    base = c.clip
    c.clip = lambda x, y: (base is None or base(x, y)) and x + y <= 1.0
    c.stroke(VBIG, 0.175, (ORANGE, 1.0))
    c.clip = lambda x, y: (base is None or base(x, y)) and x + y > 1.0
    c.stroke(VBIG, 0.175, (NAVY, 1.0))
    c.clip = base


def design_o8(c):
    """O8 角の渦 — 中心から直角に折れながら伸びる一本の線。"""
    tile(c, NAVY, NAVY_D, edge=None)
    dirs = ((1, 0), (0, 1), (-1, 0), (0, -1))
    x = y = 0.5
    pts, step = [(x, y)], 0.105
    for i in range(1, 12):
        dx, dy = dirs[(i - 1) % 4]
        L = step * ((i + 1) // 2)
        x += dx * L
        y += dy * L
        pts.append((x, y))
    c.stroke(pts, 0.072, (ORANGE, 1.0), cap="butt")


def design_o9(c):
    """O9 アーチ — 三重の半円と土台。建築的で、横に広いぶん小さくても残る。"""
    tile(c, ORANGE, ORANGE_HI, edge=None)
    for r in (0.435, 0.295, 0.155):
        c.ring(0.5, 0.735, r + 0.038, r - 0.038, (NAVY, 1.0),
               a0=math.radians(180), a1=math.radians(360))
    c.round_rect(0.075, 0.735, 0.925, 0.800, 0.02, (NAVY, 1.0))


def design_o10(c):
    """O10 折り紙 — 帯が折れて面ごとに明度が変わる。線ではなく面で紋を作る。"""
    tile(c, NAVY, NAVY_D, edge=None)
    for pts, col in ((((0.00, 0.34), (0.35, 0.17), (0.35, 0.43), (0.00, 0.60)),
                      ORANGE),
                     (((0.35, 0.17), (0.63, 0.58), (0.63, 0.84), (0.35, 0.43)),
                      ORANGE_DEEP),
                     (((0.63, 0.58), (1.00, 0.20), (1.00, 0.46), (0.63, 0.84)),
                      ORANGE_HI)):
        c.polygon(list(pts), (col, 1.0))


def design_o11(c):
    """O11 三日月 — 円を円で欠く。要素は2つだけで、遠くからでも判別できる。"""
    tile(c, NAVY, NAVY, edge=None)
    c.ring(0.485, 0.500, 0.375, 0.0, (ORANGE, 1.0))
    c.ring(0.680, 0.395, 0.330, 0.0, (NAVY, 1.0))
    c.ring(0.255, 0.760, 0.045, 0.0, (CREAM, 1.0))


def design_o12(c):
    """O12 溝 — 等間隔の細い環。密度そのものが図柄になる。"""
    tile(c, NAVY, NAVY_D, edge=None)
    for k in range(1, 15):
        r = 0.075 + k * 0.030
        c.ring(0.5, 0.5, r + 0.0085, r - 0.0085, (ORANGE, 0.92))
    c.ring(0.5, 0.5, 0.105, 0.0, (ORANGE, 1.0))
    c.ring(0.5, 0.5, 0.032, 0.0, (NAVY, 1.0))


def design_o13(c):
    """O13 極太M — 頭文字を縁まで届く太さで置く。唯一この名前を名乗れる案。"""
    tile(c, ORANGE, ORANGE_DEEP, edge=None)
    c.stroke([(0.055, 0.955), (0.055, 0.075), (0.500, 0.605),
              (0.945, 0.075), (0.945, 0.955)], 0.205, (NAVY, 1.0))


PIXV = (
    "...........",
    ".XX.....XX.",
    ".XX.....XX.",
    "..XX...XX..",
    "..XX...XX..",
    "...XX.XX...",
    "...XX.XX...",
    "....XXX....",
    "....XXX....",
    ".....X.....",
    "...........",
)


def design_o14(c):
    """O14 ドット絵 — わざと粗い格子で紋を組む。縮小しても崩れようがない。"""
    tile(c, NAVY, NAVY_D, edge=None)
    n = len(PIXV)
    for iy, row in enumerate(PIXV):
        for ix, ch in enumerate(row):
            if ch == "X":
                c.round_rect(ix / n, iy / n, (ix + 1) / n, (iy + 1) / n, 0.0,
                             (ORANGE, 1.0))


def design_o15(c):
    """O15 四方 — 同じ楔を90度ずつ回して置く。回転対称は他の案に無い秩序。"""
    tile(c, NAVY, NAVY, edge=None)
    wedge = [(0.0, 0.0), (0.0, -0.455), (0.315, -0.315), (0.175, -0.055)]
    for i, d in enumerate((0, 90, 180, 270)):
        c.polygon(xform(wedge, 1.0, d, 0.5, 0.5),
                  ((ORANGE if i % 2 == 0 else ORANGE_HI), 1.0))
    c.ring(0.5, 0.5, 0.080, 0.0, (NAVY, 1.0))


ORANGE_NAVY = [
    ("O1", "縞ズレ", design_o1), ("O2", "モアレ", design_o2),
    ("O3", "長い影", design_o3), ("O4", "押し出し", design_o4),
    ("O5", "ディザ", design_o5), ("O6", "ひび割れ", design_o6),
    ("O7", "反転", design_o7), ("O8", "角の渦", design_o8),
    ("O9", "アーチ", design_o9), ("O10", "折り紙", design_o10),
    ("O11", "三日月", design_o11), ("O12", "溝", design_o12),
    ("O13", "極太M", design_o13), ("O14", "ドット絵", design_o14),
    ("O15", "四方", design_o15),
]

# --------------------------------------------------------------------------
# 橙×青の大量案。紺は青みを強めてある（元のO3の紺は暗すぎて沈んでいた）。
# 3つの群からなる:
#   H  O3（長い影）と N8（等高線）の掛け合わせ
#   C  代表的な手法 × 配色8種
#   F  新しい手法
# --------------------------------------------------------------------------
BLUE_DEEP = (0x16, 0x2F, 0x7E)
BLUE = (0x1E, 0x48, 0xBE)
BLUE_BR = (0x30, 0x68, 0xE8)
BLUE_SKY = (0x5A, 0x99, 0xF5)
BLUE_PALE = (0xBF, 0xD8, 0xFA)


class Pal:
    """地・紋・影・差し色をひと組にしたもの。手法と配色を分けて組み合わせる。"""

    __slots__ = ("name", "bg", "bg2", "mark", "shade", "accent")

    def __init__(self, name, bg, bg2, mark, shade, accent):
        self.name = name
        self.bg, self.bg2 = bg, bg2
        self.mark, self.shade, self.accent = mark, shade, accent


PALS = [
    Pal("橙地・青紋", ORANGE, ORANGE_HI, BLUE_DEEP, ORANGE_DEEP, CREAM),
    Pal("青地・橙紋", BLUE, BLUE_DEEP, ORANGE, (0x13, 0x30, 0x88), CREAM),
    Pal("明青地・橙紋", BLUE_BR, BLUE, ORANGE, BLUE_DEEP, CREAM),
    Pal("空色地・濃橙紋", BLUE_SKY, BLUE_BR, ORANGE_DEEP, BLUE, CREAM),
    Pal("濃橙地・空色紋", ORANGE_DEEP, ORANGE, BLUE_SKY, (0x8E, 0x2C, 0x00), CREAM),
    Pal("生成り地・青紋", CREAM, (0xF4, 0xDF, 0xC0), BLUE, ORANGE, ORANGE_DEEP),
    Pal("青地・生成り紋", BLUE, BLUE_DEEP, CREAM, ORANGE, ORANGE),
    Pal("橙地・明青紋", ORANGE_HI, ORANGE, BLUE_BR, ORANGE_DEEP, BLUE_DEEP),
]


def _sq_ring(c, x, y, r, w, paint, rad=0.11):
    """角丸の環。丸い等高線と区別するために四角い等高線で使う。"""
    outer = rr_inside(x - r, y - r, x + r, y + r, min(rad, r))
    inner = rr_inside(x - r + w, y - r + w, x + r - w, y + r - w,
                      max(0.0, min(rad, r) - w))
    c.shape((x - r, y - r, x + r, y + r),
            lambda px, py: outer(px, py) and not inner(px, py), paint)


def _contours(c, col, rings=7, gap=0.062, w=0.026, drift=(0.030, -0.024),
              square=False, cx=0.50, cy=0.52, alpha=1.0):
    """中心を少しずつずらした同心の環（N8の等高線）。ずらすと密度に偏りが出る。"""
    for i in range(rings):
        r = 0.470 - i * gap
        if r <= w:
            break
        x, y = cx + drift[0] * i, cy + drift[1] * i
        if square:
            _sq_ring(c, x, y, r, w, (col, alpha))
        else:
            c.ring(x, y, r + w / 2, r - w / 2, (col, alpha))


def _mark(c, p, w=0.165, col=None):
    c.stroke(VBIG, w, (col or p.mark, 1.0))


# ── H群: 長い影 × 等高線 ────────────────────────────────────────────
def _h(fn):
    def draw(c):
        fn(c, PALS[1])          # 青地・橙紋を基本にする
    return draw


def h1(c, p):
    """地を等高線にし、紋には実体の影を落とす。二つの手法を素直に重ねた形。"""
    tile(c, p.bg, p.bg2, edge=None)
    _contours(c, p.shade)
    _long_shadow(c, VBIG, 0.165, p.shade)
    _mark(c, p)


def h2(c, p):
    """影の側を等高線で刻む。影が縞になり、奥行きに目盛りが付く。"""
    tile(c, p.bg, p.bg2, edge=None)
    for i in range(22, 0, -1):
        d = i * 0.042
        col = p.shade if i % 2 else p.bg2
        c.stroke([(x + d, y + d) for x, y in VBIG], 0.165, (col, 1.0))
    _mark(c, p)


def h3(c, p):
    """等高線の地に、影と同じ色でベタの影。紋だけ差し色で浮かせる。"""
    tile(c, p.bg, p.bg2, edge=None)
    _contours(c, p.shade, rings=9, gap=0.050, w=0.020)
    _long_shadow(c, VBIG, 0.165, p.bg2)
    _mark(c, p, col=p.accent)


def h4(c, p):
    """等高線の中心を紋の終端に置く。紋から波紋が広がって見える。"""
    tile(c, p.bg, p.bg2, edge=None)
    _contours(c, p.shade, rings=9, gap=0.052, w=0.022,
              drift=(0.0, 0.0), cx=VBIG[2][0], cy=VBIG[2][1])
    _long_shadow(c, VBIG, 0.165, p.shade)
    _mark(c, p)


def h5(c, p):
    """等高線を角丸の四角にする。丸い等高線より画面の形に馴染む。"""
    tile(c, p.bg, p.bg2, edge=None)
    _contours(c, p.shade, rings=7, gap=0.066, w=0.028, square=True)
    _long_shadow(c, VBIG, 0.165, p.shade)
    _mark(c, p)


def h6(c, p):
    """等高線を影の内側だけ濃くする。影が地の模様を染めているように見える。"""
    tile(c, p.bg, p.bg2, edge=None)
    _contours(c, p.shade, alpha=0.35)
    base = c.clip
    c.clip = lambda x, y: (base is None or base(x, y)) and x + y > 0.98
    _contours(c, p.shade)
    c.clip = base
    _mark(c, p)


def h7(c, p):
    """紋を置かず、等高線と影だけで構成する。図と地の区別を捨てた案。"""
    tile(c, p.bg, p.bg2, edge=None)
    _long_shadow(c, VBIG, 0.185, p.shade)
    _contours(c, p.mark, rings=8, gap=0.056, w=0.024)


def h8(c, p):
    """等高線を3色で刷り分ける。1本ごとに色が変わる。"""
    tile(c, p.bg, p.bg2, edge=None)
    cols = (p.shade, p.mark, p.accent)
    for i in range(9):
        r = 0.470 - i * 0.050
        if r <= 0.02:
            break
        c.ring(0.50 + 0.028 * i, 0.52 - 0.022 * i, r + 0.011, r - 0.011,
               (cols[i % 3], 1.0))
    _long_shadow(c, VBIG, 0.150, p.bg2)
    _mark(c, p)


def h9(c, p):
    """等高線の上に、影ではなく厚みのある立体を置く。"""
    tile(c, p.bg, p.bg2, edge=None)
    _contours(c, p.shade)
    for i in range(16, 0, -1):
        d = i * 0.011
        c.stroke([(x + d, y + d * 0.62) for x, y in VBIG], 0.150, (p.shade, 1.0))
    _mark(c, p, w=0.150)


def h10(c, p):
    """紋の形に地を抜く。影だけが残り、紋そのものは空白になる。"""
    tile(c, p.bg, p.bg2, edge=None)
    _contours(c, p.shade, rings=8, gap=0.056, w=0.026)
    _long_shadow(c, VBIG, 0.170, p.mark)
    c.stroke(VBIG, 0.170, (p.bg, 1.0))


def h11(c, p):
    """等高線を平行線に崩す。中心が無限遠にある等高線と考えればよい。"""
    tile(c, p.bg, p.bg2, edge=None)
    for i in range(-2, 16):
        y = i * 0.075
        c.stroke([(-0.05, y), (0.5, y + 0.10), (1.05, y - 0.02)], 0.026,
                 (p.shade, 1.0))
    _long_shadow(c, VBIG, 0.165, p.bg2)
    _mark(c, p)


def h12(c, p):
    """等高線に三日月を重ね、月に長い影を付ける。"""
    tile(c, p.bg, p.bg2, edge=None)
    _contours(c, p.shade, rings=8, gap=0.056, w=0.024)
    for i in range(22, 0, -1):
        d = i * 0.042
        c.ring(0.46 + d, 0.48 + d, 0.330, 0.0, (p.shade, 1.0))
        c.ring(0.64 + d, 0.39 + d, 0.290, 0.0, (p.bg, 1.0))
    c.ring(0.460, 0.480, 0.330, 0.0, (p.mark, 1.0))
    c.ring(0.640, 0.390, 0.290, 0.0, (p.bg, 1.0))


def h13(c, p):
    """等高線に頭文字のMを重ね、そのMに長い影を付ける。"""
    tile(c, p.bg, p.bg2, edge=None)
    _contours(c, p.shade, rings=8, gap=0.056, w=0.024)
    M = [(0.075, 0.930), (0.075, 0.105), (0.500, 0.590),
         (0.925, 0.105), (0.925, 0.930)]
    _long_shadow(c, M, 0.170, p.bg2, reach=0.5, step=0.035)
    c.stroke(M, 0.170, (p.mark, 1.0))


def h14(c, p):
    """等高線を粗い格子に落とす。等高線をドット絵化したもの。"""
    tile(c, p.bg, p.bg2, edge=None)
    n = 16
    for iy in range(n):
        for ix in range(n):
            x, y = (ix + 0.5) / n, (iy + 0.5) / n
            d = math.hypot(x - 0.46, y - 0.54)
            if int(d * 13) % 2 == 0:
                c.round_rect(ix / n, iy / n, (ix + 1) / n, (iy + 1) / n, 0.0,
                             (p.shade, 1.0))
    _long_shadow(c, VBIG, 0.165, p.bg2)
    _mark(c, p)


HYBRID = [(f"H{i}", nm, _h(fn)) for i, (nm, fn) in enumerate([
    ("等高線の地＋影", h1), ("影を等高線で刻む", h2), ("紋だけ差し色", h3),
    ("紋から波紋", h4), ("四角い等高線", h5), ("影の内側だけ濃く", h6),
    ("紋を置かない", h7), ("三色刷りの等高線", h8), ("等高線＋立体", h9),
    ("紋を抜く", h10), ("平行線に崩す", h11), ("等高線＋三日月", h12),
    ("等高線＋M", h13), ("等高線をドット化", h14)], start=1)]


# ── C群: 代表的な手法 × 配色8種 ─────────────────────────────────────
def m_shadow(p):
    def d(c):
        tile(c, p.bg, p.bg2, edge=None)
        _long_shadow(c, VBIG, 0.165, p.shade)
        _mark(c, p)
    return d


def m_extrude(p):
    def d(c):
        tile(c, p.bg, p.bg2, edge=None)
        for i in range(16, 0, -1):
            dd = i * 0.011
            c.stroke([(x + dd, y + dd * 0.62) for x, y in VBIG], 0.150,
                     (p.shade, 1.0))
        _mark(c, p, w=0.150)
    return d


def m_contour(p):
    def d(c):
        tile(c, p.bg, p.bg2, edge=None)
        _contours(c, p.mark, rings=7, gap=0.062, w=0.030)
        c.ring(0.50 + 0.030 * 6, 0.52 - 0.024 * 6, 0.055, 0.0, (p.accent, 1.0))
    return d


def m_hybrid(p):
    def d(c):
        h1(c, p)
    return d


def m_origami(p):
    def d(c):
        tile(c, p.bg, p.bg2, edge=None)
        for pts, col in ((((0.00, 0.34), (0.35, 0.17), (0.35, 0.43), (0.00, 0.60)),
                          p.mark),
                         (((0.35, 0.17), (0.63, 0.58), (0.63, 0.84), (0.35, 0.43)),
                          p.shade),
                         (((0.63, 0.58), (1.00, 0.20), (1.00, 0.46), (0.63, 0.84)),
                          p.accent)):
            c.polygon(list(pts), (col, 1.0))
    return d


def m_crescent(p):
    def d(c):
        tile(c, p.bg, p.bg, edge=None)
        c.ring(0.485, 0.500, 0.375, 0.0, (p.mark, 1.0))
        c.ring(0.680, 0.395, 0.330, 0.0, (p.bg, 1.0))
        c.ring(0.255, 0.760, 0.045, 0.0, (p.accent, 1.0))
    return d


def m_bigm(p):
    def d(c):
        tile(c, p.bg, p.bg2, edge=None)
        c.stroke([(0.055, 0.955), (0.055, 0.075), (0.500, 0.605),
                  (0.945, 0.075), (0.945, 0.955)], 0.205, (p.mark, 1.0))
    return d


def m_pixel(p):
    def d(c):
        tile(c, p.bg, p.bg2, edge=None)
        n = len(PIXV)
        for iy, row in enumerate(PIXV):
            for ix, ch in enumerate(row):
                if ch == "X":
                    c.round_rect(ix / n, iy / n, (ix + 1) / n, (iy + 1) / n, 0.0,
                                 (p.mark, 1.0))
    return d


MECHS = [("長い影", m_shadow), ("押し出し", m_extrude), ("等高線", m_contour),
         ("影×等高線", m_hybrid), ("折り紙", m_origami), ("三日月", m_crescent),
         ("極太M", m_bigm), ("ドット絵", m_pixel)]

COLORWAY = [(f"C{mi * 8 + pi + 1}", f"{mn}／{p.name}", mf(p))
            for mi, (mn, mf) in enumerate(MECHS)
            for pi, p in enumerate(PALS)]


# ── F群: 新しい手法 ──────────────────────────────────────────────────
def _f(fn, pal=1):
    return lambda c: fn(c, PALS[pal])


def f1(c, p):
    """斜めの市松。45度に振るだけで印象が大きく変わる。"""
    tile(c, p.bg, p.bg2, edge=None)

    def chk(x, y):
        u, v = (x + y) * 7.0, (x - y) * 7.0
        return (int(u) + int(v + 10)) % 2 == 0
    c.shape((0, 0, 1, 1), chk, (p.mark, 1.0))
    c.ring(0.5, 0.5, 0.250, 0.0, (p.bg, 1.0))
    c.stroke(VBIG, 0.10, (p.accent, 1.0))


def f2(c, p):
    """同心の三角形。丸や四角と違い、向きを持った入れ子になる。"""
    tile(c, p.bg, p.bg2, edge=None)
    for i in range(6):
        s = 0.48 - i * 0.078
        if s <= 0.02:
            break
        tri = [(0.5, 0.5 - s), (0.5 + s * 0.9, 0.5 + s * 0.7),
               (0.5 - s * 0.9, 0.5 + s * 0.7)]
        c.stroke(tri + [tri[0]], 0.026, (p.mark if i % 2 else p.accent, 1.0))


def f3(c, p):
    """線の太さを位置で変える。均一な縞にはない立体感が出る。"""
    tile(c, p.bg, p.bg2, edge=None)
    for i in range(16):
        y = 0.03 + i * 0.062
        w = 0.010 + 0.042 * math.sin(i / 15.0 * math.pi)
        c.stroke([(0.03, y), (0.97, y)], w, (p.mark, 1.0))


def f4(c, p):
    """正弦波の束。位相をずらすと編み目のように見える。"""
    tile(c, p.bg, p.bg2, edge=None)
    for i in range(7):
        ph = i * 0.9
        pts = [(t / 40.0, 0.12 + i * 0.125 + 0.055 * math.sin(t / 40.0 * 7 + ph))
               for t in range(41)]
        c.stroke(pts, 0.030, (p.mark if i % 2 else p.accent, 1.0))


def f5(c, p):
    """格子を中心で膨らませる。魚眼レンズを通した方眼。"""
    tile(c, p.bg, p.bg2, edge=None)

    def warp(x, y):
        dx, dy = x - 0.5, y - 0.5
        d = math.hypot(dx, dy) or 1e-9
        k = 1.0 + 0.9 * math.exp(-(d * 3.2) ** 2)
        return 0.5 + dx / k, 0.5 + dy / k

    def grid(x, y):
        u, v = warp(x, y)
        return (u % 0.115) < 0.030 or (v % 0.115) < 0.030
    c.shape((0, 0, 1, 1), grid, (p.mark, 1.0))


def f6(c, p):
    """帯を交差させて結び目にする。前後関係が生まれる。"""
    tile(c, p.bg, p.bg2, edge=None)
    c.stroke([(0.10, 0.20), (0.90, 0.80)], 0.155, (p.shade, 1.0))
    c.stroke([(0.90, 0.20), (0.10, 0.80)], 0.155, (p.mark, 1.0))
    c.stroke([(0.10, 0.20), (0.42, 0.44)], 0.155, (p.accent, 1.0))


def f7(c, p):
    """段々のピラミッド。等高線を立体として解釈したもの。"""
    tile(c, p.bg, p.bg2, edge=None)
    for i in range(6):
        w = 0.46 - i * 0.072
        y = 0.86 - i * 0.115
        c.round_rect(0.5 - w, y - 0.105, 0.5 + w, y, 0.012,
                     (p.mark if i % 2 else p.shade, 1.0))


def f8(c, p):
    """放射状のくさび。中心から光が出ているように見える。"""
    tile(c, p.bg, p.bg2, edge=None)
    for i in range(12):
        a0 = i * 30.0
        c.polygon([(0.5, 0.5)] + [(0.5 + 1.2 * math.cos(math.radians(a)),
                                   0.5 + 1.2 * math.sin(math.radians(a)))
                                  for a in (a0, a0 + 15)],
                  (p.mark if i % 2 else p.accent, 1.0))
    c.ring(0.5, 0.5, 0.135, 0.0, (p.bg, 1.0))


def f9(c, p):
    """二重らせん。2本の正弦波が交差して捻れて見える。"""
    tile(c, p.bg, p.bg2, edge=None)
    for ph, col in ((0.0, p.mark), (math.pi, p.accent)):
        pts = [(0.5 + 0.30 * math.sin(t / 30.0 * 3.6 + ph), 0.04 + t / 30.0 * 0.92)
               for t in range(31)]
        c.stroke(pts, 0.055, (col, 1.0))


def f10(c, p):
    """六角格子。四角い格子より密で、生き物めいた印象になる。"""
    tile(c, p.bg, p.bg2, edge=None)
    for row in range(7):
        for col in range(7):
            cx = col * 0.168 + (0.084 if row % 2 else 0.0)
            cy = row * 0.146 + 0.05
            hexa = [(cx + 0.072 * math.cos(math.radians(a)),
                     cy + 0.072 * math.sin(math.radians(a)))
                    for a in range(0, 360, 60)]
            c.stroke(hexa + [hexa[0]], 0.016, (p.mark, 1.0))


def f11(c, p):
    """破線の同心円。実線より軽く、点描のような密度が出る。"""
    tile(c, p.bg, p.bg2, edge=None)
    for k in range(1, 8):
        r = k * 0.062
        n = 6 + k * 5
        for i in range(n):
            a = math.radians(i * 360.0 / n + k * 11)
            c.stroke(radial(0.5, 0.5, math.degrees(a), r - 0.020, r + 0.020),
                     0.020, (p.mark, 1.0))


def f12(c, p):
    """行ごとに横へずらした格子。信号が乱れたような不安定さが出る。"""
    tile(c, p.bg, p.bg2, edge=None)
    n = 12
    for iy in range(n):
        off = ((iy * 5) % 7) / 7.0 * (1.0 / n)
        for ix in range(n):
            if (ix + iy) % 3 == 0:
                continue
            x = ix / n + off
            c.round_rect(x, iy / n, x + 1.0 / n - 0.012,
                         (iy + 1) / n - 0.012, 0.0, (p.mark, 1.0))


def f13(c, p):
    """折れ線の下を塗る。線ではなく面積で値を示す。"""
    tile(c, p.bg, p.bg2, edge=None)
    line = [(-0.05, 0.70), (0.22, 0.52), (0.44, 0.62), (0.68, 0.30), (1.05, 0.16)]
    c.polygon(line + [(1.05, 1.05), (-0.05, 1.05)], (p.shade, 1.0))
    c.stroke(line, 0.055, (p.mark, 1.0))
    c.ring(0.68, 0.30, 0.062, 0.0, (p.accent, 1.0))


def f14(c, p):
    """影を左上へ落とす。光源が逆になるだけで座りが変わる。"""
    tile(c, p.bg, p.bg2, edge=None)
    for i in range(24, 0, -1):
        d = i * 0.040
        c.stroke([(x - d, y - d) for x, y in VBIG], 0.165, (p.shade, 1.0))
    _mark(c, p)


def f15(c, p):
    """紋を輪郭線だけにする。塗らないぶん地の色が主役になる。"""
    tile(c, p.bg, p.bg2, edge=None)
    c.stroke(VBIG, 0.200, (p.mark, 1.0))
    c.stroke(VBIG, 0.120, (p.bg, 1.0))


def f16(c, p):
    """同じ紋を三重にずらす。残像のような速度感が出る。"""
    tile(c, p.bg, p.bg2, edge=None)
    for d, col in ((0.085, p.shade), (0.045, p.accent), (0.0, p.mark)):
        c.stroke([(x - d, y - d * 0.5) for x, y in VBIG], 0.130, (col, 1.0))


def f17(c, p):
    """斜め縞の太さを端に向かって変える。均一な縞より視線が動く。"""
    tile(c, p.bg, p.bg2, edge=None)
    for i in range(18):
        t = i / 17.0
        x = -0.5 + i * 0.105
        w = 0.014 + 0.062 * t
        c.stroke([(x, -0.06), (x + 0.62, 1.06)], w, (p.mark, 1.0))


def f18(c, p):
    """角丸の入れ子。画面の形をそのまま繰り返す。"""
    tile(c, p.bg, p.bg2, edge=None)
    for i in range(6):
        s = i * 0.075
        col = p.mark if i % 2 else p.bg
        c.round_rect(s, s, 1 - s, 1 - s, max(0.02, 0.215 - s), (col, 1.0))


def f19(c, p):
    """月相を並べる。同じ図形の連なりでリズムを作る。"""
    tile(c, p.bg, p.bg2, edge=None)
    for i, k in enumerate((-0.30, -0.15, 0.0, 0.15, 0.30)):
        cx = 0.14 + i * 0.18
        c.ring(cx, 0.50, 0.082, 0.0, (p.mark, 1.0))
        if k:
            c.ring(cx + k * 0.55, 0.50, 0.082, 0.0, (p.bg, 1.0))


def f20(c, p):
    """点の大きさで濃淡を作る。中心から外へ向かって粗くなる。"""
    tile(c, p.bg, p.bg2, edge=None)
    n = 11
    for iy in range(n):
        for ix in range(n):
            x, y = (ix + 0.5) / n, (iy + 0.5) / n
            d = math.hypot(x - 0.46, y - 0.54)
            r = max(0.0, 0.055 * (1.0 - d * 1.5))
            if r > 0.004:
                c.ring(x, y, r, 0.0, (p.mark, 1.0))


FRESH = [(f"F{i}", nm, _f(fn)) for i, (nm, fn) in enumerate([
    ("斜め市松", f1), ("同心三角", f2), ("太さの変わる線", f3), ("波の束", f4),
    ("魚眼格子", f5), ("結び目", f6), ("段々", f7), ("放射くさび", f8),
    ("二重らせん", f9), ("六角格子", f10), ("破線の環", f11), ("ずれた格子", f12),
    ("面で示す", f13), ("影が逆", f14), ("輪郭だけ", f15), ("三重残像", f16),
    ("太る斜め縞", f17), ("角丸の入れ子", f18), ("月相", f19), ("点の濃淡", f20)],
    start=1)]

BULK = HYBRID + COLORWAY + FRESH


# 選べる案の一覧。定義がすべて出そろってから組む
# （N1_VARIANTS の先頭は NEW と同じ N1 なので重複させない）
ALL = (DESIGNS + MIXES + B_COLORS + NEW + N1_VARIANTS[1:] + ORANGE_NAVY
       + BULK)


def build_grid(items, cols=10, size=120):
    """一覧用の密なシート。小サイズの見本は付けず、絵だけを並べる。

    案が多いときはこちらを使う。気になったものだけ、あとから
    build_sheet で小サイズ込みの比較を作ればよい。
    """
    m, gap, lab = 20, 10, 18
    cw, chh = size, size + lab
    rows = -(-len(items) // cols)
    sh = Sheet(m * 2 + cols * cw + (cols - 1) * gap,
               m * 2 + rows * chh + (rows - 1) * gap, SHEET_BG)
    for i, (key, name, fn) in enumerate(items):
        cx = m + (i % cols) * (cw + gap)
        cy = m + (i // cols) * (chh + gap)
        sh.text(key, cx, cy + 3, SHEET_TEXT, 2)
        sh.paste(render(fn, size), cx, cy + lab)
    return sh


SHEETS = (("candidates", DESIGNS, 3, None), ("mix", MIXES, 3, None),
          ("colors", B_COLORS, 3, None), ("bold", NEW, 4, None),
          ("n1var", N1_VARIANTS, 4, None),
          ("orange_navy", ORANGE_NAVY, 4, ((32, 3), (16, 6))),
          ("bulk", BULK, 10, "grid"))

MAKE_ICON = os.path.join(BASE, "make_icon.py")


def apply_icon(key: str):
    """選んだ案を実際に使うアイコンにする。切り替えの入口はここ1本だけ。

    やることが4つある。1つでも欠けると「変えたのに変わらない」が起きる。
      1. 案の記号を入れた control_panel_<記号>.ico を書く
         （Windowsは絵をパス単位でキャッシュするので、名前ごと変える）
      2. 名前固定の control_panel.ico にも複製する
         （この名前を指している古いショートカット向け）
      3. 前の記号のファイルを消す
         make_shortcut.py は記号入りファイルを探すので、残っていると
         そちらを掴んで古い絵のままになる（実際にこれで嵌まった）
      4. make_icon.py の CHOICE を合わせる
         設定が2箇所に分かれていると必ず食い違うため、ここで揃える
    """
    key = key.upper()
    for k, name, fn in ALL:
        if k != key:
            continue
        path = os.path.join(BASE, f"control_panel_{k.lower()}.ico")
        write_ico(fn, path)
        shutil.copyfile(path, ICO)
        removed = []
        for old in glob.glob(os.path.join(BASE, "control_panel_*.ico")):
            if os.path.abspath(old) != os.path.abspath(path):
                os.remove(old)
                removed.append(os.path.basename(old))
        _rewrite_choice(k)
        return path, name, removed
    raise KeyError(key)


def _rewrite_choice(key: str) -> bool:
    """make_icon.py の CHOICE を書き換える。改行コードはそのまま保つ。"""
    try:
        with open(MAKE_ICON, encoding="utf-8", newline="") as f:
            s = f.read()
    except OSError:
        return False
    new, n = re.subn(r'(?m)^CHOICE\s*=\s*".*?"', f'CHOICE = "{key}"', s, count=1)
    if not n or new == s:
        return False
    with open(MAKE_ICON, "w", encoding="utf-8", newline="") as f:
        f.write(new)
    return True


def main():
    os.makedirs(OUTDIR, exist_ok=True)
    if "--ico" in sys.argv:
        key = sys.argv[sys.argv.index("--ico") + 1].upper()
        try:
            path, name, removed = apply_icon(key)
        except KeyError:
            print(f"そんな案はありません: {key}")
            print(f"  選べるのは {', '.join(k for k, _, _ in ALL)}")
            return 1
        print(f"{key}（{name}）に切り替えました")
        print(f"  {path}  ({os.path.getsize(path) // 1024}KB)")
        for r in removed:
            print(f"  前の {r} を削除")
        print(f"  make_icon.py の CHOICE を {key} に更新")
        print()
        print("最後にショートカットを作り直してください:")
        print("  ショートカットを作る.bat")
        return 0

    only = sys.argv[1] if len(sys.argv) > 1 else None
    for name, items, cols, smalls in SHEETS:
        if only and only != name:
            continue
        for key, label, fn in items:
            with open(os.path.join(OUTDIR, f"{key}.png"), "wb") as f:
                f.write(png_bytes(render(fn, 256)))
            print(f"  {key} {label}")
        p = os.path.join(OUTDIR, f"{name}.png")
        with open(p, "wb") as f:
            sh = (build_grid(items, cols) if smalls == "grid"
                  else build_sheet(items, cols, 176, smalls))
            f.write(png_bytes(sh.px))
        print(f"比較シート: {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
