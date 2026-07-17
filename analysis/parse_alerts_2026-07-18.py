# -*- coding: utf-8 -*-
"""runnerログ（2026-07-13〜07-17）から全アラートをパースしてCSV化する。

出力: analysis/output/alerts.csv
実行: python -X utf8 parse_alerts_2026-07-18.py
"""
import re
import csv
import glob
import os

BASE = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.normpath(os.path.join(BASE, "..", "strategies", "runner", "logs"))
OUT_DIR = os.path.join(BASE, "output")
OUT = os.path.join(OUT_DIR, "alerts.csv")

# パース対象のログ行の例:
# 2026-07-14 09:01:01,615 [INFO] [UNDER急増] 9130 9130: UNDERが18000株→25800株に急増 (+7800株, +43.3%)。... （現在値1450.0円・安値圏）
# 2026-07-14 09:03:06,966 [INFO] [小口売り連続/WATCH] 4418 4418: 買い気配765.0円に小口売り5回連続 (直近の推定約定株数 100.0株)
# 2026-07-14 09:00:26,036 [INFO] [投げ売り/買い気配へぶつけ] 4813 4813: OVERから消えた12900株とほぼ同数(16300株)が買い気配にぶつけられました（投げ売り・現在値326.0円）

line_re = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2}) (?P<time>\d{2}:\d{2}:\d{2}),\d+ \[INFO\] \[(?P<label>[^\]]+)\] (?P<symbol>\d{4}) \d{4}: (?P<body>.*)$"
)
price_re = re.compile(r"(?:現在値|買い気配)([\d.]+)円")
under_re = re.compile(r"UNDERが([\d]+)株→([\d]+)株に急増 \(\+([\d]+)株, \+([\d.]+)%\)")
small_re = re.compile(r"小口売り(\d+)回連続")

rows = []
for path in sorted(glob.glob(os.path.join(LOG_DIR, "runner_2026-07-1[3-7].log"))):
    with open(path, encoding="utf-8") as f:
        for line in f:
            m = line_re.match(line.strip())
            if not m:
                continue
            d = m.groupdict()
            pm = price_re.search(d["body"])
            price = float(pm.group(1)) if pm else None
            extra = {}
            um = under_re.search(d["body"])
            if um:
                extra = {
                    "under_before": int(um.group(1)),
                    "under_after": int(um.group(2)),
                    "under_inc": int(um.group(3)),
                    "under_pct": float(um.group(4)),
                }
            sm = small_re.search(d["body"])
            if sm:
                extra["consecutive"] = int(sm.group(1))
            rows.append({
                "date": d["date"],
                "time": d["time"],
                "strategy": d["label"],
                "symbol": d["symbol"],
                "price": price,
                "under_before": extra.get("under_before", ""),
                "under_after": extra.get("under_after", ""),
                "under_inc": extra.get("under_inc", ""),
                "under_pct": extra.get("under_pct", ""),
                "consecutive": extra.get("consecutive", ""),
            })

os.makedirs(OUT_DIR, exist_ok=True)
with open(OUT, "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
    w.writeheader()
    w.writerows(rows)

print(f"parsed {len(rows)} alerts -> {OUT}")
from collections import Counter
print(Counter(r["strategy"] for r in rows))
print("days:", Counter(r["date"] for r in rows))
print("symbols:", len(set(r["symbol"] for r in rows)))
