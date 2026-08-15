# -*- coding: utf-8 -*-
"""rank_strategy の結果が本物か、過剰適合かを検証する。

見つかった候補:
  (1) ランキング上位ほど成績が良い（全アラートで単調・中核でも単調）
  (2) Short Cover（空売り機関の買い戻し）あり が良い
  (3) ATR6%以上の中核シグナルが勝率100%（n=22）← 怪しい

検証の観点:
  ・決済ルールを変えても同じ傾向が出るか（出るなら本物、消えるならノイズ）
  ・週をまたいで安定しているか
  ・他の要因（価格・流動性）の言い換えになっていないか

実行: python -X utf8 rank_verify_2026-08-14.py
"""
import json
import os
import statistics
from collections import defaultdict

BASE = os.path.dirname(os.path.abspath(__file__))
OUT5 = os.path.join(BASE, "output", "2026-08-14")
COST = 0.15
WEEKS = ("week1", "week2", "week3", "week4", "week5")
RULES = ("sl2.0_tp2.0", "sl1.0_tp1.0", "sl1.5_tp3.0", "rclose", "r60")

joined = json.load(open(os.path.join(OUT5, "rank_joined_2026-08-14.json")))
core = {}
for r in sorted([r for r in joined if r["strategy"] == "UNDER急増"
                 and r["time"] >= "13:00:00" and r["entry"] >= 500],
                key=lambda x: (x["date"], x["time"])):
    core.setdefault((r["symbol"], r["date"]), r)
core = list(core.values())


def row(label, rows, rules=RULES):
    if not rows:
        print(f"  {label:<24} —")
        return
    cells = []
    for k in rules:
        v = [r[k] for r in rows]
        cells.append(f"{sum(1 for x in v if x > 0) / len(v) * 100:5.1f}%/{statistics.mean(v):+6.3f}%")
    print(f"  {label:<24} n={len(rows):<4} " + "  ".join(cells))


def header(rules=RULES):
    print(f"  {'':<24} {'':<7} " + "  ".join(f"{k:<13}" for k in rules))


print("=" * 100)
print("① ランキング順位の効果は、決済ルールを変えても残るか（勝率/期待値）")
print("=" * 100)
print("\n【全アラート】")
header()
for lo, hi, lab in ((1, 11, "1〜10位"), (11, 26, "11〜25位"), (26, 51, "26〜50位")):
    row(lab, [r for r in joined if lo <= r["rank"] < hi])
print("\n【中核シグナル】")
header()
for lo, hi, lab in ((1, 26, "1〜25位"), (26, 51, "26〜50位")):
    row(lab, [r for r in core if lo <= r["rank"] < hi])

print()
print("=" * 100)
print("② 中核シグナル×ランキングの週別安定性（SL2/TP2）")
print("=" * 100)
for lo, hi, lab in ((1, 26, "1〜25位"), (26, 51, "26〜50位")):
    sub = [r for r in core if lo <= r["rank"] < hi]
    parts = []
    for w in WEEKS:
        s = [r["sl2.0_tp2.0"] for r in sub if r["week"] == w]
        parts.append(f"w{w[-1]}:{'—' if not s else f'{statistics.mean(s):+.2f}%'}({len(s)})")
    v = [r["sl2.0_tp2.0"] for r in sub]
    print(f"  {lab:<10} n={len(v):<4} 勝率{sum(1 for x in v if x > 0) / len(v) * 100:5.1f}%"
          f" 期待値{statistics.mean(v):+.3f}%  |  {'  '.join(parts)}")

print()
print("=" * 100)
print("③ ATR6%以上の「勝率100%」は本物か")
print("=" * 100)
print("\n全アラートでのATR別（単調なら本物、バラバラならノイズ）")
header()
for lo, hi in ((0, 4), (4, 6), (6, 8), (8, 100)):
    row(f"ATR {lo}〜{hi}%", [r for r in joined if r["atr"] is not None and lo <= r["atr"] < hi])
print("\n中核シグナルでのATR別")
header()
for lo, hi in ((0, 6), (6, 100)):
    row(f"ATR {lo}〜{hi}%", [r for r in core if r["atr"] is not None and lo <= r["atr"] < hi])
hi_atr = [r for r in core if r["atr"] is not None and r["atr"] >= 6]
print(f"\n  ATR6%以上の中核シグナル {len(hi_atr)}件の内訳:")
for r in sorted(hi_atr, key=lambda x: (x["date"], x["time"])):
    print(f"    {r['date']} {r['symbol']} ATR{r['atr']:>5.1f}% 順位{r['rank']:>2}  "
          f"SL/TP{r['sl2.0_tp2.0']:+6.2f}%  最大損{r['mae']:+6.2f}%")
mae = [r["mae"] for r in hi_atr]
print(f"  最大損の中央値 {statistics.median(mae):+.2f}% / 最悪 {min(mae):+.2f}%"
      f" → -2%に触れた件数 {sum(1 for m in mae if m <= -2)}件")

print()
print("=" * 100)
print("④ ランキングは他の要因の言い換えになっていないか（中核シグナル）")
print("=" * 100)
for lo, hi, lab in ((1, 26, "1〜25位"), (26, 51, "26〜50位")):
    sub = [r for r in core if lo <= r["rank"] < hi]
    def med(k):
        v = [r[k] for r in sub if r.get(k) is not None]
        return statistics.median(v) if v else float("nan")
    print(f"  {lab}: n={len(sub)}  価格中央値{med('entry'):>7.0f}円  "
          f"ATR中央値{med('atr'):>5.1f}%  出来高中央値{med('vol'):>9,.0f}株  "
          f"買い戻しあり{sum(1 for r in sub if r['cover']) / len(sub) * 100:4.0f}%  "
          f"時価総額中央値{med('mcap'):>6.0f}億")

print()
print("=" * 100)
print("⑤ Short Cover の効果は決済ルールをまたいで残るか")
print("=" * 100)
print("\n【全アラート】")
header()
row("買い戻しあり", [r for r in joined if r["cover"]])
row("買い戻しなし", [r for r in joined if not r["cover"]])
parts = []
for w in WEEKS:
    a = [r["sl2.0_tp2.0"] for r in joined if r["cover"] and r["week"] == w]
    b = [r["sl2.0_tp2.0"] for r in joined if not r["cover"] and r["week"] == w]
    parts.append(f"w{w[-1]}:{statistics.mean(a) - statistics.mean(b):+.2f}%")
print(f"\n  週別の差（あり − なし）: {'  '.join(parts)}")
print("\n【中核シグナル】")
header()
row("買い戻しあり", [r for r in core if r["cover"]])
row("買い戻しなし", [r for r in core if not r["cover"]])
