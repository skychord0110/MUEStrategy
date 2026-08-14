# -*- coding: utf-8 -*-
"""AI仮想売買（実稼働フォワード）の成績集計と、翌日持ち越しのリスク分布。

実行: python -X utf8 summarize_2026-08-14.py
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

print("=" * 78)
print("M. AI仮想売買の実稼働フォワード成績（ログに残った実際の判定）")
print("=" * 78)
papers = list(csv.DictReader(
    open(os.path.join(OUT5, "paper_trades_2026-08-14.csv"), encoding="utf-8")))
exits = [p for p in papers if p["kind"] == "EXIT" and p["ret"] not in ("", None)]
by = defaultdict(list)
for p in exits:
    by[p["strategy"]].append(p)

for strat in sorted(by):
    rows = by[strat]
    v = [float(r["ret"]) for r in rows]
    print(f"\n  【{strat}】 n={len(v)}  勝率{sum(1 for a in v if a > 0) / len(v) * 100:.1f}%"
          f"  期待値{statistics.mean(v):+.3f}%  コスト後{statistics.mean(v) - COST:+.3f}%")
    parts = []
    for w in WEEKS:
        sub = [float(r["ret"]) for r in rows if r["week"] == w]
        parts.append(f"w{w[-1]}:{'—' if not sub else f'{statistics.mean(sub):+.2f}%'}({len(sub)})")
    print(f"      週別 {'  '.join(parts)}")
    reasons = defaultdict(list)
    for r in rows:
        reasons[r["reason"] or "?"].append(float(r["ret"]))
    for k, v2 in sorted(reasons.items(), key=lambda x: -len(x[1])):
        print(f"      決済理由 {k:<10} {len(v2):>3}件 平均{statistics.mean(v2):+.3f}%")

print()
print("=" * 78)
print("N. 翌日持ち越しのリスク分布（中核シグナル・翌日の寄りで手仕舞い）")
print("=" * 78)
res = json.load(open(os.path.join(OUT5, "eval_results_2026-08-14.json")))
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

core = {}
for r in sorted([r for r in res if r["strategy"] == "UNDER急増"
                 and r["time"] >= "13:00:00" and r["entry"] >= 500],
                key=lambda x: (x["date"], x["time"])):
    core.setdefault((r["symbol"], r["date"]), r)
core = list(core.values())

on = []
for r in core:
    ds = daily.get(r["symbol"], {})
    dates = list(ds)
    if r["date"] not in dates:
        continue
    i = dates.index(r["date"])
    if i + 1 >= len(dates):
        continue
    on.append((ds[dates[i + 1]][0] - r["entry"]) / r["entry"] * 100)

on.sort()
n = len(on)
print(f"  n={n}")
print(f"  平均   {statistics.mean(on):+.3f}%   中央値 {statistics.median(on):+.3f}%")
print(f"  最小   {on[0]:+.2f}%      最大   {on[-1]:+.2f}%")
print(f"  下位5% {on[int(n * 0.05)]:+.2f}%      上位5% {on[int(n * 0.95)]:+.2f}%")
for th in (-2, -3, -5):
    k = sum(1 for v in on if v <= th)
    print(f"  {th}%以下: {k}件 ({k / n * 100:.1f}%)")
top3 = on[-3:]
rest = statistics.mean(on[:-3])
print(f"  上位3件を除いた平均: {rest:+.3f}%（上位3件は {', '.join(f'{v:+.1f}%' for v in top3)}）")

print()
print("  比較: 当日大引けで手仕舞った場合")
cl = sorted(r["rclose"] for r in core)
print(f"  平均   {statistics.mean(cl):+.3f}%   中央値 {statistics.median(cl):+.3f}%")
print(f"  最小   {cl[0]:+.2f}%      最大   {cl[-1]:+.2f}%")
