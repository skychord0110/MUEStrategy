# -*- coding: utf-8 -*-
"""需給シグナルを「そのとき買っていたらどうなったか」で検証する。

先読みを避けるため、約定はアラート**直後の5分足の始値**とする
（アラートと同じ足の終値を使うと、その足の中の値動きを見てから
買ったことになってしまう）。

決済は2通りを見る。
  ・時間で切る    +15分 / +30分 / +60分 / 大引け
  ・値幅で切る    利確と損切りの組み合わせ。同じ足で両方に触れたら
                  損切り側が先に約定したとみなす（不利な側に倒す）

入力: analysis/output/2026-08-21/alerts_2026-08-21.csv
      analysis/output/2026-08-21/bars_5m_2026-08-21.json
実行: python -X utf8 analysis/evaluate_signals_2026-08-21.py
"""
import csv
import json
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from math import comb

BASE = os.path.dirname(os.path.abspath(__file__))
OUTDIR = os.path.join(BASE, "output", "2026-08-21")
JST = timezone(timedelta(hours=9))
COST = 0.15
MIN_PRICE = 500.0          # 自動売買の下限に合わせる


def binom_p(k, n):
    if n == 0:
        return 1.0
    tail = sum(comb(n, i) for i in range(n + 1)
               if abs(i - n / 2) >= abs(k - n / 2))
    return min(1.0, tail / (2 ** n))


def load_bars():
    with open(os.path.join(OUTDIR, "bars_5m_2026-08-21.json")) as f:
        raw = json.load(f)
    bars = defaultdict(lambda: defaultdict(list))     # sym -> date -> [(t,o,h,l,c)]
    for sym, d in raw.items():
        for i, ts in enumerate(d["ts"]):
            o, h, l, c = (d["open"][i], d["high"][i], d["low"][i], d["close"][i])
            if None in (o, h, l, c):
                continue
            t = datetime.fromtimestamp(ts, JST)
            bars[sym][t.strftime("%Y-%m-%d")].append((t, o, h, l, c))
    for sym in bars:
        for day in bars[sym]:
            bars[sym][day].sort()
    return bars


def simulate(day_bars, i0, tp, sl):
    """i0番目の足の始値で買い、利確/損切り/大引けのどれかで閉じる。"""
    entry = day_bars[i0][1]
    if entry <= 0:
        return None
    for _, o, h, l, c in day_bars[i0:]:
        if sl is not None and l <= entry * (1 - sl / 100):
            return -sl, "損切り"
        if tp is not None and h >= entry * (1 + tp / 100):
            return tp, "利確"
    return (day_bars[-1][4] / entry - 1) * 100, "大引け"


def horizon(day_bars, i0, minutes):
    entry = day_bars[i0][1]
    limit = day_bars[i0][0] + timedelta(minutes=minutes)
    px = day_bars[-1][4]
    for t, o, h, l, c in day_bars[i0:]:
        if t >= limit:
            px = c
            break
    return (px / entry - 1) * 100


def row(label, vals, width=26):
    n = len(vals)
    if not n:
        return f"  {label:<{width}} —"
    w = sum(1 for v in vals if v > 0)
    mean = sum(vals) / n
    star = "*" if binom_p(w, n) < 0.05 else " "
    return (f"  {label:<{width}} n={n:>4}  勝率{w / n * 100:>5.1f}%{star} "
            f"平均{mean:>+6.2f}%  期待値{mean - COST:>+6.2f}%  "
            f"累計{sum(vals):>+8.1f}%")


def main():
    bars = load_bars()
    with open(os.path.join(OUTDIR, "alerts_2026-08-21.csv"),
              encoding="utf-8-sig") as f:
        alerts = list(csv.DictReader(f))

    # ここでは重複を落とさない。午前で1件使うと午後の同一銘柄が消えてしまい、
    # 「午後だけ見る」戦略の検証ができなくなるため、絞り込みは群ごとに行う。
    entries = []
    skipped = defaultdict(int)
    for a in alerts:
        db = bars.get(a["symbol"], {}).get(a["date"])
        if not db:
            skipped["5分足が無い"] += 1
            continue
        t = datetime.strptime(f"{a['date']} {a['time']}", "%Y-%m-%d %H:%M:%S")
        t = t.replace(tzinfo=JST)
        i0 = next((i for i, b in enumerate(db) if b[0] > t), None)
        if i0 is None or i0 >= len(db) - 1:
            skipped["直後の足が無い（引け間際）"] += 1
            continue
        if db[i0][1] < MIN_PRICE:
            skipped["500円未満"] += 1
            continue
        entries.append({"a": a, "db": db, "i0": i0,
                        "entry": db[i0][1], "hour": int(a["time"][:2])})

    print(f"検証対象 {len(entries)}件（アラート {len(alerts)}件から抽出）")
    for k, v in skipped.items():
        print(f"  除外 {k}: {v}件")

    def sub(pred):
        """条件に合う候補を、その日その銘柄の最初の1件だけに絞る。

        実運用でも建玉は1銘柄につき1つしか持たないため、同じ日に何度
        シグナルが出ても取れるのは最初の1回だけ。
        """
        out, seen = [], set()
        for e in entries:
            if not pred(e):
                continue
            k = (e["a"]["date"], e["a"]["symbol"], e["a"]["kind"],
                 e["a"]["level"])
            if k in seen:
                continue
            seen.add(k)
            out.append(e)
        return out

    groups = [
        ("UNDER急増 全体", sub(lambda e: e["a"]["kind"] == "UNDER急増")),
        ("  うち午前(〜12時)", sub(lambda e: e["a"]["kind"] == "UNDER急増"
                                and e["hour"] < 12)),
        ("  うち午後(13時〜)", sub(lambda e: e["a"]["kind"] == "UNDER急増"
                                and e["hour"] >= 13)),
        ("小口売り連続 STRONG", sub(lambda e: e["a"]["kind"] == "小口売り連続"
                                 and e["a"]["level"] == "STRONG")),
        ("小口売り連続 WATCH", sub(lambda e: e["a"]["kind"] == "小口売り連続"
                                and e["a"]["level"] == "WATCH")),
        ("定期買い集め STRONG", sub(lambda e: e["a"]["kind"] == "定期買い集め"
                                 and e["a"]["level"] == "STRONG")),
        ("定期買い集め WATCH", sub(lambda e: e["a"]["kind"] == "定期買い集め"
                                and e["a"]["level"] == "WATCH")),
    ]

    print("\n━━ 時間で決済した場合 ━━")
    for mins in (15, 30, 60):
        print(f"\n  ◆ {mins}分後に決済")
        for name, es in groups:
            print(row(name, [horizon(e["db"], e["i0"], mins) for e in es]))
    print("\n  ◆ 大引けで決済")
    for name, es in groups:
        print(row(name, [horizon(e["db"], e["i0"], 600) for e in es]))

    print("\n━━ 値幅で決済した場合（利確/損切り、届かなければ大引け）━━")
    for tp, sl in ((2.0, 1.0), (2.0, 2.0), (1.5, 1.0), (1.0, 1.0), (3.0, 1.5)):
        print(f"\n  ◆ 利確+{tp}% / 損切り-{sl}%")
        for name, es in groups:
            res = [simulate(e["db"], e["i0"], tp, sl) for e in es]
            print(row(name, [r[0] for r in res if r]))

    print("\n━━ UNDER急増を時間帯で刻む（利確+2%/損切り-2%）━━")
    # 時間帯で絞ってから1銘柄1日1件にする。順番が逆だと、午前で枠を使った
    # 銘柄の午後シグナルが消えてしまい、午後の件数が実際より少なく出る。
    for lo, hi, lbl in ((9, 10, "09時台"), (10, 11, "10時台"), (11, 13, "11-12時"),
                        (13, 14, "13時台"), (14, 15, "14時台"), (15, 16, "15時台")):
        es = sub(lambda e, lo=lo, hi=hi: e["a"]["kind"] == "UNDER急増"
                 and lo <= e["hour"] < hi)
        res = [simulate(e["db"], e["i0"], 2.0, 2.0) for e in es]
        print(row(lbl, [r[0] for r in res if r]))

    print("\n━━ UNDER急増をUNDER増加率で刻む（利確+2%/損切り-2%・午後のみ）━━")
    for lo, hi in ((0, 30), (30, 50), (50, 80), (80, 10000)):
        es = sub(lambda e, lo=lo, hi=hi: e["a"]["kind"] == "UNDER急増"
                 and e["hour"] >= 13 and lo <= float(e["a"]["v1"] or 0) < hi)
        res = [simulate(e["db"], e["i0"], 2.0, 2.0) for e in es]
        print(row(f"UNDER +{lo}〜{hi if hi < 9999 else '∞'}%",
                  [r[0] for r in res if r]))

    print("\n━━ 定期買い集め STRONG の掘り下げ ━━")
    acc = sub(lambda e: e["a"]["kind"] == "定期買い集め"
              and e["a"]["level"] == "STRONG")
    print(f"  対象 {len(acc)}件 / {len({e['a']['symbol'] for e in acc})}銘柄 "
          f"/ {len({e['a']['date'] for e in acc})}日")
    for lbl, tp, sl in (("利確なし・大引けのみ", None, None),
                        ("損切り-1%のみ", None, 1.0),
                        ("損切り-2%のみ", None, 2.0),
                        ("利確+2%/損切り-1%", 2.0, 1.0),
                        ("利確+2%/損切り-2%", 2.0, 2.0),
                        ("利確+3%/損切り-2%", 3.0, 2.0)):
        res = [simulate(e["db"], e["i0"], tp, sl) for e in acc]
        print(row(lbl, [r[0] for r in res if r]))
    print("  z値で刻む（損切り-2%のみ）:")
    for lo, hi in ((0, 8), (8, 12), (12, 999)):
        es = [e for e in acc if lo <= float(e["a"]["v1"] or 0) < hi]
        res = [simulate(e["db"], e["i0"], None, 2.0) for e in es]
        print(row(f"    z {lo}〜{hi if hi < 998 else '∞'}",
                  [r[0] for r in res if r]))
    print("  時間帯で刻む（損切り-2%のみ）:")
    for lo, hi, lbl in ((9, 11, "午前前半"), (11, 13, "昼"), (13, 16, "午後")):
        es = [e for e in acc if lo <= e["hour"] < hi]
        res = [simulate(e["db"], e["i0"], None, 2.0) for e in es]
        print(row(f"    {lbl}", [r[0] for r in res if r]))


if __name__ == "__main__":
    main()
