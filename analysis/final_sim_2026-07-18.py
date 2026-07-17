# -*- coding: utf-8 -*-
"""実運用想定シミュレーション: 午後シグナル・同一銘柄1日1回・コスト控除後。

入力: analysis/output/eval_results.json（evaluate_2026-07-18.py の出力）
実行: python -X utf8 final_sim_2026-07-18.py
"""
import json
import os
import statistics
from collections import defaultdict

BASE = os.path.dirname(os.path.abspath(__file__))
results = json.load(open(os.path.join(BASE, "output", "eval_results.json")))

def tick_size(p):
    # 東証 呼値(TOPIX500構成銘柄以外): 〜1000円:1円, 〜3000円:5円 に簡略化（保守的）
    if p <= 1000: return 1.0
    if p <= 3000: return 5.0
    return 10.0

def simulate(rows, name, rule, cost_ticks=1.0):
    """rows: 対象アラート。同一(銘柄,日)は最初の1件のみ採用"""
    taken = {}
    for r in sorted(rows, key=lambda x: (x["date"], x["time"])):
        key = (r["symbol"], r["date"])
        if key not in taken:
            taken[key] = r
    trades = list(taken.values())
    if not trades:
        return
    rets = []
    for r in trades:
        cost_pct = cost_ticks * tick_size(r["entry"]) / r["entry"] * 100
        rets.append(r[rule] - cost_pct)
    wins = sum(1 for v in rets if v > 0)
    total = sum(rets)
    days = defaultdict(list)
    for r, v in zip(trades, rets):
        days[r["date"]].append(v)
    print(f"== {name} / {rule} / コスト{cost_ticks}ティック ==")
    print(f"  トレード数: {len(trades)} ({len(trades)/5:.1f}件/日)")
    print(f"  勝率: {wins/len(rets)*100:.1f}%  平均(期待値): {statistics.mean(rets):+.3f}%/回  中央値: {statistics.median(rets):+.3f}%")
    print(f"  合計リターン(1トレード等金額): {total:+.2f}%  最大勝ち {max(rets):+.2f}% 最大負け {min(rets):+.2f}%")
    for d in sorted(days):
        vs = days[d]
        print(f"    {d}: n={len(vs):2d} 平均 {statistics.mean(vs):+.3f}% 合計 {sum(vs):+.2f}%")
    # 30万円/トレードの場合の金額期待値
    print(f"  → 30万円/回運用時の期待値: 約{statistics.mean(rets)/100*300000:+,.0f}円/回, 週合計 約{total/100*300000:+,.0f}円")
    print()

pm = [r for r in results if r["time"] >= "13:00:00"]

# 案A: 午後のUNDER急増 → 大引け決済
us_pm = [r for r in pm if r["strategy"].startswith("UNDER")]
simulate(us_pm, "案A: 午後UNDER急増→大引け", "rclose")
simulate(us_pm, "案A': 午後UNDER急増→SL2%/TP2%", "sl2.0_tp2.0")

# 案B: 午後の小口売り連続(WATCH以上) → 大引け or SL1/TP1
sl_pm = [r for r in pm if r["strategy"].startswith("小口")]
simulate(sl_pm, "案B: 午後小口売り連続→大引け", "rclose")
simulate(sl_pm, "案B': 午後小口売り連続→SL1%/TP1%", "sl1.0_tp1.0")

# 案C: 午後の全シグナル → 大引け
simulate(pm, "案C: 午後全シグナル→大引け", "rclose")

# 参考: 全時間帯でやってしまった場合
simulate(results, "参考: 全時間帯全シグナル→大引け", "rclose")

# 参考: コスト0(指値約定想定)の案A
simulate(us_pm, "案A(コスト0=指値約定想定)", "rclose", cost_ticks=0.0)
