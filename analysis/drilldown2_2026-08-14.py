# -*- coding: utf-8 -*-
"""追加検証: 同時多発シグナルの扱い / 投げ売りの週別 / 翌日持ち越しの週別 / z値シグナル。

week5で中核戦略が弱かった原因を追う。8/13は14:17〜14:22の5分間に9銘柄が同時に
UNDER急増を出しており、銘柄固有ではなく相場全体の動き（指数連動の買い板出現など）
の可能性がある。これはシグナル時点で判別できる（＝先読みにならない）。

実行: python -X utf8 drilldown2_2026-08-14.py
"""
import csv
import json
import os
import statistics
from collections import defaultdict
from datetime import datetime, timezone, timedelta

BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(BASE, "output")
OUT5 = os.path.join(OUT, "2026-08-14")
JST = timezone(timedelta(hours=9))
COST = 0.15
WEEKS = ("week1", "week2", "week3", "week4", "week5")

res = json.load(open(os.path.join(OUT5, "eval_results_2026-08-14.json")))
alerts = list(csv.DictReader(
    open(os.path.join(OUT5, "alerts_2026-08-14.csv"), encoding="utf-8")))

bars = defaultdict(list)
for p in [os.path.join(OUT, fn) for fn in
          ("bars_5m.json", "bars_5m_week2.json", "bars_5m_week3.json", "bars_5m_week4.json")] \
        + [os.path.join(OUT5, "bars_5m_week5.json")]:
    if not os.path.exists(p):
        continue
    for sym, d in json.load(open(p)).items():
        for ts, o, h, l, c in zip(d["ts"], d["open"], d["high"], d["low"], d["close"]):
            if None in (o, h, l, c):
                continue
            dt = datetime.fromtimestamp(ts, JST)
            bars[(sym, dt.strftime("%Y-%m-%d"))].append((dt, o, h, l, c))
for k in bars:
    bars[k].sort()
daily = defaultdict(dict)
for (sym, d), b in bars.items():
    daily[sym][d] = (b[0][1], max(x[2] for x in b), min(x[3] for x in b), b[-1][4])
for sym in daily:
    daily[sym] = dict(sorted(daily[sym].items()))


def secs(t):
    h, m, s = (int(x) for x in t.split(":"))
    return h * 3600 + m * 60 + s


def line(name, rows, key="sl2.0_tp2.0", min_n=8):
    if len(rows) < min_n:
        print(f"  {name:<36} n={len(rows):<4} 件数不足")
        return
    v = [r[key] for r in rows]
    print(f"  {name:<36} n={len(v):<4} 勝率{sum(1 for a in v if a > 0) / len(v) * 100:5.1f}%"
          f"  期待値{statistics.mean(v):+7.3f}%  コスト後{statistics.mean(v) - COST:+7.3f}%")


def weekly(rows, key="sl2.0_tp2.0"):
    parts = []
    for w in WEEKS:
        sub = [r for r in rows if r["week"] == w]
        parts.append(f"w{w[-1]}:{'—' if not sub else f'{statistics.mean([x[key] for x in sub]):+.2f}%'}"
                     f"({len(sub)})")
    print(f"      週別 {'  '.join(parts)}")


core = {}
for r in sorted([r for r in res if r["strategy"] == "UNDER急増"
                 and r["time"] >= "13:00:00" and r["entry"] >= 500],
                key=lambda x: (x["date"], x["time"])):
    core.setdefault((r["symbol"], r["date"]), r)
core = list(core.values())

# ── 同時多発の判定（シグナル時点で分かる情報のみ）──
# その銘柄のシグナル時刻の「直前5分以内」に、他の何銘柄がUNDER急増を出していたか
under_times = defaultdict(list)
for a in alerts:
    if a["strategy"] == "UNDER急増":
        under_times[a["date"]].append((secs(a["time"]), a["symbol"]))
for d in under_times:
    under_times[d].sort()

for r in core:
    t = secs(r["time"])
    others = {s for tt, s in under_times[r["date"]]
              if t - 300 <= tt <= t and s != r["symbol"]}
    r["concurrent"] = len(others)

print("=" * 78)
print("I. 同時多発シグナルの影響（直前5分以内に他の何銘柄がUNDER急増したか）")
print("=" * 78)
for lo, hi, lab in ((0, 1, "他0銘柄（単独）"), (1, 3, "他1〜2銘柄"),
                    (3, 6, "他3〜5銘柄"), (6, 10 ** 9, "他6銘柄以上（相場全体）")):
    sub = [r for r in core if lo <= r["concurrent"] < hi]
    line(lab, sub)
    if len(sub) >= 8:
        weekly(sub)
print()
line("他2銘柄以下だけ採用（提案フィルタ）", [r for r in core if r["concurrent"] <= 2])
weekly([r for r in core if r["concurrent"] <= 2])
line("他3銘柄以上（除外対象）", [r for r in core if r["concurrent"] >= 3])
weekly([r for r in core if r["concurrent"] >= 3])

print()
print("=" * 78)
print("J. 投げ売り検知（1日1回）の決済ルール別・週別")
print("=" * 78)
panic = {}
for r in sorted([r for r in res if r["strategy"].startswith("投げ売り")],
                key=lambda x: (x["date"], x["time"])):
    panic.setdefault((r["symbol"], r["date"]), r)
panic = list(panic.values())
for key in ("sl1.0_tp1.0", "sl1.5_tp3.0", "sl2.0_tp2.0"):
    line(key, panic, key=key, min_n=5)
    weekly(panic, key)
print(f"  参考: 最大益の中央値 {statistics.median([r['mfe'] for r in panic]):+.2f}% / "
      f"最大損の中央値 {statistics.median([r['mae'] for r in panic]):+.2f}%")

print()
print("=" * 78)
print("K. 翌日持ち越しの週別（中核シグナル・翌日の寄りで手仕舞い）")
print("=" * 78)


def next_open(r):
    ds = daily.get(r["symbol"])
    if not ds or r["date"] not in ds:
        return None
    dates = list(ds)
    i = dates.index(r["date"])
    if i + 1 >= len(dates):
        return None
    return (ds[dates[i + 1]][0] - r["entry"]) / r["entry"] * 100


on = []
for r in core:
    v = next_open(r)
    if v is not None:
        on.append(dict(r, overnight=v))
line("翌日の寄りで手仕舞い", on, key="overnight")
weekly(on, "overnight")
line("  └ 当日大引けで手仕舞い（比較）", [r for r in on], key="rclose")
weekly(on, "rclose")
worst = sorted(on, key=lambda x: x["overnight"])[:5]
print("  最悪だった5件（持ち越しの尾を確認）:")
for r in worst:
    print(f"    {r['date']} {r['symbol']} 翌寄り{r['overnight']:+6.2f}%  "
          f"（当日大引け{r['rclose']:+6.2f}%）")

print()
print("=" * 78)
print("L. z値の定期買い集め検知（8/12稼働開始・11件）")
print("=" * 78)
zpath = os.path.join(OUT5, "zscore_signals_2026-08-14.csv")
if os.path.exists(zpath):
    zs = list(csv.DictReader(open(zpath, encoding="utf-8")))
    print(f"  シグナル {len(zs)}件")
    for z in zs:
        ds = daily.get(z["symbol"], {})
        dates = list(ds)
        if z["date"] not in dates:
            print(f"    {z['date']} {z['time']} {z['symbol']} {z['tier']}: 日足なし")
            continue
        i = dates.index(z["date"])
        o, h, l, c = ds[z["date"]]
        nxt = ""
        if i + 1 < len(dates):
            nc = ds[dates[i + 1]][3]
            nxt = f" / 翌日終値まで {(nc - c) / c * 100:+.2f}%"
        print(f"    {z['date']} {z['time']} {z['symbol']} {z['tier']}: "
              f"当日終値{c:.0f}円{nxt}")
else:
    print("  z値シグナルのCSVがありません")
