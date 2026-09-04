# -*- coding: utf-8 -*-
"""板のシグナル（UNDER急増）に、歩み値side（出来高・ヒゲ）の確認条件を足して精度を上げる。

5分足だけから需給を読む試み（research_signals.py）は全滅した。勝率45〜53%で、
既存のUNDER急増（午後67.6%）に遠く及ばない。**板の厚みは価格と出来高には
写らない**ということで、これは想定の範囲。

そこで方針を変え、板のシグナルを土台にして、そこに5分足から取れる確認条件を
重ねる。「下値に買いが積まれた（板）」ことに加えて「実際に売りが吸収された
（歩み値）」を要求する、という二段構えの読み。

過剰適合を避けるため、条件は**単独の特徴量ごとに**評価し、判定期間で効いた
ものが検証期間でも効くかを必ず確認する。

実行:
    python analysis/research_combo.py
出力:
    analysis/output/research/combo_report.md
"""
import json
import os
import re
import statistics as st
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from math import comb

BASE = os.path.dirname(os.path.abspath(__file__))
LOGDIR = os.path.normpath(os.path.join(BASE, "..", "strategies", "runner", "logs"))
OUTDIR = os.path.join(BASE, "output", "research")
JST = timezone(timedelta(hours=9))

COST = 0.15
MIN_PRICE = 500.0
SINCE = "2026-07-01"
TARGET_DAYS = 20

RE_UNDER = re.compile(
    r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}),\d+ \[INFO\] "
    r"\[UNDER急増\] (\d+) .*?急増 \(\+(\d+)株, \+([\d.]+)%\).*?現在値([\d.]+)円")


def binom_p(k, n):
    if n == 0:
        return 1.0
    tail = sum(comb(n, i) for i in range(n + 1)
               if abs(i - n / 2) >= abs(k - n / 2))
    return min(1.0, tail / (2 ** n))


def load_alerts():
    out = []
    for name in sorted(os.listdir(LOGDIR)):
        if not (name.startswith("runner_") and name.endswith(".log")):
            continue
        if name[7:17] < SINCE:
            continue
        with open(os.path.join(LOGDIR, name), encoding="utf-8",
                  errors="replace") as f:
            for line in f:
                m = RE_UNDER.match(line)
                if m:
                    d, t, sym, qty, pct, px = m.groups()
                    out.append({"date": d, "time": t, "sym": sym,
                                "inc_pct": float(pct), "px": float(px)})
    return out


def load_bars():
    raw = json.load(open(os.path.join(OUTDIR, "bars_ohlcv.json")))
    bars = defaultdict(dict)
    for sym, d in raw.items():
        tmp = defaultdict(list)
        for i, ts in enumerate(d["ts"]):
            o, h, l, c = d["open"][i], d["high"][i], d["low"][i], d["close"][i]
            v = (d.get("volume") or [None] * len(d["ts"]))[i]
            if None in (o, h, l, c) or v is None:
                continue
            t = datetime.fromtimestamp(ts, JST)
            tmp[t.strftime("%Y-%m-%d")].append((t, o, h, l, c, v))
        for day, b in tmp.items():
            b.sort()
            if len(b) >= 20:
                bars[sym][day] = b
    return bars


def features(day_bars, i):
    """シグナルが出た足までの情報だけで特徴量を作る（先読みしない）。"""
    seen = day_bars[:i + 1]
    t, o, h, l, c, v = day_bars[i]
    vols = [x[5] for x in seen if x[5] > 0]
    med = st.median(vols) if vols else 0.0
    tv = sum(x[5] for x in seen)
    vwap = (sum((x[2] + x[3] + x[4]) / 3 * x[5] for x in seen) / tv) if tv else c
    day_low = min(x[3] for x in seen)
    day_high = max(x[2] for x in seen)
    span = day_high - day_low
    rng = h - l
    prev = seen[max(0, i - 5):i + 1]
    drop = (c / max(x[2] for x in prev) - 1) * 100 if prev else 0.0
    return {
        "hour": t.hour,
        "vol_mult": (v / med) if med > 0 else 0.0,
        "wick": ((c - l) / rng) if rng > 0 else 0.5,
        "pos_in_day": ((c - day_low) / span) if span > 0 else 0.5,
        "vs_vwap": (c / vwap - 1) * 100 if vwap else 0.0,
        "drop_5": drop,
        "bull": 1.0 if c >= o else 0.0,
    }


def simulate(day_bars, i0, tp=2.0, sl=2.0):
    entry = day_bars[i0][1]
    if entry <= 0:
        return None
    for _, o, h, l, c, v in day_bars[i0:]:
        if sl is not None and l <= entry * (1 - sl / 100):
            return -sl
        if tp is not None and h >= entry * (1 + tp / 100):
            return tp
    return (day_bars[-1][4] / entry - 1) * 100


def stats(vals):
    n = len(vals)
    if not n:
        return None
    w = sum(1 for v in vals if v > 0)
    m = sum(vals) / n
    return {"n": n, "wr": w / n * 100, "mean": m, "ev": m - COST,
            "p": binom_p(w, n)}


def row(label, s):
    if not s:
        return f"| {label} | — | | | |"
    star = "*" if s["p"] < 0.05 else ""
    return (f"| {label} | {s['n']} | {s['wr']:.1f}%{star} | "
            f"{s['mean']:+.2f}% | {s['ev']:+.2f}% |")


HEAD = "| 条件 | 件数 | 勝率 | 平均 | 期待値 |\n|---|---:|---:|---:|---:|"


def main():
    alerts, bars = load_alerts(), load_bars()
    # 午後のUNDER急増だけを土台にする（午前は既存分析で優位性が無い）
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
        f = features(db, i - 1 if i > 0 else 0)     # シグナルが出た足の特徴
        r = simulate(db, i)
        if r is None:
            continue
        recs.append({**a, **f, "ret": r})

    dates = sorted({r["date"] for r in recs})
    target = set(dates[-TARGET_DAYS:])
    hold = set(dates[:-TARGET_DAYS])
    T = [r for r in recs if r["date"] in target]
    H = [r for r in recs if r["date"] in hold]

    out = ["# 板シグナル × 歩み値の確認条件", "",
           f"土台: 午後(13時〜)のUNDER急増・{MIN_PRICE:.0f}円以上・1銘柄1日1回",
           f"決済: 利確+2% / 損切り-2%（既存分析で最良だった条件）",
           f"対象 {len(recs)}件 / {len(dates)}営業日（{dates[0]} 〜 {dates[-1]}）",
           f"　判定期間 直近{min(TARGET_DAYS, len(dates))}営業日: {len(T)}件",
           f"　検証期間 それ以前: {len(H)}件", "",
           "## 土台そのもの（確認条件なし）", "", HEAD,
           row("判定期間", stats([r["ret"] for r in T])),
           row("検証期間", stats([r["ret"] for r in H])), ""]

    # 単独の確認条件ごとに、判定期間と検証期間の両方で効くかを見る
    conds = [
        ("出来高が中央値の1.5倍以上", lambda r: r["vol_mult"] >= 1.5),
        ("出来高が中央値の2倍以上", lambda r: r["vol_mult"] >= 2.0),
        ("下ヒゲ比率0.5以上（売り吸収）", lambda r: r["wick"] >= 0.5),
        ("下ヒゲ比率0.7以上", lambda r: r["wick"] >= 0.7),
        ("当日安値圏（下から30%以内）", lambda r: r["pos_in_day"] <= 0.30),
        ("当日安値圏（下から20%以内）", lambda r: r["pos_in_day"] <= 0.20),
        ("VWAP以下", lambda r: r["vs_vwap"] <= 0),
        ("VWAPを1%以上下回る", lambda r: r["vs_vwap"] <= -1.0),
        ("直近5本で1%以上下落した後", lambda r: r["drop_5"] <= -1.0),
        ("直近5本で2%以上下落した後", lambda r: r["drop_5"] <= -2.0),
        ("陽線で確認", lambda r: r["bull"] > 0),
        ("UNDER増加率30%以上", lambda r: r["inc_pct"] >= 30),
        ("UNDER増加率50%以上", lambda r: r["inc_pct"] >= 50),
        ("13時台のみ", lambda r: r["hour"] == 13),
        ("14時台以降", lambda r: r["hour"] >= 14),
    ]
    out += ["## 確認条件を1つずつ足す", "", HEAD]
    good = []
    for name, fn in conds:
        stt = stats([r["ret"] for r in T if fn(r)])
        sth = stats([r["ret"] for r in H if fn(r)])
        out.append(row(f"判定 {name}", stt))
        out.append(row(f"　検証 {name}", sth))
        if stt and stt["n"] >= 15 and stt["wr"] >= 70 and stt["ev"] > 0:
            good.append((name, fn, stt, sth))
    out.append("")

    out += ["## 判定期間で勝率70%以上・n>=15・期待値プラス", "", HEAD]
    if good:
        for name, _, stt, sth in sorted(good, key=lambda x: -x[2]["wr"]):
            out.append(row(name, stt))
            out.append(row("　検証期間での再現", sth))
    else:
        out.append("| （なし） | | | | |")

    path = os.path.join(OUTDIR, "combo_report.md")
    open(path, "w", encoding="utf-8", newline="\n").write("\n".join(out) + "\n")
    print("\n".join(out[:12]))
    print(f"\n条件を満たしたもの: {len(good)}件")
    for name, _, stt, sth in sorted(good, key=lambda x: -x[2]["wr"]):
        hv = f"検証 n={sth['n']} {sth['wr']:.1f}% {sth['ev']:+.2f}%" if sth else "検証なし"
        print(f"  {name:<26} 判定 n={stt['n']:>3} {stt['wr']:>5.1f}% "
              f"{stt['ev']:+.2f}%  ({hv})")
    print(f"\n詳細: {path}")


if __name__ == "__main__":
    main()
