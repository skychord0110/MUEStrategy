# -*- coding: utf-8 -*-
"""アラート対象銘柄の5分足をYahoo Financeから取得してJSONで保存する（両週対応）。

先週分は analysis/output/bars_5m.json（07-13〜17）に既にある想定。
本スクリプトは今週分(07-21〜24)を取得し、日付キー付きで別ファイルに保存する。

入力: analysis/output/alerts_2026-07-24.csv
出力: analysis/output/bars_5m_week2.json  （{symbol: {date: {ts,open,...}}} 形式）
実行: python -X utf8 fetch_5m_2026-07-24.py

注意: Yahoo Financeの5分足は直近約60日分しか取得できない。取得済みJSONは保管すること。
"""
import csv
import json
import os
import time
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(BASE, "output")
ALERTS = os.path.join(OUT_DIR, "alerts_2026-07-24.csv")
OUT = os.path.join(OUT_DIR, "bars_5m_week2.json")

# 今週(week2): 2026-07-21 00:00 JST 〜 2026-07-25 00:00 JST
P1 = 1784559600  # 2026-07-21T00:00+09:00
P2 = 1784905200  # 2026-07-25T00:00+09:00

symbols = sorted({r["symbol"] for r in csv.DictReader(open(ALERTS, encoding="utf-8"))
                  if r["week"] == "week2"})
print(f"week2 {len(symbols)} symbols, {P1}..{P2}")

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
