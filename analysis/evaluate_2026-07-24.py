# -*- coding: utf-8 -*-
"""アラートを買いシグナルとして決済ルール別に検証（両週対応）。

入力:
  analysis/output/alerts_2026-07-24.csv  （両週の全アラート）
  analysis/output/bars_5m.json           （week1: 07-13〜17 の5分足）
  analysis/output/bars_5m_week2.json      （week2: 07-21〜24 の5分足）
出力: analysis/output/eval_results_2026-07-24.json ＋ コンソール集計表
実行: python -X utf8 evaluate_2026-07-24.py

検証方法は前回(evaluate_2026-07-18.py)と同一:
  - エントリー=アラート時の現在値、決済はアラート時刻より後の5分足で判定
  - rclose=大引け決済、slX_tpY=損切り-X%/利確+Y%（未達は大引け、同一足両到達は損切り優先）
"""
import csv
import json
import os
import statistics
from datetime import datetime, timezone, timedelta
from collections import defaultdict

BASE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(BASE, "output")
JST = timezone(timedelta(hours=9))

alerts = list(csv.DictReader(open(os.path.join(OUT_DIR, "alerts_2026-07-24.csv"), encoding="utf-8")))

# 5分足を (symbol, date) -> [(dt,o,h,l,c,v)] に展開（両週のJSONを結合）
sym_day_bars = defaultdict(list)
for fname in ("bars_5m.json", "bars_5m_week2.json"):
    path = os.path.join(OUT_DIR, fname)
    if not os.path.exists(path):
        continue
    bars_all = json.load(open(path))
    for sym, d in bars_all.items():
        for ts, o, h, l, c, v in zip(d["ts"], d["open"], d["high"], d["low"], d["close"], d["volume"]):
            if o is None or h is None or l is None or c is None:
                continue
            dt = datetime.fromtimestamp(ts, JST)
            sym_day_bars[(sym, dt.strftime("%Y-%m-%d"))].append((dt, o, h, l, c, v))
for k in sym_day_bars:
    sym_day_bars[k].sort()

def evaluate(alert):
    sym, date = alert["symbol"], alert["date"]
    at = datetime.strptime(f"{date} {alert['time']}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=JST)
    if not alert["price"]:
        return None
    entry = float(alert["price"])
    bars = sym_day_bars.get((sym, date))
    if not bars:
        return None
    after = [b for b in bars if b[0] > at]
    if len(after) < 2:
        return None
    res = {"entry": entry, "n_after": len(after)}
    for label, nbars in (("r15", 3), ("r30", 6), ("r60", 12)):
        idx = min(nbars, len(after)) - 1
        res[label] = (after[idx][4] - entry) / entry * 100
    res["rclose"] = (after[-1][4] - entry) / entry * 100
    res["mfe"] = (max(b[2] for b in after) - entry) / entry * 100
    res["mae"] = (min(b[3] for b in after) - entry) / entry * 100
    for sl, tp in ((1.0, 1.0), (1.0, 2.0), (1.5, 3.0), (2.0, 2.0)):
        stop_p = entry * (1 - sl / 100)
        tp_p = entry * (1 + tp / 100)
        ret = None
        for b in after:
            if b[3] <= stop_p:
                ret = -sl
                break
            if b[2] >= tp_p:
                ret = tp
                break
        if ret is None:
            ret = (after[-1][4] - entry) / entry * 100
        res[f"sl{sl}_tp{tp}"] = ret
    return res

results = []
for a in alerts:
    r = evaluate(a)
    if r is None:
        continue
    r.update({"week": a["week"], "strategy": a["strategy"], "symbol": a["symbol"],
              "date": a["date"], "time": a["time"], "under_pct": a["under_pct"],
              "consecutive": a["consecutive"]})
    results.append(r)

print(f"evaluated {len(results)} / {len(alerts)} alerts\n")

RULES = ["r15", "r30", "r60", "rclose", "sl1.0_tp1.0", "sl1.5_tp3.0", "sl2.0_tp2.0"]

def summarize(rows, name):
    if len(rows) < 5:
        print(f"== {name} (n={len(rows)}) — サンプル不足\n")
        return
    print(f"== {name} (n={len(rows)}) ==")
    print(f"{'rule':<14}{'win%':>7}{'avg%':>8}{'med%':>8}")
    for rule in RULES:
        vals = [r[rule] for r in rows]
        wins = sum(1 for v in vals if v > 0)
        print(f"{rule:<14}{wins/len(vals)*100:>6.1f}%{statistics.mean(vals):>7.3f}%{statistics.median(vals):>7.3f}%")
    print()

def pm(r):
    return r["time"] >= "13:00:00"

# 全体（両週）とストラテジー別
summarize(results, "ALL 両週")
for strat in sorted(set(r["strategy"] for r in results)):
    summarize([r for r in results if r["strategy"] == strat], f"両週 {strat}")

# 週別
for wk in ("week1", "week2"):
    summarize([r for r in results if r["week"] == wk], f"{wk} ALL")

# 午後のUNDER急増（提案戦略の中核）を週別に
print("### 午後(13時以降)のUNDER急増 ###")
for wk in ("week1", "week2", None):
    rows = [r for r in results if r["strategy"].startswith("UNDER") and pm(r)
            and (wk is None or r["week"] == wk)]
    summarize(rows, f"午後UNDER急増 {wk or '両週合算'}")

# 時間帯別（両週合算）
def hourband(t):
    h = int(t[:2]); m = int(t[3:5])
    if h == 9 and m < 30: return "09:00-09:30"
    if h < 10: return "09:30-10:00"
    if h < 11: return "10:00-11:00"
    if h < 13: return "11:00-13:00"
    if h < 14: return "13:00-14:00"
    return "14:00-"
print("### 時間帯別（両週合算・全ストラテジー） ###")
for band in ["09:00-09:30", "09:30-10:00", "10:00-11:00", "11:00-13:00", "13:00-14:00", "14:00-"]:
    summarize([r for r in results if hourband(r["time"]) == band], f"ALL {band}")

json.dump(results, open(os.path.join(OUT_DIR, "eval_results_2026-07-24.json"), "w"))
print("saved eval_results_2026-07-24.json")
