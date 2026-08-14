# -*- coding: utf-8 -*-
"""5週間ぶんの検証結果を掘り下げ、追加の需給戦略候補を検証する。

入力: output/2026-08-14/eval_results_2026-08-14.json
      output/2026-08-14/alerts_2026-08-14.csv
      bars（翌日持ち越しの検証に使う）
実行: python -X utf8 drilldown_2026-08-14.py

【先読みバイアスを避ける】
フィルタに使ってよいのは、シグナル時点で分かる情報だけ:
  時刻 / 価格 / UNDER急増率 / 連続回数 / その日それまでに出たアラート / 銘柄
「その日の後半に何が起きたか」を条件に使わない。
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

# 翌日持ち越し検証用の日足（5分足から日次のOHLCを作る）
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

daily = defaultdict(dict)          # symbol -> date -> (open, high, low, close)
for (sym, d), b in bars.items():
    daily[sym][d] = (b[0][1], max(x[2] for x in b), min(x[3] for x in b), b[-1][4])
for sym in daily:
    daily[sym] = dict(sorted(daily[sym].items()))


def stat(rows, key):
    v = [r[key] for r in rows]
    return (len(v), sum(1 for a in v if a > 0) / len(v) * 100,
            statistics.mean(v), statistics.mean(v) - COST)


def line(name, rows, key="sl2.0_tp2.0", min_n=8):
    if len(rows) < min_n:
        print(f"  {name:<34} n={len(rows):<4} 件数不足")
        return
    n, win, exp, net = stat(rows, key)
    print(f"  {name:<34} n={n:<4} 勝率{win:5.1f}%  期待値{exp:+7.3f}%  コスト後{net:+7.3f}%")


def weekly(name, rows, key="sl2.0_tp2.0"):
    parts = []
    for w in WEEKS:
        sub = [r for r in rows if r["week"] == w]
        parts.append(f"{w[-1]}:{'—' if not sub else f'{statistics.mean([x[key] for x in sub]):+.2f}%'}"
                     f"({len(sub)})")
    print(f"    週別 {' '.join(parts)}")


def once_per_symbol_day(rows):
    """同一銘柄・同一日の最初の1件だけ残す（実運用のルールに合わせる）。"""
    first = {}
    for r in sorted(rows, key=lambda x: (x["date"], x["time"])):
        first.setdefault((r["symbol"], r["date"]), r)
    return list(first.values())


print("=" * 78)
print("A. 中核戦略の再確認と、時間帯の絞り込み（午後UNDER急増・1日1回・500円以上）")
print("=" * 78)
core = once_per_symbol_day(
    [r for r in res if r["strategy"] == "UNDER急増" and r["time"] >= "13:00:00"])
core = [r for r in core if r["entry"] >= 500]
line("13:00-15:30（現行）", core)
weekly("", core)
for lo, hi, lab in (("13:00:00", "14:00:00", "13:00-14:00"),
                    ("14:00:00", "15:00:00", "14:00-15:00"),
                    ("15:00:00", "23:59:59", "15:00-")):
    line(lab, [r for r in core if lo <= r["time"] < hi])
print()
line("13:00-15:00（現行のエントリー窓）", [r for r in core if r["time"] < "15:00:00"])
weekly("", [r for r in core if r["time"] < "15:00:00"])

print()
print("=" * 78)
print("B. UNDER急増の強度別（シグナル時点で分かる情報）")
print("=" * 78)
for lo, hi in ((20, 30), (30, 50), (50, 100), (100, 10 ** 9)):
    sub = [r for r in core if r["under_pct"] and lo <= float(r["under_pct"]) < hi]
    line(f"UNDER +{lo}〜{hi if hi < 10**9 else '∞'}%", sub)

print()
print("=" * 78)
print("C. 価格帯別（500円以上の中でさらに絞る）")
print("=" * 78)
for lo, hi, lab in ((500, 1000, "500〜1000円"), (1000, 2000, "1000〜2000円"),
                    (2000, 10 ** 9, "2000円〜")):
    line(lab, [r for r in core if lo <= r["entry"] < hi])

print()
print("=" * 78)
print("D. 投げ売り検知（セリングクライマックス）— 同一銘柄1日1回に整理")
print("=" * 78)
panic_all = [r for r in res if r["strategy"].startswith("投げ売り")]
panic = once_per_symbol_day(panic_all)
print(f"  重複除去前 n={len(panic_all)} → 除去後 n={len(panic)}")
for key in ("sl1.0_tp1.0", "sl1.5_tp3.0", "sl2.0_tp2.0", "rclose"):
    line(f"{key}", panic, key=key, min_n=5)
weekly("", panic, "sl1.0_tp1.0")
line("うち午後(13時〜)", [r for r in panic if r["time"] >= "13:00:00"], "sl1.0_tp1.0", min_n=5)
line("うち午前", [r for r in panic if r["time"] < "13:00:00"], "sl1.0_tp1.0", min_n=5)

print()
print("=" * 78)
print("E. 複合: 午後のUNDER急増で、その日それ以前に小口売り連続が出ていた銘柄")
print("=" * 78)
# 「その日それ以前」なので先読みにならない
small_by_day = defaultdict(list)
for a in alerts:
    if a["strategy"].startswith("小口売り連続"):
        small_by_day[(a["symbol"], a["date"])].append(a["time"])
with_small, without_small = [], []
for r in core:
    prior = [t for t in small_by_day.get((r["symbol"], r["date"]), []) if t < r["time"]]
    (with_small if prior else without_small).append(r)
line("小口売り連続あり", with_small)
weekly("", with_small)
line("小口売り連続なし", without_small)
weekly("", without_small)

print()
print("=" * 78)
print("F. 同一銘柄・同日の2回目以降のUNDER急増（1回目と比べて）")
print("=" * 78)
us_pm = [r for r in res if r["strategy"] == "UNDER急増" and r["time"] >= "13:00:00"
         and r["entry"] >= 500]
seq = defaultdict(list)
for r in sorted(us_pm, key=lambda x: (x["date"], x["time"])):
    seq[(r["symbol"], r["date"])].append(r)
first_ = [v[0] for v in seq.values()]
later = [x for v in seq.values() for x in v[1:]]
line("1回目（現行ルール）", first_)
line("2回目以降", later)

print()
print("=" * 78)
print("G. 翌日への持ち越し（中核シグナルを大引けで手仕舞わず翌日へ）")
print("=" * 78)


def next_day_return(r, mode):
    ds = daily.get(r["symbol"])
    if not ds:
        return None
    dates = list(ds)
    if r["date"] not in dates:
        return None
    i = dates.index(r["date"])
    if i + 1 >= len(dates):
        return None
    o, h, l, c = ds[dates[i + 1]]
    entry = r["entry"]
    return (o - entry) / entry * 100 if mode == "open" else (c - entry) / entry * 100


for mode, lab in (("open", "翌日の寄り"), ("close", "翌日の大引け")):
    vals = [(r, next_day_return(r, mode)) for r in core]
    vals = [(r, v) for r, v in vals if v is not None]
    if len(vals) < 8:
        print(f"  {lab:<34} n={len(vals)} 件数不足")
        continue
    v = [x[1] for x in vals]
    print(f"  {lab:<34} n={len(v):<4} 勝率{sum(1 for a in v if a > 0) / len(v) * 100:5.1f}%"
          f"  期待値{statistics.mean(v):+7.3f}%  コスト後{statistics.mean(v) - COST:+7.3f}%")

print()
print("=" * 78)
print("H. week5（今週）の中核シグナル明細")
print("=" * 78)
for r in sorted([r for r in core if r["week"] == "week5"], key=lambda x: (x["date"], x["time"])):
    print(f"  {r['date']} {r['time']} {r['symbol']} @{r['entry']:>7.1f}円 "
          f"UNDER+{float(r['under_pct'] or 0):5.1f}%  "
          f"SL/TP{r['sl2.0_tp2.0']:+6.2f}%  大引け{r['rclose']:+6.2f}%  "
          f"最大益{r['mfe']:+6.2f}% 最大損{r['mae']:+6.2f}%")
