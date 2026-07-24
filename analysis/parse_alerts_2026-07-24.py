# -*- coding: utf-8 -*-
"""runnerログ（2026-07-13〜07-24の全営業日）から全アラートをパースしCSV化する。

先週分(07-13〜17)＋今週分(07-21〜24)を対象にする。
週の区別は date から自動判定（07-20以前=week1, 07-21以降=week2）。

出力: analysis/output/alerts_2026-07-24.csv
実行: python -X utf8 parse_alerts_2026-07-24.py
"""
import re
import csv
import glob
import os

BASE = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.normpath(os.path.join(BASE, "..", "strategies", "runner", "logs"))
OUT_DIR = os.path.join(BASE, "output")
OUT = os.path.join(OUT_DIR, "alerts_2026-07-24.csv")

line_re = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2}) (?P<time>\d{2}:\d{2}:\d{2}),\d+ \[INFO\] \[(?P<label>[^\]]+)\] (?P<symbol>\d{4}) \d{4}: (?P<body>.*)$"
)
price_re = re.compile(r"(?:現在値|買い気配)([\d.]+)円")
under_re = re.compile(r"UNDERが([\d]+)株→([\d]+)株に急増 \(\+([\d]+)株, \+([\d.]+)%\)")
small_re = re.compile(r"小口売り(\d+)回連続")

# 基礎検知ストラテジーのアラートだけを対象にする（AI仮想売買の行は別スクリプトで集計）
BASE_LABELS = ("UNDER急増", "小口売り連続/WATCH", "小口売り連続/STRONG",
               "投げ売り/買い気配へぶつけ", "投げ売り/投げ売り吸収")

rows = []
# 07-13〜17 と 07-21〜24 の両方をカバー
for path in sorted(glob.glob(os.path.join(LOG_DIR, "runner_2026-07-*.log"))):
    fdate = os.path.basename(path).replace("runner_", "").replace(".log", "")
    if fdate < "2026-07-13":
        continue  # 07-12 は起動テストのみ
    with open(path, encoding="utf-8") as f:
        for line in f:
            m = line_re.match(line.strip())
            if not m:
                continue
            d = m.groupdict()
            if d["label"] not in BASE_LABELS:
                continue
            pm = price_re.search(d["body"])
            price = float(pm.group(1)) if pm else None
            extra = {}
            um = under_re.search(d["body"])
            if um:
                extra = {"under_before": int(um.group(1)), "under_after": int(um.group(2)),
                         "under_inc": int(um.group(3)), "under_pct": float(um.group(4))}
            sm = small_re.search(d["body"])
            if sm:
                extra["consecutive"] = int(sm.group(1))
            week = "week1" if d["date"] <= "2026-07-17" else "week2"
            rows.append({
                "week": week, "date": d["date"], "time": d["time"],
                "strategy": d["label"], "symbol": d["symbol"], "price": price,
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
print("週別:", Counter(r["week"] for r in rows))
print("ストラテジー別:", Counter(r["strategy"] for r in rows))
print("日別:", dict(sorted(Counter(r["date"] for r in rows).items())))
print("銘柄数:", len(set(r["symbol"] for r in rows)))
