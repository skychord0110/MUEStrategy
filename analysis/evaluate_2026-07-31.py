# -*- coding: utf-8 -*-
"""3週間（2026-07-13〜07-31）の全アラートを決済ルール別に検証する。

入力: output/alerts_2026-07-31.csv, bars_5m*.json（week1/2/3）
出力: output/eval_results_2026-07-31.json ＋ コンソール集計
実行: python -X utf8 evaluate_2026-07-31.py

決済ルール: rN=N分後の終値 / rclose=大引け /
           slX_tpY=損切り-X%・利確+Y%（未達は大引け、同一足両到達は損切り優先）
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

RULES = ["r15", "r30", "r60", "rclose", "sl1.0_tp1.0", "sl1.5_tp3.0", "sl2.0_tp2.0"]


def evaluate(a):
    if not a["price"]:
        return None
    entry = float(a["price"])
    at = datetime.strptime(f"{a['date']} {a['time']}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=JST)
    b = bars.get((a["symbol"], a["date"]))
    if not b:
        return None
    after = [x for x in b if x[0] > at]
    if len(after) < 2:
        return None
    r = {"entry": entry}
    for lab, n in (("r15", 3), ("r30", 6), ("r60", 12)):
        r[lab] = (after[min(n, len(after)) - 1][4] - entry) / entry * 100
    r["rclose"] = (after[-1][4] - entry) / entry * 100
    r["mfe"] = (max(x[2] for x in after) - entry) / entry * 100
    r["mae"] = (min(x[3] for x in after) - entry) / entry * 100
    for sl, tp in ((1.0, 1.0), (1.5, 3.0), (2.0, 2.0)):
        stop, take = entry * (1 - sl / 100), entry * (1 + tp / 100)
        val = None
        for x in after:
            if x[3] <= stop:
                val = -sl; break
            if x[2] >= take:
                val = tp; break
        r[f"sl{sl}_tp{tp}"] = val if val is not None else (after[-1][4] - entry) / entry * 100
    return r


res = []
for a in alerts:
    e = evaluate(a)
    if e:
        e.update({k: a[k] for k in ("week", "date", "time", "strategy", "symbol",
                                    "under_pct", "consecutive")})
        res.append(e)
print(f"検証 {len(res)} / {len(alerts)} 件\n")


def rep(rows, name, rules=RULES, min_n=5):
    if len(rows) < min_n:
        print(f"== {name} (n={len(rows)}) 不足\n"); return
    print(f"== {name} (n={len(rows)}) ==")
    print(f"{'rule':<14}{'勝率':>7}{'期待値':>9}{'中央値':>9}")
    for r in rules:
        v = [x[r] for x in rows]
        print(f"{r:<14}{sum(1 for a in v if a>0)/len(v)*100:>6.1f}%{statistics.mean(v):>8.3f}%{statistics.median(v):>8.3f}%")
    print()


def pm(r):
    return r["time"] >= "13:00:00"


rep(res, "ALL 3週間")
for s in sorted(set(r["strategy"] for r in res)):
    rep([r for r in res if r["strategy"] == s], f"[種別] {s}")
for w in ("week1", "week2", "week3"):
    rep([r for r in res if r["week"] == w], f"[週別] {w}")

print("### 中核: 午後(13時〜)のUNDER急増・銘柄1日1回・500円以上 ###")
us = [r for r in res if r["strategy"].startswith("UNDER") and pm(r)]
first = {}
for r in sorted(us, key=lambda x: (x["date"], x["time"])):
    first.setdefault((r["symbol"], r["date"]), r)
f1 = list(first.values())
f5 = [r for r in f1 if r["entry"] >= 500]
rep(f1, "午後UNDER急増・1日1回（価格フィルタなし）")
rep(f5, "午後UNDER急増・1日1回・500円以上")
for w in ("week1", "week2", "week3"):
    rep([r for r in f5 if r["week"] == w], f"  └ {w}")

print("### 時間帯別（3週間・全種別） ###")
def band(t):
    h, m = int(t[:2]), int(t[3:5])
    if h == 9 and m < 30: return "09:00-09:30"
    if h < 10: return "09:30-10:00"
    if h < 11: return "10:00-11:00"
    if h < 13: return "11:00-13:00"
    if h < 14: return "13:00-14:00"
    return "14:00-"
for b in ["09:00-09:30", "09:30-10:00", "10:00-11:00", "11:00-13:00", "13:00-14:00", "14:00-"]:
    rep([r for r in res if band(r["time"]) == b], f"ALL {b}", rules=["rclose", "sl2.0_tp2.0"])

json.dump(res, open(os.path.join(OUT, "eval_results_2026-07-31.json"), "w"))
print("saved eval_results_2026-07-31.json")
