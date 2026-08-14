# -*- coding: utf-8 -*-
"""runnerログ（2026-07-13〜08-14の全営業日）から全アラートをパースしCSV化する。

week1:07-13〜17 / week2:07-21〜24 / week3:07-27〜31 / week4:08-03〜07
week5:08-10,12,13,14（08-11は山の日で休場）  計23営業日

基礎検知アラートと、AI仮想売買（実稼働のフォワード実績）を別ファイルに出力する。

出力: analysis/output/2026-08-14/alerts_2026-08-14.csv
      analysis/output/2026-08-14/paper_trades_2026-08-14.csv
実行: python -X utf8 parse_alerts_2026-08-14.py
"""
import re
import csv
import glob
import os
from collections import Counter

BASE = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.normpath(os.path.join(BASE, "..", "strategies", "runner", "logs"))
OUT_DIR = os.path.join(BASE, "output", "2026-08-14")

line_re = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2}) (?P<time>\d{2}:\d{2}:\d{2}),\d+ \[INFO\] "
    r"\[(?P<label>[^\]]+)\] (?P<symbol>\d{4}) \d{4}: (?P<body>.*)$")
price_re = re.compile(r"(?:現在値|買い気配)([\d.]+)円")
under_re = re.compile(r"UNDERが([\d]+)株→([\d]+)株に急増 \(\+([\d]+)株, \+([\d.]+)%\)")
small_re = re.compile(r"小口売り(\d+)回連続")
exit_re = re.compile(r"仮想決済 [\d.]+円→[\d.]+円 \((?P<ret>[+-][\d.]+)%\)")

# z値方式の定期買い集めは書式が違う（銘柄コードの後にコロンが無い形もある）ため別扱い
z_re = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2}) (?P<time>\d{2}:\d{2}:\d{2}),\d+ \[INFO\] "
    r"\[定期買い集め/(?P<tier>WATCH|STRONG)\] (?P<symbol>\d{4})")

BASE_LABELS = ("UNDER急増", "小口売り連続/WATCH", "小口売り連続/STRONG",
               "投げ売り/買い気配へぶつけ", "投げ売り/投げ売り吸収",
               "アルゴ買い集め(RSS)/WATCH", "アルゴ買い集め(RSS)/STRONG")


def week_of(d):
    if d <= "2026-07-17":
        return "week1"
    if d <= "2026-07-24":
        return "week2"
    if d <= "2026-07-31":
        return "week3"
    if d <= "2026-08-07":
        return "week4"
    return "week5"


alerts, papers, zsignals = [], [], []
for path in sorted(glob.glob(os.path.join(LOG_DIR, "runner_2026-0*.log"))):
    fdate = os.path.basename(path).replace("runner_", "").replace(".log", "")
    if fdate < "2026-07-13":
        continue
    for raw in open(path, encoding="utf-8", errors="replace"):
        raw = raw.strip()

        zm = z_re.match(raw)
        if zm:
            zd = zm.groupdict()
            zsignals.append({"week": week_of(zd["date"]), "date": zd["date"],
                             "time": zd["time"], "tier": zd["tier"],
                             "symbol": zd["symbol"]})
            continue

        m = line_re.match(raw)
        if not m:
            continue
        d = m.groupdict()
        label, body = d["label"], d["body"]

        # AI仮想売買（実稼働の結果）
        if label.startswith("AI"):
            strat = label.split("/")[0][2:]
            kind = "ENTRY" if "エントリー" in label else "EXIT"
            row = {"week": week_of(d["date"]), "date": d["date"], "time": d["time"],
                   "strategy": strat, "symbol": d["symbol"], "kind": kind,
                   "reason": label.split(":")[1].rstrip("]") if ":" in label else "",
                   "ret": ""}
            xm = exit_re.search(body)
            if xm:
                row["ret"] = float(xm.group("ret"))
            papers.append(row)
            continue

        if label not in BASE_LABELS:
            continue
        pm = price_re.search(body)
        um = under_re.search(body)
        sm = small_re.search(body)
        alerts.append({
            "week": week_of(d["date"]), "date": d["date"], "time": d["time"],
            "strategy": label, "symbol": d["symbol"],
            "price": float(pm.group(1)) if pm else None,
            "under_before": int(um.group(1)) if um else "",
            "under_after": int(um.group(2)) if um else "",
            "under_pct": float(um.group(4)) if um else "",
            "consecutive": int(sm.group(1)) if sm else "",
        })

os.makedirs(OUT_DIR, exist_ok=True)


def dump(rows, name):
    if not rows:
        print(f"（{name} は0件）")
        return
    with open(os.path.join(OUT_DIR, name), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


dump(alerts, "alerts_2026-08-14.csv")
dump(papers, "paper_trades_2026-08-14.csv")
dump(zsignals, "zscore_signals_2026-08-14.csv")

print(f"基礎アラート {len(alerts)}件 / AI仮想売買 {len(papers)}行 / z値シグナル {len(zsignals)}件")
print("週別:", dict(sorted(Counter(r["week"] for r in alerts).items())))
print("種別:", dict(Counter(r["strategy"] for r in alerts)))
print("銘柄数:", len(set(r["symbol"] for r in alerts)))
print("日別(week5):", dict(sorted(
    Counter(r["date"] for r in alerts if r["week"] == "week5").items())))
