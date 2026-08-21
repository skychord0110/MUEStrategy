# -*- coding: utf-8 -*-
"""アラートが出た銘柄の5分足をYahoo Financeから取得する。

kabuステーションAPIの /timeandsales は直近2営業日しか遡れないため、
過去1か月ぶんの検証にはこちらを使う（Yahooの5分足は約60日ぶん取れる）。
公開されている株価を読むだけで、口座には一切触らない。

入力: analysis/output/2026-08-21/alerts_2026-08-21.csv
出力: analysis/output/2026-08-21/bars_5m_2026-08-21.json
実行: python -X utf8 analysis/fetch_5m_2026-08-21.py
"""
import csv
import json
import os
import time
import urllib.request
from datetime import datetime, timedelta, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
OUTDIR = os.path.join(BASE, "output", "2026-08-21")
ALERTS = os.path.join(OUTDIR, "alerts_2026-08-21.csv")
OUT = os.path.join(OUTDIR, "bars_5m_2026-08-21.json")

JST = timezone(timedelta(hours=9))
P1 = int(datetime(2026, 7, 20, 0, 0, tzinfo=JST).timestamp())
P2 = int(datetime(2026, 8, 22, 0, 0, tzinfo=JST).timestamp())


def main():
    with open(ALERTS, encoding="utf-8-sig") as f:
        symbols = sorted({r["symbol"] for r in csv.DictReader(f)})
    print(f"{len(symbols)}銘柄の5分足を取得します")

    data, errors = {}, []
    for i, sym in enumerate(symbols, 1):
        url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}.T"
               f"?period1={P1}&period2={P2}&interval=5m")
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            with urllib.request.urlopen(req, timeout=25) as resp:
                j = json.load(resp)
            res = j["chart"]["result"][0]
            q = res["indicators"]["quote"][0]
            data[sym] = {"ts": res["timestamp"], "open": q["open"],
                         "high": q["high"], "low": q["low"], "close": q["close"]}
            if i % 10 == 0 or i == len(symbols):
                print(f"  [{i}/{len(symbols)}] 取得中… 直近 {sym}: "
                      f"{len(res['timestamp'])}本")
        except Exception as e:
            errors.append((sym, str(e)[:60]))
        time.sleep(0.5)

    with open(OUT, "w") as f:
        json.dump(data, f)
    print(f"保存: {len(data)}銘柄 -> {OUT}")
    if errors:
        print(f"取得できなかった銘柄 {len(errors)}件:",
              ", ".join(s for s, _ in errors[:12]))


if __name__ == "__main__":
    main()
