# -*- coding: utf-8 -*-
"""新戦略候補を「地合いを差し引いた超過リターン」で比べる検証（2026-10-03）。

weekly_report.py のシグナル検証は素の期待値しか出さないため、検証期間が
上昇基調なら買い戦略はみな良く見え、下落基調ならみな悪く見える。
2026-09-21 の節目割れ検証（analysis_levels_2026-09-21.md）では、これで
見かけの edge がほぼ消えた。そこで本スクリプトは各シグナルに対照群を付ける。

  対照群 = 同じ日・同じ時刻の足で、監視銘柄**全部**を買って同じ決済をした平均
  超過    = シグナルの損益 − 対照群の損益（その時刻の地合いを差し引いたもの）

検証する候補（すべて既存ログの検知を入力にする。新しい検知器は不要）:
  A. 定期買い集め WATCH / STRONG を大引けまで持つ（現行の買い集め追随は STRONG のみ）
  B. UNDER急増 × 当日の始値との位置（始値割れか否か）
  C. UNDER急増 × 当日すでに定期買い集めが出ている銘柄

先読みの排除・コスト・重複排除の順序は weekly_report.py と同じ。

実行:
    python analysis/research_candidates.py --date 2026-10-03 --since 2026-08-05
    （株価は analysis/output/<date>/bars_5m_long_<date>.json に保存し、次回は再利用）
"""
import argparse
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import weekly_report as wr  # noqa: E402

COST = wr.COST


def market_baseline(bars):
    """(日付, 時刻) -> その足の始値で全銘柄を買ったときの決済別リターン一覧を作る関数。"""
    index = defaultdict(list)   # (date, HH:MM) -> [(db, i)]
    for sym, days in bars.items():
        for d, db in days.items():
            for i, b in enumerate(db[:-1]):
                if b[1] >= wr.MIN_PRICE:
                    index[(d, b[0].strftime("%H:%M"))].append((db, i))
    cache = {}

    def base(d, hhmm, exit_fn):
        key = (d, hhmm, exit_fn.__name__)
        if key not in cache:
            vals = [v for v in (exit_fn(db, i) for db, i in index.get((d, hhmm), []))
                    if v is not None]
            cache[key] = sum(vals) / len(vals) if vals else None
        return cache[key]
    return base


def ex_close(db, i):
    return wr.simulate(db, i, None, None)


def ex_sl2(db, i):
    return wr.simulate(db, i, None, 2.0)


def ex_tp3_sl15(db, i):
    return wr.simulate(db, i, 3.0, 1.5)


def ex_60m(db, i):
    return wr.horizon(db, i, 60)


EXITS = [("大引け", ex_close), ("損切り-2%・大引け", ex_sl2),
         ("利確+3%/損切り-1.5%", ex_tp3_sl15), ("60分後", ex_60m)]


def first_per_day(cands, pred):
    seen, res = set(), []
    for e in cands:
        if not pred(e):
            continue
        k = (e["a"]["date"], e["a"]["symbol"])
        if k in seen:
            continue
        seen.add(k)
        res.append(e)
    return res


def row(label, es, exit_fn, base, mid):
    n = len(es)
    if n == 0:
        return f"| {label} | 0 | | | | | | | |"
    vals, exc = [], []
    for e in es:
        v = exit_fn(e["db"], e["i0"])
        b = base(e["a"]["date"], e["db"][e["i0"]][0].strftime("%H:%M"), exit_fn)
        if v is None or b is None:
            continue
        vals.append((e, v))
        exc.append(v - b)
    n = len(vals)
    w = sum(1 for _, v in vals if v > 0)
    mean = sum(v for _, v in vals) / n
    star = "*" if wr.binom_p(w, n) < 0.05 else ""
    ex_mean = sum(exc) / n
    ew = sum(1 for x in exc if x > 0)
    ex_star = "*" if wr.binom_p(ew, n) < 0.05 else ""
    h1 = [v for e, v in vals if e["a"]["date"] < mid]
    h2 = [v for e, v in vals if e["a"]["date"] >= mid]
    f = lambda xs: f"{sum(xs) / len(xs) - COST:+.2f}% ({len(xs)})" if xs else "—"  # noqa: E731
    c = Counter(e["a"]["symbol"] for e, _ in vals)
    top3 = sum(k for _, k in c.most_common(3)) / n * 100
    return (f"| {label} | {n} | {w / n * 100:.0f}%{star} | {mean - COST:+.2f}% | "
            f"**{ex_mean:+.2f}%**{ex_star} | {f(h1)} | {f(h2)} | {len(c)} | {top3:.0f}% |")


HDR = ("| 区分 | 件数 | 勝率 | 期待値 | 地合い超過 | 前半 | 後半 | 銘柄 | 上位3 |\n"
       "|---|---:|---:|---:|---:|---:|---:|---:|---:|")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True)
    ap.add_argument("--since", required=True)
    ap.add_argument("--refetch", action="store_true")
    args = ap.parse_args()

    outdir = os.path.join(wr.BASE, "output", args.date)
    os.makedirs(outdir, exist_ok=True)
    alerts = wr.parse_alerts(args.since)
    syms = sorted({a["symbol"] for a in alerts})
    bpath = os.path.join(outdir, f"bars_5m_long_{args.date}.json")
    if args.refetch or not os.path.exists(bpath):
        wr.fetch_bars(syms, args.since, args.date, bpath)
    bars = wr.load_bars(bpath)
    cands, skipped = wr.build_candidates(alerts, bars)
    days = sorted({d for s in bars.values() for d in s})
    mid = days[len(days) // 2]
    base = market_baseline(bars)

    # 当日の始値（1本目の足の始値）と、その日すでに定期買い集めが出ていたか
    for e in cands:
        e["open"] = e["db"][0][1]
        e["dev"] = (e["db"][e["i0"]][1] / e["open"] - 1) * 100
    accum_seen = defaultdict(lambda: None)    # (date,symbol) -> 最初の定期買い集めの時刻
    for a in alerts:
        if a["kind"] == "定期買い集め":
            k = (a["date"], a["symbol"])
            if accum_seen[k] is None or a["time"] < accum_seen[k]:
                accum_seen[k] = a["time"]

    kind = lambda e, k: e["a"]["kind"] == k  # noqa: E731
    U = lambda e: kind(e, "UNDER急増")        # noqa: E731
    groups = [
        ("【参考】UNDER急増 全体", U),
        ("【参考】UNDER急増 午後(13時〜)", lambda e: U(e) and e["hour"] >= 13),
        ("A1 定期買い集め STRONG", lambda e: kind(e, "定期買い集め") and e["a"]["level"] == "STRONG"),
        ("A2 定期買い集め WATCH", lambda e: kind(e, "定期買い集め") and e["a"]["level"] == "WATCH"),
        ("A3 定期買い集め どちらか", lambda e: kind(e, "定期買い集め")),
        ("A4 定期買い集め どちらか・4013除く", lambda e: kind(e, "定期買い集め") and e["a"]["symbol"] != "4013"),
        ("A5 定期買い集め どちらか・12時まで", lambda e: kind(e, "定期買い集め") and e["hour"] < 12),
        ("B1 UNDER急増・始値割れ", lambda e: U(e) and e["dev"] < 0),
        ("B2 UNDER急増・始値以上", lambda e: U(e) and e["dev"] >= 0),
        ("B3 UNDER急増・始値-2%以下", lambda e: U(e) and e["dev"] <= -2),
        ("B4 UNDER急増 午後・始値割れ", lambda e: U(e) and e["hour"] >= 13 and e["dev"] < 0),
        ("B5 UNDER急増 午後・始値以上", lambda e: U(e) and e["hour"] >= 13 and e["dev"] >= 0),
        ("C1 UNDER急増・当日に定期買い集め既出", lambda e: U(e) and accum_seen[(e["a"]["date"], e["a"]["symbol"])] is not None
         and accum_seen[(e["a"]["date"], e["a"]["symbol"])] < e["a"]["time"]),
    ]
    resolved = [(name, first_per_day(cands, pred)) for name, pred in groups]

    out = []
    p = out.append
    p(f"# 新戦略候補の検証（地合い超過） {args.date}")
    p("")
    p(f"アラート {len(alerts)}件（{args.since}以降）/ 候補 {len(cands)}件 / "
      f"株価 {days[0]}〜{days[-1]}（{len(days)}営業日）/ 前半・後半の境 {mid}")
    p(f"除外: " + " / ".join(f"{k} {v}件" for k, v in skipped.items()))
    p("")
    p("- 約定は検知の次の足の始値。利確・損切りが同じ足なら損切り優先。1銘柄1日1件（条件適用後）。")
    p("- 期待値・前半・後半は往復0.15%控除後。**地合い超過**は、同じ日・同じ時刻に監視銘柄を"
      "全部買って同じ決済をした平均との差（コストは両方にかかるので控除なし）。")
    p("- `*` は二項検定 p<0.05（勝率は50%と、超過は0と比べた勝ち負けの数で判定）。")
    p("")
    for lbl, fn in EXITS:
        p(f"## 決済: {lbl}")
        p(HDR)
        for name, es in resolved:
            p(row(name, es, fn, base, mid))
        p("")
    path = os.path.join(outdir, f"research_candidates_{args.date}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    print("\n".join(out))
    print(f"\n-> {path}")


if __name__ == "__main__":
    main()
