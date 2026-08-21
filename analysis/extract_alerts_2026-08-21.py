# -*- coding: utf-8 -*-
"""場中ログから需給シグナルのアラートを取り出してCSVにする。

仮想売買の対象になっていない検知（小口売り連続・定期買い集め）も含めて拾い、
あとで株価と突き合わせて「本当に上がったのか」を検証できるようにする。

実行:
    python analysis/extract_alerts_2026-08-21.py
出力:
    analysis/output/2026-08-21/alerts_2026-08-21.csv
"""
import csv
import os
import re
from collections import Counter

BASE = os.path.dirname(os.path.abspath(__file__))
LOGDIR = os.path.normpath(os.path.join(BASE, "..", "strategies", "runner", "logs"))
OUTDIR = os.path.join(BASE, "output", "2026-08-21")
SINCE = "2026-07-21"          # Yahooの5分足が遡れる範囲に合わせる

HEAD = r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}),\d+ \[INFO\] "
PATS = [
    ("UNDER急増", re.compile(
        HEAD + r"\[UNDER急増\] (\d+) .*?急増 \(\+(\d+)株, \+([\d.]+)%\)"
        r".*?現在値([\d.]+)円・([^）]+)）")),
    ("小口売り連続", re.compile(
        HEAD + r"\[小口売り連続/(\w+)\] (\d+) \S+: 買い気配([\d.]+)円に"
        r"小口売り(\d+)回連続")),
    ("定期買い集め", re.compile(
        HEAD + r"\[定期買い集め/(\w+)\] (\d+) \S+: .*?z値 ([\d.]+) / 該当(\d+)件")),
]


def main():
    os.makedirs(OUTDIR, exist_ok=True)
    rows, kinds = [], Counter()
    for name in sorted(os.listdir(LOGDIR)):
        if not (name.startswith("runner_") and name.endswith(".log")):
            continue
        if name[7:17] < SINCE:
            continue
        with open(os.path.join(LOGDIR, name), encoding="utf-8",
                  errors="replace") as f:
            for line in f:
                for kind, pat in PATS:
                    m = pat.match(line)
                    if not m:
                        continue
                    g = m.groups()
                    if kind == "UNDER急増":
                        r = {"kind": kind, "level": "", "date": g[0],
                             "time": g[1], "symbol": g[2], "price": g[5],
                             "v1": g[4], "v2": g[3], "note": g[6]}
                        # v1=増加率(%)  v2=増加株数
                    elif kind == "小口売り連続":
                        r = {"kind": kind, "level": g[2], "date": g[0],
                             "time": g[1], "symbol": g[3], "price": g[4],
                             "v1": g[5], "v2": "", "note": ""}
                    else:
                        r = {"kind": kind, "level": g[2], "date": g[0],
                             "time": g[1], "symbol": g[3], "price": "",
                             "v1": g[4], "v2": g[5], "note": ""}
                    rows.append(r)
                    kinds[f"{kind}/{r['level']}" if r["level"] else kind] += 1
                    break

    path = os.path.join(OUTDIR, "alerts_2026-08-21.csv")
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["kind", "level", "date", "time",
                                          "symbol", "price", "v1", "v2", "note"])
        w.writeheader()
        w.writerows(rows)

    syms = sorted({r["symbol"] for r in rows})
    days = sorted({r["date"] for r in rows})
    print(f"{SINCE} 以降のアラート {len(rows)}件 / {len(syms)}銘柄 / {len(days)}日")
    for k, n in kinds.most_common():
        print(f"  {k:<24} {n:>5}件")
    print(f"  出力: {path}")
    notes = Counter(r["note"] for r in rows if r["note"])
    if notes:
        print("  UNDER急増の値位置:", dict(notes))


if __name__ == "__main__":
    main()
