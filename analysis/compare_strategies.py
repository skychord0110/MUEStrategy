# -*- coding: utf-8 -*-
"""2つの戦略を「同じ期間・同じシグナル」で突き合わせる。

全期間の集計を並べるだけでは、稼働開始日が違う戦略を比べたときに
期間差と実力差が混ざってしまう。片方がもう片方のフィルタ版である場合は
なおさらで、本当に知りたいのは「**フィルタが落としたシグナルは良かったのか
悪かったのか**」であり、それは対応付けないと出てこない。

実行:
    python analysis/compare_strategies.py <trades.csv> "戦略A" "戦略B"
    （省略時は最新の trades_*.csv と 午後引け戻り／順位優先）
"""
import csv
import glob
import os
import statistics as st
import sys
from math import comb

BASE = os.path.dirname(os.path.abspath(__file__))
COST = 0.15


def binom_p(k, n):
    if n == 0:
        return 1.0
    tail = sum(comb(n, i) for i in range(n + 1)
               if abs(i - n / 2) >= abs(k - n / 2))
    return min(1.0, tail / (2 ** n))


def stats(vals):
    n = len(vals)
    if not n:
        return None
    w = sum(1 for v in vals if v > 0)
    m = sum(vals) / n
    return {"n": n, "w": w, "wr": w / n * 100, "mean": m, "ev": m - COST,
            "tot": sum(vals), "med": st.median(vals), "p": binom_p(w, n)}


def line(label, s, width=30):
    if not s:
        return f"  {label:<{width}} —"
    star = "*" if s["p"] < 0.05 else " "
    return (f"  {label:<{width}} n={s['n']:>3}  勝率{s['wr']:>5.1f}%{star} "
            f"平均{s['mean']:>+6.2f}%  期待値{s['ev']:>+6.2f}%  累計{s['tot']:>+7.1f}%")


def main():
    args = sys.argv[1:]
    path = args[0] if args else sorted(
        glob.glob(os.path.join(BASE, "output", "*", "trades_*.csv")))[-1]
    a = args[1] if len(args) > 1 else "AI午後引け戻り"
    b = args[2] if len(args) > 2 else "AI午後引け戻り(順位優先)"

    rows = list(csv.DictReader(open(path, encoding="utf-8-sig")))
    for r in rows:
        r["pct"] = float(r["pct"])
    print(f"入力: {os.path.relpath(path, BASE)}")
    print(f"  A = {a}\n  B = {b}\n")

    # 稼働期間が違うので、両方が動いていた期間だけで比べる
    start = min((r["date"] for r in rows if r["strategy"] == b), default=None)
    if start is None:
        print("Bのトレードがありません")
        return 1
    common = [r for r in rows if r["date"] >= start]
    ra = {(r["date"], r["symbol"]): r for r in common if r["strategy"] == a}
    rb = {(r["date"], r["symbol"]): r for r in common if r["strategy"] == b}
    both, only_a, only_b = ra.keys() & rb.keys(), ra.keys() - rb.keys(), rb.keys() - ra.keys()

    print(f"━━ 全期間（期間差を含むので参考値）━━")
    print(line(f"A {a}", stats([r["pct"] for r in rows if r["strategy"] == a])))
    print(line(f"B {b}", stats([r["pct"] for r in rows if r["strategy"] == b])))

    print(f"\n━━ 共通期間（{start} 以降）で全体を比べる ━━")
    print(line("A（全シグナル）", stats([ra[k]["pct"] for k in ra])))
    print(line("B（絞り込み後）", stats([rb[k]["pct"] for k in rb])))

    print(f"\n━━ シグナルの対応 ━━")
    print(f"  両方が取った            : {len(both)}件")
    print(f"  Aだけ（Bは見送り）      : {len(only_a)}件  ← Bのフィルタの是非はここで決まる")
    print(f"  Bだけ                   : {len(only_b)}件")

    if both:
        da = [ra[k]["pct"] for k in sorted(both)]
        db = [rb[k]["pct"] for k in sorted(both)]
        same = sum(1 for x, y in zip(da, db) if abs(x - y) < 1e-9)
        print(f"\n━━ 両方が取った{len(both)}件 ━━")
        print(line("A", stats(da)))
        print(line("B", stats(db)))
        print(f"  うち結果が完全に一致: {same}/{len(both)}件"
              f"{'（＝同じ値段・同じ決済。差は付かない）' if same == len(both) else ''}")

    if only_a:
        v = [ra[k]["pct"] for k in only_a]
        s = stats(v)
        print(f"\n━━ Bが見送った{len(only_a)}件（Aは取った）━━")
        print(line("見送られたシグナル", s))
        print(f"  内訳: " + " / ".join(
            f"{ra[k]['symbol']} {ra[k]['pct']:+.2f}%" for k in sorted(only_a)))
        print()
        if s["mean"] > 0:
            print(f"  → 見送った側の平均が{s['mean']:+.2f}%。**利益を捨てている**。")
            print(f"     この{len(only_a)}件を取らなかったことで累計{s['tot']:+.1f}%を逃した。")
        else:
            print(f"  → 見送った側の平均が{s['mean']:+.2f}%。フィルタが損失を避けている。")

    print("\n━━ 判断の材料 ━━")
    if both and only_a:
        sa, sb = stats([ra[k]["pct"] for k in ra]), stats([rb[k]["pct"] for k in rb])
        print(f"  共通期間の期待値   A {sa['ev']:+.2f}%  vs  B {sb['ev']:+.2f}%"
              f"  （差 {sb['ev'] - sa['ev']:+.2f}%）")
        print(f"  同じ期間の総取り高 A {sa['tot']:+.1f}%  vs  B {sb['tot']:+.1f}%")
        print(f"  1件あたりで勝っても、取れる件数が少なければ総額では負けることがある。")
        print(f"  見送り{len(only_a)}件の統計的な確からしさ: "
              f"p={stats([ra[k]['pct'] for k in only_a])['p']:.2f}"
              f"（0.05未満なら偶然とは言いにくい）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
