# -*- coding: utf-8 -*-
"""仮想売買の成績を集計する。週次・戦略別・決済理由別・累積。

入力: analysis/output/2026-08-21/trades_2026-08-21.csv
実行: python analysis/summarize_trades_2026-08-21.py

往復コストは 0.15%（手数料＋スリッページ）を仮定。過去の分析と揃えてある。
勝率の有意性は「勝ちと負けが五分」を帰無仮説にした二項検定（両側）で見る。
"""
import csv
import os
import statistics as st
from collections import defaultdict
from datetime import date
from math import comb

BASE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(BASE, "output", "2026-08-21", "trades_2026-08-21.csv")
COST = 0.15


def binom_p(k, n):
    """n回中k回勝つ確率が偶然でどれだけ起こりにくいか（両側）。"""
    if n == 0:
        return 1.0
    tail = sum(comb(n, i) for i in range(n + 1)
               if abs(i - n / 2) >= abs(k - n / 2))
    return min(1.0, tail / (2 ** n))


def week_of(d):
    y, m, dd = (int(v) for v in d.split("-"))
    iso = date(y, m, dd).isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def stats(rows):
    p = [r["pct"] for r in rows]
    n = len(p)
    if not n:
        return None
    w = sum(1 for v in p if v > 0)
    return {"n": n, "win": w, "wr": w / n * 100, "mean": sum(p) / n,
            "med": st.median(p), "ev": sum(p) / n - COST, "tot": sum(p),
            "best": max(p), "worst": min(p),
            "sd": st.pstdev(p) if n > 1 else 0.0, "p": binom_p(w, n)}


def line(label, s, width=30):
    if not s:
        return f"  {label:<{width}} —"
    star = "*" if s["p"] < 0.05 else " "
    return (f"  {label:<{width}} n={s['n']:>3}  勝率{s['wr']:>5.1f}%{star} "
            f"平均{s['mean']:>+6.2f}%  中央{s['med']:>+6.2f}%  "
            f"期待値{s['ev']:>+6.2f}%  累計{s['tot']:>+7.1f}%")


def drawdown(rows):
    """時系列に並べた累積リターンの最大落ち込み。"""
    peak = cum = 0.0
    worst = 0.0
    for r in sorted(rows, key=lambda x: (x["date"], x["entry_time"])):
        cum += r["pct"] - COST
        peak = max(peak, cum)
        worst = min(worst, cum - peak)
    return worst, cum


def main():
    rows = []
    with open(SRC, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            r["pct"] = float(r["pct"])
            r["week"] = week_of(r["date"])
            rows.append(r)

    weeks = sorted({r["week"] for r in rows})
    last = weeks[-1]
    print(f"対象 {len(rows)}件 / {len(weeks)}週（{weeks[0]} 〜 {last}）"
          f" / 往復コスト {COST}% を控除")
    print("  * は勝率が偶然では説明しにくい水準（二項検定 p<0.05）")

    print(f"\n━━ 今週 {last} ━━")
    tw = [r for r in rows if r["week"] == last]
    print(line("全戦略", stats(tw)))
    for s in sorted({r["strategy"] for r in tw}):
        print(line(s, stats([r for r in tw if r["strategy"] == s])))

    print("\n━━ 累計（全期間） ━━")
    print(line("全戦略", stats(rows)))
    for s in sorted({r["strategy"] for r in rows}):
        print(line(s, stats([r for r in rows if r["strategy"] == s])))

    print("\n━━ 週ごとの推移（全戦略） ━━")
    for w in weeks:
        print(line(w, stats([r for r in rows if r["week"] == w]), width=30))

    print("\n━━ 決済理由の内訳（累計） ━━")
    for why in sorted({r["exit_reason"] for r in rows}):
        print(line(why, stats([r for r in rows if r["exit_reason"] == why])))

    print("\n━━ 検知トリガー別（累計） ━━")
    for t in sorted({r["trigger"] for r in rows}):
        print(line(t, stats([r for r in rows if r["trigger"] == t])))

    print("\n━━ 主力戦略の週次（戦略×週） ━━")
    for s in sorted({r["strategy"] for r in rows}):
        sub = [r for r in rows if r["strategy"] == s]
        if len(sub) < 10:
            continue
        print(f"  【{s}】")
        for w in weeks:
            ws = stats([r for r in sub if r["week"] == w])
            if ws:
                print("  " + line(w, ws, width=28))

    print("\n━━ 資金曲線 ━━")
    for s in [None] + sorted({r["strategy"] for r in rows}):
        sub = rows if s is None else [r for r in rows if r["strategy"] == s]
        if len(sub) < 5:
            continue
        dd, cum = drawdown(sub)
        print(f"  {(s or '全戦略'):<30} 累積{cum:>+7.1f}%  最大ドローダウン{dd:>7.1f}%")

    print("\n━━ 日別の件数（接続断の影響確認用） ━━")
    byday = defaultdict(list)
    for r in rows:
        byday[r["date"]].append(r["pct"])
    for d in sorted(byday):
        v = byday[d]
        print(f"  {d}  {len(v):>2}件  平均{sum(v) / len(v):>+6.2f}%")


if __name__ == "__main__":
    main()
