# -*- coding: utf-8 -*-
"""week5（2026-08-10〜08-14）のアラート銘柄の5分足をYahoo Financeから取得する。

入力: analysis/output/2026-08-14/alerts_2026-08-14.csv
出力: analysis/output/2026-08-14/bars_5m_week5.json
実行: python -X utf8 fetch_5m_2026-08-14.py

注意: Yahoo Financeの5分足は直近約60日分しか取得できない。取得済みJSONは保管すること。
      過去週ぶんは analysis/output/bars_5m*.json に保存済み。
"""
import csv
import json
import os
import time
import urllib.request
from datetime import datetime, timezone, timedelta

BASE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(BASE, "output", "2026-08-14")
ALERTS = os.path.join(OUT_DIR, "alerts_2026-08-14.csv")
OUT = os.path.join(OUT_DIR, "bars_5m_week5.json")

JST = timezone(timedelta(hours=9))
P1 = int(datetime(2026, 8, 10, 0, 0, tzinfo=JST).timestamp())   # 08-10 00:00 JST
P2 = int(datetime(2026, 8, 15, 0, 0, tzinfo=JST).timestamp())   # 08-15 00:00 JST

symbols = sorted({r["symbol"] for r in csv.DictReader(open(ALERTS, encoding="utf-8"))
                  if r["week"] == "week5"})
print(f"week5 {len(symbols)}銘柄  期間 {P1}..{P2}")

data, errors = {}, []
for i, sym in enumerate(symbols, 1):
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}.T"
           f"?period1={P1}&period2={P2}&interval=5m")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            j = json.load(resp)
        res = j["chart"]["result"][0]
        q = res["indicators"]["quote"][0]
        data[sym] = {"ts": res["timestamp"], "open": q["open"], "high": q["high"],
                     "low": q["low"], "close": q["close"], "volume": q["volume"]}
        print(f"[{i}/{len(symbols)}] {sym}: {len(res['timestamp'])} bars")
    except Exception as e:
        errors.append((sym, str(e)))
        print(f"[{i}/{len(symbols)}] {sym}: ERROR {e}")
    time.sleep(0.6)

json.dump(data, open(OUT, "w"))
print(f"saved {len(data)}銘柄 -> {OUT}")
if errors:
    print("errors:", errors)
