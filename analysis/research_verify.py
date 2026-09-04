# -*- coding: utf-8 -*-
"""有望だった条件が「本物か、20営業日にたまたま合っただけか」を詰める。

見るのは4点。
  1. しきい値を動かしたとき、成績がなめらかに変わるか
     （ある1点だけ跳ね上がるなら、その値に合わせただけの可能性が高い）
  2. 週ごとに安定しているか（特定の1週が全部を作っていないか）
  3. 銘柄が偏っていないか（数銘柄の癖を見ているだけではないか）
  4. 決済ルールを変えても優位が残るか

実行:
    python analysis/research_verify.py
"""
import os
import sys
from collections import Counter
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from research_combo import (  # noqa: E402
    COST, MIN_PRICE, TARGET_DAYS, load_alerts, load_bars, features, simulate,
    stats, JST)
from datetime import datetime  # noqa: E402


def build(tp=2.0, sl=2.0):
    alerts, bars = load_alerts(), load_bars()
    recs, seen = [], set()
    for a in alerts:
        if int(a["time"][:2]) < 13:
            continue
        key = (a["date"], a["sym"])
        if key in seen:
            continue
        db = bars.get(a["sym"], {}).get(a["date"])
        if not db:
            continue
        ts = datetime.strptime(f"{a['date']} {a['time']}",
                               "%Y-%m-%d %H:%M:%S").replace(tzinfo=JST)
        i = next((k for k, b in enumerate(db) if b[0] > ts), None)
        if i is None or i >= len(db) - 1 or db[i][1] < MIN_PRICE:
            continue
        seen.add(key)
        f = features(db, i - 1 if i > 0 else 0)
        r = simulate(db, i, tp, sl)
        if r is None:
            continue
        recs.append({**a, **f, "ret": r})
    return recs


def wk(d):
    y, m, dd = (int(v) for v in d.split("-"))
    iso = date(y, m, dd).isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def show(label, rows):
    s = stats([r["ret"] for r in rows])
    if not s:
        print(f"  {label:<26} —")
        return
    star = "*" if s["p"] < 0.05 else " "
    syms = Counter(r["sym"] for r in rows)
    top3 = sum(c for _, c in syms.most_common(3)) / len(rows) * 100
    print(f"  {label:<26} n={s['n']:>3} 勝率{s['wr']:>5.1f}%{star} "
          f"期待値{s['ev']:>+6.2f}%  銘柄{len(syms):>2} 上位3で{top3:>3.0f}%")


def main():
    recs = build()
    dates = sorted({r["date"] for r in recs})
    target = set(dates[-TARGET_DAYS:])

    print("=" * 78)
    print("1. しきい値を動かす（VWAPからの乖離）— なめらかに変わるか")
    print("=" * 78)
    print("  ＜判定期間（直近20営業日）＞")
    for th in (0.0, -0.5, -1.0, -1.5, -2.0, -2.5):
        show(f"VWAP{th:+.1f}%以下",
             [r for r in recs if r["date"] in target and r["vs_vwap"] <= th])
    print("  ＜検証期間（それ以前）＞")
    for th in (0.0, -0.5, -1.0, -1.5, -2.0, -2.5):
        show(f"VWAP{th:+.1f}%以下",
             [r for r in recs if r["date"] not in target and r["vs_vwap"] <= th])

    print("\n" + "=" * 78)
    print("2. 週ごとの安定性（VWAP-1%以下）")
    print("=" * 78)
    sel = [r for r in recs if r["vs_vwap"] <= -1.0]
    for w in sorted({wk(r["date"]) for r in sel}):
        show(w, [r for r in sel if wk(r["date"]) == w])

    print("\n" + "=" * 78)
    print("3. 銘柄の偏り（VWAP-1%以下・全期間）")
    print("=" * 78)
    syms = Counter(r["sym"] for r in sel)
    print(f"  {len(sel)}件 / {len(syms)}銘柄 / 上位3銘柄で"
          f"{sum(c for _, c in syms.most_common(3)) / len(sel) * 100:.0f}%")
    print("  最多: " + ", ".join(f"{s}×{c}" for s, c in syms.most_common(5)))

    print("\n" + "=" * 78)
    print("4. 決済ルールを変える（VWAP-1%以下・全期間）")
    print("=" * 78)
    for lbl, tp, sl in (("利確+2%/損切り-2%", 2.0, 2.0),
                        ("利確+2%/損切り-1%", 2.0, 1.0),
                        ("利確+3%/損切り-2%", 3.0, 2.0),
                        ("利確+1.5%/損切り-1.5%", 1.5, 1.5),
                        ("利確なし/損切り-2%", None, 2.0),
                        ("大引けのみ", None, None)):
        rows = [r for r in build(tp, sl) if r["vs_vwap"] <= -1.0]
        show(lbl, rows)

    print("\n" + "=" * 78)
    print("5. 土台との比較（同じ期間・同じ決済）")
    print("=" * 78)
    show("UNDER急増 午後 すべて", recs)
    show("  + VWAP-1%以下", sel)
    show("  + VWAP-1%以下（判定期間）",
         [r for r in sel if r["date"] in target])
    show("  + VWAP-1%以下（検証期間）",
         [r for r in sel if r["date"] not in target])
    excluded = [r for r in recs if r["vs_vwap"] > -1.0]
    show("  除外された側", excluded)


if __name__ == "__main__":
    main()
