# -*- coding: utf-8 -*-
"""有望条件の深掘り: 午後シグナル・日別頑健性・複合シグナル。

入力: analysis/output/eval_results.json（evaluate_2026-07-18.py の出力）
実行: python -X utf8 drilldown_2026-07-18.py
"""
import json
import os
import statistics
from collections import defaultdict

BASE = os.path.dirname(os.path.abspath(__file__))
results = json.load(open(os.path.join(BASE, "output", "eval_results.json")))

RULES = ["r30", "r60", "rclose", "sl1.0_tp1.0", "sl1.5_tp3.0", "sl2.0_tp2.0"]

def summarize(rows, name, min_n=5):
    if len(rows) < min_n:
        print(f"== {name} (n={len(rows)}) — サンプル不足\n")
        return
    print(f"== {name} (n={len(rows)}) ==")
    print(f"{'rule':<14}{'win%':>7}{'avg%':>8}{'med%':>8}")
    for rule in RULES:
        vals = [r[rule] for r in rows]
        wins = sum(1 for v in vals if v > 0)
        print(f"{rule:<14}{wins/len(vals)*100:>6.1f}%{statistics.mean(vals):>7.3f}%{statistics.median(vals):>7.3f}%")
    print()

def pm(r):  # 午後(13:00以降)
    return r["time"] >= "13:00:00"

# 1) 午後×ストラテジー別
for strat in sorted(set(r["strategy"] for r in results)):
    summarize([r for r in results if r["strategy"] == strat and pm(r)], f"午後 {strat}")

# 2) 午後シグナルの日別頑健性（rclose）
pm_rows = [r for r in results if pm(r)]
print("== 午後シグナル 日別 (rclose / sl1.0_tp1.0) ==")
for d in sorted(set(r["date"] for r in pm_rows)):
    rows = [r for r in pm_rows if r["date"] == d]
    rc = [r["rclose"] for r in rows]
    sl = [r["sl1.0_tp1.0"] for r in rows]
    print(f"{d}: n={len(rows):3d}  rclose avg {statistics.mean(rc):+.3f}% win {sum(1 for v in rc if v>0)/len(rc)*100:.0f}%"
          f" | sl1tp1 avg {statistics.mean(sl):+.3f}% win {sum(1 for v in sl if v>0)/len(sl)*100:.0f}%")
print()

# 3) 午後シグナルの銘柄集中度
print("== 午後シグナル 銘柄別件数上位 ==")
cnt = defaultdict(list)
for r in pm_rows:
    cnt[r["symbol"]].append(r["rclose"])
for sym, vals in sorted(cnt.items(), key=lambda kv: -len(kv[1]))[:12]:
    print(f"{sym}: n={len(vals):3d} rclose avg {statistics.mean(vals):+.3f}%")
print()

# 4) 複合シグナル: 同一銘柄・同一日に30分以内で異なるストラテジーが両方点灯
def tosec(t):
    h, m, s = map(int, t.split(":"))
    return h * 3600 + m * 60 + s

by_symday = defaultdict(list)
for r in results:
    by_symday[(r["symbol"], r["date"])].append(r)

confluence = []
for key, rows in by_symday.items():
    rows.sort(key=lambda r: r["time"])
    for i, r in enumerate(rows):
        kinds = {r["strategy"].split("/")[0]}
        for r2 in rows[:i]:
            if tosec(r["time"]) - tosec(r2["time"]) <= 1800:
                kinds.add(r2["strategy"].split("/")[0])
        if len(kinds) >= 2:
            confluence.append(r)
summarize(confluence, "複合シグナル(30分以内に2種以上点灯した時点のアラート)")
summarize([r for r in confluence if pm(r)], "複合シグナル×午後")

# 5) 午後×UNDER急増の増加率別
pmu = [r for r in pm_rows if r["strategy"].startswith("UNDER")]
for lo, hi in ((20, 50), (50, 10000)):
    summarize([r for r in pmu if r["under_pct"] and lo <= float(r["under_pct"]) < hi],
              f"午後 UNDER急増 +{lo}〜{hi}%")

# 6) 14時以降に絞った場合の日別
late = [r for r in results if r["time"] >= "14:00:00"]
print("== 14:00以降 日別 (rclose) ==")
for d in sorted(set(r["date"] for r in late)):
    rows = [r for r in late if r["date"] == d]
    rc = [r["rclose"] for r in rows]
    print(f"{d}: n={len(rows):3d} avg {statistics.mean(rc):+.3f}% win {sum(1 for v in rc if v>0)/len(rc)*100:.0f}%")
print()

# 7) 前場の逆張り回避チェック: 9:30-10:00のシグナルを「売り」とみなした場合
neg = [r for r in results if "09:30:00" <= r["time"] < "10:00:00"]
if neg:
    rc = [-r["rclose"] for r in neg]
    print(f"== 参考: 9:30-10:00を空売りした場合の大引けリターン n={len(neg)} avg {statistics.mean(rc):+.3f}% win {sum(1 for v in rc if v>0)/len(rc)*100:.0f}% ==")
