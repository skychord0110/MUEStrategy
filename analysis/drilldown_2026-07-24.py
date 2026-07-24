# -*- coding: utf-8 -*-
"""両週合算での午後UNDER急増の絞り込み条件を探索する。

入力: analysis/output/eval_results_2026-07-24.json
実行: python -X utf8 drilldown_2026-07-24.py
"""
import json
import os
import statistics
from collections import defaultdict

BASE = os.path.dirname(os.path.abspath(__file__))
results = json.load(open(os.path.join(BASE, "output", "eval_results_2026-07-24.json")))

def stat(rows, rule):
    vals = [r[rule] for r in rows]
    wins = sum(1 for v in vals if v > 0)
    return len(vals), wins / len(vals) * 100, statistics.mean(vals), statistics.median(vals)

def line(rows, name, rule="sl2.0_tp2.0"):
    if len(rows) < 5:
        print(f"  {name:<32} n={len(rows):<3} — サンプル不足")
        return
    n, w, a, m = stat(rows, rule)
    print(f"  {name:<32} n={n:<3} 勝率{w:>5.1f}% 期待値{a:+.3f}% 中央値{m:+.3f}%")

def pm(r): return r["time"] >= "13:00:00"

us_pm = [r for r in results if r["strategy"].startswith("UNDER") and pm(r)]
# 同一銘柄・同一日で最初の1件だけ（実運用に合わせる）
first = {}
for r in sorted(us_pm, key=lambda x: (x["date"], x["time"])):
    key = (r["symbol"], r["date"])
    if key not in first:
        first[key] = r
us_pm_first = list(first.values())

print("### 午後UNDER急増（SL2%/TP2%基準・全件 vs 銘柄1日1回） ###")
line(us_pm, "全件")
line(us_pm_first, "同一銘柄1日1回に限定")
print()

print("### 午後UNDER急増(1日1回) × 急増率バケット ###")
for lo, hi in ((20, 30), (30, 50), (50, 100), (100, 1e9)):
    rows = [r for r in us_pm_first if r["under_pct"] and lo <= float(r["under_pct"]) < hi]
    line(rows, f"+{lo}〜{hi if hi < 1e9 else '∞'}%")
print()

print("### 午後UNDER急増(1日1回) × 時間帯 ###")
line([r for r in us_pm_first if "13:00:00" <= r["time"] < "14:00:00"], "13:00-14:00")
line([r for r in us_pm_first if "14:00:00" <= r["time"] < "14:30:00"], "14:00-14:30")
line([r for r in us_pm_first if r["time"] >= "14:30:00"], "14:30-引け")
print()

print("### 午後UNDER急増(1日1回) × 決済ルール比較 ###")
for rule in ("rclose", "sl1.0_tp1.0", "sl1.5_tp3.0", "sl2.0_tp2.0", "sl1.5_tp2.0" if False else "sl2.0_tp2.0"):
    n, w, a, m = stat(us_pm_first, rule)
    print(f"  {rule:<14} n={n} 勝率{w:.1f}% 期待値{a:+.3f}% 中央値{m:+.3f}%")
print()

print("### 午後UNDER急増(1日1回) × 週別（頑健性） ###")
for wk in ("week1", "week2"):
    line([r for r in us_pm_first if r["week"] == wk], wk)
print()

print("### 価格帯別（低位株ほどノイズが大きいか） ###")
for lo, hi, name in ((0, 500, "〜500円"), (500, 1500, "500-1500円"), (1500, 1e9, "1500円〜")):
    rows = [r for r in us_pm_first if lo <= r["entry"] < hi]
    line(rows, name)
