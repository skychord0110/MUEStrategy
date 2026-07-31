# -*- coding: utf-8 -*-
"""3週間データから、需給読みに基づく買い戦略の候補を探索する。

入力: output/eval_results_2026-07-31.json, alerts_2026-07-31.csv, bars_5m*.json
実行: python -X utf8 drilldown_2026-07-31.py

検証する仮説:
  A. 投げ売り検知（安値圏でOVER急減→買い気配へぶつけ）＝セリクラの底
  B. 同一銘柄・同日のUNDER急増の多発＝下値の買い集めが厚い
  C. 市場全体のアラート数＝地合いフィルタ（全体が売られた日ほど反発する？）
  D. 前場に売られた銘柄を後場寄りで買う（時間帯の非対称性の利用）
  E. UNDER急増の増加率の大きさ別
"""
import csv
import json
import os
import statistics
from collections import defaultdict
from datetime import datetime, timezone, timedelta

BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(BASE, "output")
JST = timezone(timedelta(hours=9))

res = json.load(open(os.path.join(OUT, "eval_results_2026-07-31.json")))
alerts = list(csv.DictReader(open(os.path.join(OUT, "alerts_2026-07-31.csv"), encoding="utf-8")))

bars = defaultdict(list)
for fn in ("bars_5m.json", "bars_5m_week2.json", "bars_5m_week3.json"):
    p = os.path.join(OUT, fn)
    if not os.path.exists(p):
        continue
    for sym, d in json.load(open(p)).items():
        for ts, o, h, l, c, v in zip(d["ts"], d["open"], d["high"], d["low"], d["close"], d["volume"]):
            if None in (o, h, l, c):
                continue
            dt = datetime.fromtimestamp(ts, JST)
            bars[(sym, dt.strftime("%Y-%m-%d"))].append((dt, o, h, l, c, v))
for k in bars:
    bars[k].sort()


def rep(rows, name, rules=("rclose", "sl1.0_tp1.0", "sl1.5_tp3.0", "sl2.0_tp2.0"), min_n=5):
    if len(rows) < min_n:
        print(f"  {name:<40} n={len(rows):<4} 不足")
        return
    out = f"  {name:<40} n={len(rows):<4}"
    for r in rules:
        v = [x[r] for x in rows]
        out += f" | {r}: 勝率{sum(1 for a in v if a>0)/len(v)*100:>5.1f}% 期待値{statistics.mean(v):+.3f}%"
    print(out)


print("=" * 110)
print("【A】投げ売り検知（セリクラ）")
print("=" * 110)
panic = [r for r in res if r["strategy"].startswith("投げ売り")]
rep(panic, "全件")
rep([r for r in panic if r["time"] < "11:00:00"], "前場(〜11時)")
rep([r for r in panic if r["time"] >= "12:30:00"], "後場(12:30〜)")
for w in ("week1", "week2", "week3"):
    rep([r for r in panic if r["week"] == w], f"  {w}")
print("  ※明細:")
for r in sorted(panic, key=lambda x: (x["date"], x["time"])):
    print(f"    {r['date']} {r['time']} {r['symbol']} {r['entry']:>7.0f}円 "
          f"→ r30 {r['r30']:+6.2f}% / 引け {r['rclose']:+6.2f}% / SL1TP1 {r['sl1.0_tp1.0']:+5.2f}%")

print()
print("=" * 110)
print("【B】同一銘柄・同日のUNDER急増の多発度（買い集めの厚さ）")
print("=" * 110)
cnt = defaultdict(int)
for a in alerts:
    if a["strategy"].startswith("UNDER"):
        cnt[(a["symbol"], a["date"])] += 1
us = [r for r in res if r["strategy"].startswith("UNDER")]
for r in us:
    r["day_count"] = cnt[(r["symbol"], r["date"])]
for lo, hi, nm in ((1, 1, "1回のみ"), (2, 3, "2〜3回"), (4, 6, "4〜6回"), (7, 99, "7回以上")):
    rep([r for r in us if lo <= r["day_count"] <= hi], f"当日のUNDER急増 {nm}")
print("  ※午後(13時〜)に限定:")
for lo, hi, nm in ((1, 1, "1回のみ"), (2, 3, "2〜3回"), (4, 99, "4回以上")):
    rep([r for r in us if lo <= r["day_count"] <= hi and r["time"] >= "13:00:00"],
        f"  午後×{nm}")

print()
print("=" * 110)
print("【C】市場全体のアラート数＝地合いフィルタ")
print("=" * 110)
daily = defaultdict(int)
for a in alerts:
    daily[a["date"]] += 1
med = statistics.median(daily.values())
print(f"  1日あたりアラート数の中央値: {med:.0f}")
for r in res:
    r["day_alerts"] = daily[r["date"]]
rep([r for r in res if r["day_alerts"] >= med], f"アラート多い日(>={med:.0f})")
rep([r for r in res if r["day_alerts"] < med], f"アラート少ない日(<{med:.0f})")
print("  ※午後UNDER急増に限定:")
pm_us = [r for r in res if r["strategy"].startswith("UNDER") and r["time"] >= "13:00:00"]
rep([r for r in pm_us if r["day_alerts"] >= med], "  午後UNDER×アラート多い日")
rep([r for r in pm_us if r["day_alerts"] < med], "  午後UNDER×アラート少ない日")

print()
print("=" * 110)
print("【D】前場に売られた銘柄を後場寄り(12:30)で買い、大引けで決済")
print("=" * 110)
morning = defaultdict(int)
for a in alerts:
    if a["time"] < "11:00:00":
        morning[(a["symbol"], a["date"])] += 1
trades = []
for (sym, date), n in morning.items():
    b = bars.get((sym, date))
    if not b:
        continue
    pm_bars = [x for x in b if x[0].strftime("%H:%M") >= "12:30"]
    if len(pm_bars) < 3:
        continue
    entry = pm_bars[0][1]          # 後場寄りの始値
    close = pm_bars[-1][4]         # 大引け
    low = min(x[3] for x in pm_bars)
    high = max(x[2] for x in pm_bars)
    if not entry:
        continue
    t = {"sym": sym, "date": date, "n": n, "entry": entry,
         "rclose": (close - entry) / entry * 100,
         "mae": (low - entry) / entry * 100, "mfe": (high - entry) / entry * 100}
    # SL2/TP2
    val = None
    for x in pm_bars:
        if x[3] <= entry * 0.98: val = -2.0; break
        if x[2] >= entry * 1.02: val = 2.0; break
    t["sl2.0_tp2.0"] = val if val is not None else t["rclose"]
    t["sl1.0_tp1.0"] = None
    val = None
    for x in pm_bars:
        if x[3] <= entry * 0.99: val = -1.0; break
        if x[2] >= entry * 1.01: val = 1.0; break
    t["sl1.0_tp1.0"] = val if val is not None else t["rclose"]
    t["sl1.5_tp3.0"] = t["rclose"]
    trades.append(t)
rep(trades, "前場アラートあり→後場寄り買い")
for lo, hi, nm in ((1, 2, "前場1〜2件"), (3, 5, "前場3〜5件"), (6, 99, "前場6件以上")):
    rep([t for t in trades if lo <= t["n"] <= hi], f"  {nm}")
rep([t for t in trades if t["entry"] >= 500], "  500円以上に限定")

print()
print("=" * 110)
print("【E】UNDER急増の増加率別（午後・1日1回・500円以上）")
print("=" * 110)
first = {}
for r in sorted(us, key=lambda x: (x["date"], x["time"])):
    if r["time"] >= "13:00:00":
        first.setdefault((r["symbol"], r["date"]), r)
core = [r for r in first.values() if r["entry"] >= 500]
rep(core, "中核戦略（全体）")
for lo, hi, nm in ((20, 30, "+20〜30%"), (30, 50, "+30〜50%"), (50, 100, "+50〜100%"), (100, 1e9, "+100%〜")):
    rep([r for r in core if r["under_pct"] and lo <= float(r["under_pct"]) < hi], f"  急増率 {nm}")
print()
print("  ※中核戦略の時間帯内訳:")
rep([r for r in core if r["time"] < "14:00:00"], "  13:00-14:00")
rep([r for r in core if r["time"] >= "14:00:00"], "  14:00-")
