# -*- coding: utf-8 -*-
"""アラート対象銘柄の5分足をYahoo Financeから取得してJSONで保存する。

入力: analysis/output/alerts.csv（parse_alerts_2026-07-18.py の出力）
出力: analysis/output/bars_5m.json
実行: python -X utf8 fetch_5m_2026-07-18.py

注意: Yahoo Financeの5分足は直近約60日分しか取得できない。
      分析対象週が古くなった場合は再取得できないので bars_5m.json を保管しておくこと。
"""
import csv
import json
import os
import time
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(BASE, "output")
ALERTS = os.path.join(OUT_DIR, "alerts.csv")
OUT = os.path.join(OUT_DIR, "bars_5m.json")

# 分析対象週: 2026-07-13 00:00 JST 〜 2026-07-18 00:00 JST
P1 = 1783868400  # 2026-07-13T00:00+09:00
P2 = 1784300400  # 2026-07-18T00:00+09:00

symbols = sorted({r["symbol"] for r in csv.DictReader(open(ALERTS, encoding="utf-8"))})
print(f"{len(symbols)} symbols")

data = {}
errors = []
for i, sym in enumerate(symbols):
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}.T"
           f"?period1={P1}&period2={P2}&interval=5m")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            j = json.load(resp)
        res = j["chart"]["result"][0]
        ts = res["timestamp"]
        q = res["indicators"]["quote"][0]
        data[sym] = {"ts": ts, "open": q["open"], "high": q["high"],
                     "low": q["low"], "close": q["close"], "volume": q["volume"]}
        print(f"[{i+1}/{len(symbols)}] {sym}: {len(ts)} bars")
    except Exception as e:
        errors.append((sym, str(e)))
        print(f"[{i+1}/{len(symbols)}] {sym}: ERROR {e}")
    time.sleep(0.6)

json.dump(data, open(OUT, "w"))
print(f"saved {len(data)} symbols -> {OUT}")
if errors:
    print("errors:", errors)
