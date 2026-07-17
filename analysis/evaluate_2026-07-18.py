# -*- coding: utf-8 -*-
"""アラートを買いシグナルとして各種決済ルールで検証し、勝率・期待値を集計する。

入力: analysis/output/alerts.csv, analysis/output/bars_5m.json
出力: analysis/output/eval_results.json ＋ コンソールに集計表
実行: python -X utf8 evaluate_2026-07-18.py

検証方法:
  - エントリー価格はアラート時の現在値（ログ記載の値）
  - 決済はアラート時刻より後の5分足で判定
  - r15/r30/r60: 15/30/60分後の足の終値で決済した場合のリターン(%)
  - rclose: 大引け（当日最終足の終値）で決済した場合のリターン(%)
  - slX_tpY: 損切り-X% / 利確+Y%、どちらも当たらなければ大引け決済
    （同一足で両方に到達した場合は損切り優先の保守的処理）
  - MFE/MAE: エントリー後の最大含み益/最大含み損(%)
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

alerts = list(csv.DictReader(open(os.path.join(OUT_DIR, "alerts.csv"), encoding="utf-8")))
bars_all = json.load(open(os.path.join(OUT_DIR, "bars_5m.json")))

# 銘柄ごとに日付別のバー配列を作る
sym_day_bars = defaultdict(list)  # (sym, date) -> [(dt, o, h, l, c, v)]
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
    # アラート時刻より後に始まるバー（エントリーはアラート価格、検証は後続バーで行う）
    after = [b for b in bars if b[0] > at]
    if len(after) < 2:
        return None  # 引け間際すぎて検証不能
    res = {"entry": entry, "n_after": len(after)}
    # ホライズン別リターン（後続バーの終値）
    for label, nbars in (("r15", 3), ("r30", 6), ("r60", 12)):
        idx = min(nbars, len(after)) - 1
        res[label] = (after[idx][4] - entry) / entry * 100
    res["rclose"] = (after[-1][4] - entry) / entry * 100  # 大引け決済
    res["mfe"] = (max(b[2] for b in after) - entry) / entry * 100
    res["mae"] = (min(b[3] for b in after) - entry) / entry * 100
    # 損切り/利確ルール: stop%下で損切り、tp%上で利確、どちらも来なければ大引け
    for sl, tp in ((1.0, 1.0), (1.0, 2.0), (1.5, 3.0), (2.0, 2.0)):
        stop_p = entry * (1 - sl / 100)
        tp_p = entry * (1 + tp / 100)
        ret = None
        for b in after:
            if b[3] <= stop_p:  # 同一バー両到達は損切り優先（保守的）
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
    r.update({"strategy": a["strategy"], "symbol": a["symbol"], "date": a["date"],
              "time": a["time"], "under_pct": a["under_pct"], "consecutive": a["consecutive"]})
    results.append(r)

print(f"evaluated {len(results)} / {len(alerts)} alerts\n")

RULES = ["r15", "r30", "r60", "rclose", "sl1.0_tp1.0", "sl1.0_tp2.0", "sl1.5_tp3.0", "sl2.0_tp2.0"]

def summarize(rows, name):
    if len(rows) < 5:
        return
    print(f"== {name} (n={len(rows)}) ==")
    print(f"{'rule':<14}{'win%':>7}{'avg%':>8}{'med%':>8}{'p25':>7}{'p75':>7}")
    for rule in RULES:
        vals = [r[rule] for r in rows]
        wins = sum(1 for v in vals if v > 0)
        qs = statistics.quantiles(vals, n=4)
        print(f"{rule:<14}{wins/len(vals)*100:>6.1f}%{statistics.mean(vals):>7.3f}%{statistics.median(vals):>7.3f}%{qs[0]:>6.2f}%{qs[2]:>6.2f}%")
    mfe = [r["mfe"] for r in rows]; mae = [r["mae"] for r in rows]
    print(f"MFE avg {statistics.mean(mfe):.2f}% / MAE avg {statistics.mean(mae):.2f}%")
    print()

# 全体・ストラテジー別
summarize(results, "ALL")
for strat in sorted(set(r["strategy"] for r in results)):
    summarize([r for r in results if r["strategy"] == strat], strat)

# UNDER急増: 急増率の大きさ別
us = [r for r in results if r["strategy"].startswith("UNDER")]
for lo, hi in ((20, 40), (40, 80), (80, 10000)):
    summarize([r for r in us if r["under_pct"] and lo <= float(r["under_pct"]) < hi],
              f"UNDER急増 +{lo}%〜{hi}%")

# 時間帯別（全体）
def hourband(t):
    h = int(t[:2]); m = int(t[3:5])
    if h == 9 and m < 30: return "09:00-09:30"
    if h < 10: return "09:30-10:00"
    if h < 11: return "10:00-11:00"
    if h < 13: return "11:00-13:00"
    if h < 14: return "13:00-14:00"
    return "14:00-"
for band in ["09:00-09:30", "09:30-10:00", "10:00-11:00", "11:00-13:00", "13:00-14:00", "14:00-"]:
    summarize([r for r in results if hourband(r["time"]) == band], f"ALL {band}")

# 同一銘柄・同一日の「N回目以降のアラート」= シグナル持続性
seen = defaultdict(int)
for r in sorted(results, key=lambda x: (x["date"], x["time"])):
    seen[(r["symbol"], r["date"])] += 1
    r["nth"] = seen[(r["symbol"], r["date"])]
summarize([r for r in results if r["nth"] == 1], "ALL 当日初回アラートのみ")
summarize([r for r in results if r["nth"] >= 3], "ALL 当日3回目以降")

json.dump(results, open(os.path.join(OUT_DIR, "eval_results.json"), "w"))
print("saved eval_results.json")
