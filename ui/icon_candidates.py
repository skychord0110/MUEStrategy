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
import math
import os
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
        """定数色か線形グラデーションを (x, y) -> (r, g, b, a) の関数にする。"""
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

    def _blend(self, ix, iy, col):
        r, g, b, a = col
        if a <= 0:
            return
        i = (iy * self.w + ix) * 4
        buf = self.buf
        ia = 1.0 - a
        buf[i] = r * a + buf[i] * ia
        buf[i + 1] = g * a + buf[i + 1] * ia
        buf[i + 2] = b * a + buf[i + 2] * ia
        buf[i + 3] = a + buf[i + 3] * ia

    def _scan(self, box, inside, paint):
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
                    self._blend(ix, iy, f(x, y))

    # -- 図形 --------------------------------------------------------------
    def round_rect(self, x0, y0, x1, y1, r, paint):
        self._scan((x0, y0, x1, y1), rr_inside(x0, y0, x1, y1, r), paint)

    def taper(self, p0, p1, p2, w0, w1, paint, n=28):
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
        self.polygon(left + right[::-1], paint)

    def ring(self, cx, cy, r_out, r_in, paint, a0=None, a1=None):
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
        self._scan((cx - r_out, cy - r_out, cx + r_out, cy + r_out), inside, paint)

    def stroke(self, pts, width, paint, cap="round"):
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
        self._scan((min(xs) - h, min(ys) - h, max(xs) + h, max(ys) + h), inside, paint)

    def polygon(self, pts, paint):
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
        self._scan((min(xs), min(ys), max(xs), max(ys)), inside, paint)

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

ALL = DESIGNS + MIXES + B_COLORS


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


def build_sheet(items):
    big, cols = 176, 3
    cw, chh, m, gap = 366, 362, 20, 16
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
        for size, mag in ((48, 2), (32, 3), (16, 6)):
            img = render(fn, size)
            sh.paste(img, x, y, mag)
            sh.text(str(size), x + size * mag + 8, y + size * mag // 2 - 7,
                    SHEET_TEXT, 2)
            y += size * mag + 12
    return sh


SHEETS = (("candidates", DESIGNS), ("mix", MIXES), ("colors", B_COLORS))


def main():
    os.makedirs(OUTDIR, exist_ok=True)
    if "--ico" in sys.argv:
        key = sys.argv[sys.argv.index("--ico") + 1].upper()
        for k, name, fn in ALL:
            if k == key:
                write_ico(fn)
                print(f"{k}（{name}）を {ICO} に書き出しました "
                      f"({os.path.getsize(ICO) // 1024}KB)")
                return 0
        print(f"そんな案はありません: {key}"
              f"（選べるのは {', '.join(k for k, _, _ in ALL)}）")
        return 1

    only = sys.argv[1] if len(sys.argv) > 1 else None
    for name, items in SHEETS:
        if only and only != name:
            continue
        for key, label, fn in items:
            with open(os.path.join(OUTDIR, f"{key}.png"), "wb") as f:
                f.write(png_bytes(render(fn, 256)))
            print(f"  {key} {label}")
        p = os.path.join(OUTDIR, f"{name}.png")
        with open(p, "wb") as f:
            f.write(png_bytes(build_sheet(items).px))
        print(f"比較シート: {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
