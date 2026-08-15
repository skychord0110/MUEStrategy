# -*- coding: utf-8 -*-
"""銘柄リストCSVのランキング・属性と、実際の売買成績を突き合わせる。

extracted_stocks/*_export.csv は R/Rスコア順に並んだ需給スクリーニング結果で、
空売り機関の買い戻し（Short Cover / Cover Insts）、下落率（12M DD）、
出来高急増（Vol Surge）、変動率（ATR）、モメンタム（20D Mom）を持つ。
これらが「その後の値動き」を予測できるかを検証する。

【先読みバイアスを避ける】
アラート日 D に対して、**D より前の日付のCSV**だけを使う。
CSVは場中（例 11:39）に生成されているため、同日のCSVは使わない。
  07-13〜07-24 のアラート → 07-12 のCSV
  07-25〜07-31           → 07-24 のCSV
  08-01〜08-10           → 07-31 のCSV
  08-11〜08-14           → 08-10 のCSV

入力: output/2026-08-14/eval_results_2026-08-14.json, extracted_stocks/*.csv
実行: python -X utf8 rank_strategy_2026-08-14.py
"""
import csv
import glob
import json
import os
import re
import statistics
from collections import defaultdict
from datetime import datetime, timezone, timedelta

BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(BASE, "output")
OUT5 = os.path.join(OUT, "2026-08-14")
CSV_DIR = os.path.normpath(os.path.join(BASE, "..", "extracted_stocks"))
JST = timezone(timedelta(hours=9))
COST = 0.15
WEEKS = ("week1", "week2", "week3", "week4", "week5")
KEY = "sl2.0_tp2.0"

# ── CSVを日付つきで読み込む ──
snapshots = []           # [(日付, {symbol: {...}}), ...] 古い順
for p in sorted(glob.glob(os.path.join(CSV_DIR, "*_export.csv"))):
    m = re.match(r"^(\d{4}-\d{2}-\d{2})T", os.path.basename(p))
    if not m:
        continue
    rows = list(csv.DictReader(open(p, encoding="utf-8-sig")))
    d = {}
    for i, r in enumerate(rows, start=1):
        code = (r.get("Ticker") or "").strip()
        if not code:
            continue

        def num(k):
            try:
                return float(r.get(k) or "")
            except ValueError:
                return None

        d[code] = {"rank": i, "score": num("R/R Score"), "atr": num("ATR (%)"),
                   "dd12": num("12M DD (%)"), "surge": num("Vol Surge (10/60)"),
                   "mom": num("20D Mom (%)"), "vol": num("60D Avg Vol"),
                   "mcap": num("MCap (¥100M)"),
                   "cover": bool((r.get("Short Cover") or "").strip()),
                   "insts": int(float(r.get("Cover Insts") or 0)),
                   "theme": (r.get("Theme") or "").strip()}
    snapshots.append((m.group(1), d))
print("読み込んだCSV:", ", ".join(f"{d}({len(s)}件)" for d, s in snapshots))


def attrs_for(symbol, date):
    """その日付より前で最も新しいCSVの属性を返す（無ければ None）。"""
    best = None
    for d, s in snapshots:
        if d < date and symbol in s:
            best = s[symbol]
    return best


res = json.load(open(os.path.join(OUT5, "eval_results_2026-08-14.json")))
joined, missing = [], 0
for r in res:
    a = attrs_for(r["symbol"], r["date"])
    if a is None:
        missing += 1
        continue
    joined.append(dict(r, **a))
print(f"突き合わせ {len(joined)}件 / 属性なし {missing}件\n")


def line(name, rows, key=KEY, min_n=15):
    if len(rows) < min_n:
        print(f"  {name:<30} n={len(rows):<5} 件数不足")
        return
    v = [r[key] for r in rows]
    print(f"  {name:<30} n={len(v):<5} 勝率{sum(1 for x in v if x > 0) / len(v) * 100:5.1f}%"
          f"  期待値{statistics.mean(v):+7.3f}%  コスト後{statistics.mean(v) - COST:+7.3f}%")


def weekly(rows, key=KEY):
    parts = []
    for w in WEEKS:
        sub = [r[key] for r in rows if r["week"] == w]
        parts.append(f"w{w[-1]}:{'—' if not sub else f'{statistics.mean(sub):+.2f}%'}({len(sub)})")
    print(f"      週別 {'  '.join(parts)}")


core = {}
for r in sorted([r for r in joined if r["strategy"] == "UNDER急増"
                 and r["time"] >= "13:00:00" and r["entry"] >= 500],
                key=lambda x: (x["date"], x["time"])):
    core.setdefault((r["symbol"], r["date"]), r)
core = list(core.values())
print(f"中核シグナル（午後UNDER急増・1日1回・500円以上）: {len(core)}件\n")

print("=" * 84)
print("A. ランキング順位別（全アラート n={}）".format(len(joined)))
print("=" * 84)
for lo, hi in ((1, 11), (11, 26), (26, 51), (51, 10 ** 9)):
    lab = f"{lo}〜{hi - 1}位" if hi < 10 ** 9 else f"{lo}位以下（圏外）"
    sub = [r for r in joined if lo <= r["rank"] < hi]
    line(lab, sub)
    if len(sub) >= 15:
        weekly(sub)

print()
print("=" * 84)
print("B. ランキング順位別（中核シグナルのみ n={}）".format(len(core)))
print("=" * 84)
for lo, hi in ((1, 11), (11, 26), (26, 51), (51, 10 ** 9)):
    lab = f"{lo}〜{hi - 1}位" if hi < 10 ** 9 else f"{lo}位以下"
    line(lab, [r for r in core if lo <= r["rank"] < hi], min_n=8)

print()
print("=" * 84)
print("C. CSVの需給属性別（全アラート）")
print("=" * 84)
print("\n-- 空売り機関の買い戻し --")
line("Short Cover あり", [r for r in joined if r["cover"]])
line("Short Cover なし", [r for r in joined if not r["cover"]])
for n in (1, 2, 3):
    lab = f"買い戻し機関 {n}社" if n < 3 else "買い戻し機関 3社以上"
    sub = [r for r in joined if (r["insts"] == n if n < 3 else r["insts"] >= 3)]
    line(lab, sub)

print("\n-- 変動率(ATR) --")
for lo, hi in ((0, 4), (4, 6), (6, 8), (8, 100)):
    line(f"ATR {lo}〜{hi}%", [r for r in joined if r["atr"] is not None and lo <= r["atr"] < hi])

print("\n-- 12ヶ月の下落率 --")
for lo, hi in ((-100, -70), (-70, -60), (-60, -50), (-50, 0)):
    line(f"12M DD {lo}〜{hi}%",
         [r for r in joined if r["dd12"] is not None and lo <= r["dd12"] < hi])

print("\n-- 出来高急増(10日/60日) --")
for lo, hi in ((0, 0.7), (0.7, 1.0), (1.0, 1.5), (1.5, 100)):
    line(f"Vol Surge {lo}〜{hi}",
         [r for r in joined if r["surge"] is not None and lo <= r["surge"] < hi])

print("\n-- 20日モメンタム --")
for lo, hi in ((-100, -20), (-20, -5), (-5, 5), (5, 100)):
    line(f"20D Mom {lo}〜{hi}%",
         [r for r in joined if r["mom"] is not None and lo <= r["mom"] < hi])

print()
print("=" * 84)
print("D. 効いた条件の組み合わせ（全アラート）と週別の安定性")
print("=" * 84)
combos = [
    ("ATR 6%以上", lambda r: r["atr"] is not None and r["atr"] >= 6),
    ("ATR 6%以上 ＋ 買い戻しあり", lambda r: r["atr"] is not None and r["atr"] >= 6 and r["cover"]),
    ("ATR 6%以上 ＋ 上位25位", lambda r: r["atr"] is not None and r["atr"] >= 6 and r["rank"] <= 25),
    ("下落率70%以上 ＋ 買い戻しあり",
     lambda r: r["dd12"] is not None and r["dd12"] <= -70 and r["cover"]),
]
for lab, f in combos:
    sub = [r for r in joined if f(r)]
    line(lab, sub)
    if len(sub) >= 15:
        weekly(sub)

print()
print("=" * 84)
print("E. 中核シグナルに属性フィルタを足す")
print("=" * 84)
line("フィルタなし（現行）", core, min_n=8)
weekly(core)
for lab, f in combos:
    sub = [r for r in core if f(r)]
    line(lab, sub, min_n=8)
    if len(sub) >= 8:
        weekly(sub)

json.dump(joined, open(os.path.join(OUT5, "rank_joined_2026-08-14.json"), "w"))
print("\nsaved rank_joined_2026-08-14.json")
