# -*- coding: utf-8 -*-
"""需給ベースの新シグナル候補を、5分足で検証する。

目的は「直近20営業日で勝率70%以上」の売買ルールを見つけること。ただし
条件を探し回れば20営業日くらいの標本には必ず何か当たってしまうので、
**期間を2つに割って、後から見つけたルールが前の期間でも通用するか**を必ず見る。

  判定期間  直近20営業日          … 目標の勝率を測る対象
  検証期間  それより前の全期間    … 同じルールが通用するかの確認（後出しではない）

先読みはしない。
  ・シグナルの判定にはその足までの情報しか使わない
  ・約定は**次の足の始値**（同じ足の終値だと、その足の値動きを見てから買える）
  ・利確と損切りに同じ足で両方触れたら損切りが先（不利側に倒す）

実行:
    python analysis/research_signals.py
出力:
    analysis/output/research/signal_report.md
"""
import json
import os
import statistics as st
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from math import comb

BASE = os.path.dirname(os.path.abspath(__file__))
OUTDIR = os.path.join(BASE, "output", "research")
JST = timezone(timedelta(hours=9))

COST = 0.15
MIN_PRICE = 500.0        # 自動売買の下限に合わせる
TARGET_DAYS = 20         # 「直近20営業日」


def binom_p(k, n):
    if n == 0:
        return 1.0
    tail = sum(comb(n, i) for i in range(n + 1)
               if abs(i - n / 2) >= abs(k - n / 2))
    return min(1.0, tail / (2 ** n))


class Bar:
    __slots__ = ("t", "o", "h", "l", "c", "v")

    def __init__(self, t, o, h, l, c, v):
        self.t, self.o, self.h, self.l, self.c, self.v = t, o, h, l, c, v


def load(path=None):
    path = path or os.path.join(OUTDIR, "bars_ohlcv.json")
    raw = json.load(open(path))
    days = defaultdict(dict)          # date -> sym -> [Bar]
    for sym, d in raw.items():
        tmp = defaultdict(list)
        for i, ts in enumerate(d["ts"]):
            o, h, l, c = d["open"][i], d["high"][i], d["low"][i], d["close"][i]
            v = (d.get("volume") or [None] * len(d["ts"]))[i]
            if None in (o, h, l, c) or v is None:
                continue
            t = datetime.fromtimestamp(ts, JST)
            tmp[t.strftime("%Y-%m-%d")].append(Bar(t, o, h, l, c, v))
        for day, bars in tmp.items():
            bars.sort(key=lambda b: b.t)
            if len(bars) >= 20:       # 半日で終わった日などは捨てる
                days[day][sym] = bars
    return days


# ── 決済 ────────────────────────────────────────────────────────────
def simulate(bars, i0, tp, sl):
    """i0の足の始値で買う。損切り→利確→大引けの順に判定。"""
    entry = bars[i0].o
    if entry <= 0:
        return None
    for b in bars[i0:]:
        if sl is not None and b.l <= entry * (1 - sl / 100):
            return -sl
        if tp is not None and b.h >= entry * (1 + tp / 100):
            return tp
    return (bars[-1].c / entry - 1) * 100


# ── シグナル候補 ────────────────────────────────────────────────────
# それぞれ「その足までの情報だけ」で判定する。i は判定対象の足の添字。
# 返り値 True でエントリー候補（約定は i+1 の始値）。

def _ctx(bars, i):
    """その足までの当日情報をまとめる（先読みしない）。"""
    seen = bars[:i + 1]
    vols = [b.v for b in seen if b.v > 0]
    lows = [b.l for b in seen]
    highs = [b.h for b in seen]
    tv = sum(b.v for b in seen)
    vwap = (sum((b.h + b.l + b.c) / 3 * b.v for b in seen) / tv) if tv else seen[-1].c
    return {
        "med_vol": st.median(vols) if vols else 0.0,
        "day_low": min(lows), "day_high": max(highs), "vwap": vwap,
        "n": len(seen),
    }


def sig_absorption(bars, i, vol_mult=2.0, wick=0.6, near_low=0.3, after=13):
    """売り吸収: 出来高を伴う下ヒゲが当日安値圏で出る。

    売り物が出たのに終値が押し戻されている＝下値で買いが吸収している、という読み。
    """
    b = bars[i]
    if b.t.hour < after:
        return False
    c = _ctx(bars, i)
    if c["n"] < 12 or c["med_vol"] <= 0:
        return False
    rng = b.h - b.l
    if rng <= 0:
        return False
    if b.v < c["med_vol"] * vol_mult:
        return False
    if (b.c - b.l) / rng < wick:                       # 下ヒゲの長さ
        return False
    span = c["day_high"] - c["day_low"]
    if span <= 0:
        return False
    return (b.l - c["day_low"]) / span <= near_low     # 当日安値圏か


def sig_dryup(bars, i, vol_ratio=0.6, near_low=0.2, after=13):
    """売り枯れ: 当日安値圏なのに出来高が細っている。売り物が尽きたという読み。"""
    b = bars[i]
    if b.t.hour < after:
        return False
    c = _ctx(bars, i)
    if c["n"] < 12 or c["med_vol"] <= 0:
        return False
    if b.v > c["med_vol"] * vol_ratio:
        return False
    span = c["day_high"] - c["day_low"]
    if span <= 0:
        return False
    return (b.c - c["day_low"]) / span <= near_low


def sig_vwap_reclaim(bars, i, vol_mult=1.5, after=13):
    """VWAP奪回: VWAPを下回っていた価格が、出来高を伴って上抜ける。

    その日の平均コストを買い方が取り返した＝需給の主導権が移った、という読み。
    """
    if i < 1:
        return False
    b, p = bars[i], bars[i - 1]
    if b.t.hour < after:
        return False
    c = _ctx(bars, i)
    if c["n"] < 12 or c["med_vol"] <= 0:
        return False
    if b.v < c["med_vol"] * vol_mult:
        return False
    return p.c < c["vwap"] <= b.c and b.c > b.o


def sig_higher_low(bars, i, after=13, look=6):
    """下値切り上げ: 直近の押し安値が当日安値を切り上げ、陽線で確認。"""
    b = bars[i]
    if b.t.hour < after or i < look:
        return False
    c = _ctx(bars, i)
    if c["n"] < 12:
        return False
    recent_low = min(x.l for x in bars[i - look:i + 1])
    span = c["day_high"] - c["day_low"]
    if span <= 0:
        return False
    return (recent_low - c["day_low"]) / span >= 0.15 and b.c > b.o


def sig_gap_fill(bars, i, after=13, vol_mult=1.5):
    """寄り安の切り返し: 始値を下回っていた価格が出来高を伴って始値を回復。"""
    if i < 1:
        return False
    b, p = bars[i], bars[i - 1]
    if b.t.hour < after:
        return False
    c = _ctx(bars, i)
    if c["n"] < 12 or c["med_vol"] <= 0:
        return False
    day_open = bars[0].o
    return (p.c < day_open <= b.c and b.v >= c["med_vol"] * vol_mult
            and c["day_low"] < day_open)


CANDIDATES = [
    ("売り吸収", sig_absorption),
    ("売り枯れ", sig_dryup),
    ("VWAP奪回", sig_vwap_reclaim),
    ("下値切り上げ", sig_higher_low),
    ("寄り安の切り返し", sig_gap_fill),
]


# ── 検証 ────────────────────────────────────────────────────────────
def collect(days, fn, dates, **kw):
    """条件に合うエントリーを集める。1銘柄1日1回まで。"""
    out = []
    for day in dates:
        for sym, bars in days.get(day, {}).items():
            for i in range(len(bars) - 1):
                if bars[i + 1].o < MIN_PRICE:
                    continue
                try:
                    hit = fn(bars, i, **kw)
                except Exception:
                    hit = False
                if hit:
                    out.append({"date": day, "sym": sym, "bars": bars, "i0": i + 1})
                    break          # その日その銘柄は最初の1回だけ
    return out


def evaluate(entries, tp, sl):
    vals, syms = [], Counter()
    for e in entries:
        r = simulate(e["bars"], e["i0"], tp, sl)
        if r is None:
            continue
        vals.append(r)
        syms[e["sym"]] += 1
    n = len(vals)
    if not n:
        return None
    w = sum(1 for v in vals if v > 0)
    m = sum(vals) / n
    top3 = sum(c for _, c in syms.most_common(3))
    return {"n": n, "wr": w / n * 100, "mean": m, "ev": m - COST,
            "tot": sum(vals), "p": binom_p(w, n), "syms": len(syms),
            "conc": top3 / n * 100}


def row(label, s):
    if not s:
        return f"| {label} | — | | | | | |"
    star = "*" if s["p"] < 0.05 else ""
    return (f"| {label} | {s['n']} | {s['wr']:.1f}%{star} | {s['mean']:+.2f}% | "
            f"{s['ev']:+.2f}% | {s['syms']} | {s['conc']:.0f}% |")


HEAD = ("| 条件 | 件数 | 勝率 | 平均 | 期待値 | 銘柄数 | 上位3銘柄 |\n"
        "|---|---:|---:|---:|---:|---:|---:|")


def main():
    days = load()
    all_dates = sorted(days)
    target = all_dates[-TARGET_DAYS:]
    holdout = all_dates[:-TARGET_DAYS]
    out = ["# 需給シグナル候補の検証", "",
           f"営業日 {len(all_dates)}日（{all_dates[0]} 〜 {all_dates[-1]}）",
           f"　判定期間: 直近{len(target)}営業日（{target[0]} 〜 {target[-1]}）",
           f"　検証期間: それ以前 {len(holdout)}営業日"
           f"（{holdout[0]} 〜 {holdout[-1]}）" if holdout else "",
           f"　往復コスト {COST}% 控除 / {MIN_PRICE:.0f}円以上 / 1銘柄1日1回",
           "", "先読みなし（約定は次の足の始値、同足で両建てなら損切り優先）", ""]

    # 利確を小さく損切りを大きくすれば勝率はいくらでも上がるが、それは
    # 「勝率70%」の字面を満たすだけで中身は悪化する。比較できるよう
    # 非対称な組み合わせも並べたうえで、**期待値がプラスであること**を
    # 採用の必須条件にする。
    exits = [("利確+2%/損切り-2%", 2.0, 2.0), ("利確+2%/損切り-1%", 2.0, 1.0),
             ("利確+1.5%/損切り-1%", 1.5, 1.0), ("利確+3%/損切り-2%", 3.0, 2.0),
             ("利確+1%/損切り-2%（勝率狙い）", 1.0, 2.0),
             ("利確+1%/損切り-3%（勝率狙い）", 1.0, 3.0),
             ("利確なし/損切り-2%", None, 2.0), ("大引けのみ", None, None)]

    best = []
    for name, fn in CANDIDATES:
        et = collect(days, fn, target)
        eh = collect(days, fn, holdout)
        out += [f"## {name}", "", HEAD]
        for lbl, tp, sl in exits:
            st_ = evaluate(et, tp, sl)
            sh_ = evaluate(eh, tp, sl)
            out.append(row(f"判定 {lbl}", st_))
            out.append(row(f"　検証 {lbl}", sh_))
            # 採用の条件: 勝率70%以上・件数15以上・**期待値がプラス**
            if st_ and st_["wr"] >= 70 and st_["n"] >= 15 and st_["ev"] > 0:
                best.append((name, lbl, st_, sh_))
        out.append("")

    out += ["## 目標（勝率70%以上・n>=15・期待値プラス）を満たしたもの", "", HEAD]
    if best:
        for name, lbl, st_, sh_ in sorted(best, key=lambda x: -x[2]["wr"]):
            out.append(row(f"{name} / {lbl}", st_))
            out.append(row(f"　→ 検証期間での再現", sh_))
    else:
        out.append("| （なし） | | | | | | |")
    out.append("")

    path = os.path.join(OUTDIR, "signal_report.md")
    open(path, "w", encoding="utf-8", newline="\n").write("\n".join(out) + "\n")
    print("\n".join(out[:6]))
    print(f"\n条件を満たした組み合わせ: {len(best)}件")
    for name, lbl, st_, sh_ in sorted(best, key=lambda x: -x[2]["wr"])[:8]:
        hv = f"検証{sh_['wr']:.1f}%/{sh_['ev']:+.2f}%" if sh_ else "検証データなし"
        print(f"  {name:<12} {lbl:<18} 判定 n={st_['n']:>3} "
              f"勝率{st_['wr']:>5.1f}% 期待値{st_['ev']:>+6.2f}%  ({hv})")
    print(f"\n詳細: {path}")


if __name__ == "__main__":
    main()
