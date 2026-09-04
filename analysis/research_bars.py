# -*- coding: utf-8 -*-
"""新シグナルの研究用に、5分足を**出来高込み**で取得してキャッシュする。

週次パイプライン（weekly_report.py）はOHLCしか持っていない。需給を読むには
出来高が要るので、研究用にはこちらを使う。公開株価を読むだけで口座には触れない。

実行:
    python analysis/research_bars.py                 既定のユニバース・期間で取得
    python analysis/research_bars.py --since 2026-07-01 --until 2026-08-28
出力:
    analysis/output/research/bars_ohlcv.json
"""
import argparse
import json
import os
import time
import urllib.request
from datetime import datetime, timedelta, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
OUTDIR = os.path.join(BASE, "output", "research")
JST = timezone(timedelta(hours=9))


def fetch(sym, p1, p2):
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}.T"
           f"?period1={p1}&period2={p2}&interval=5m")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=25) as resp:
        j = json.load(resp)
    res = j["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    return {"ts": res["timestamp"], "open": q["open"], "high": q["high"],
            "low": q["low"], "close": q["close"], "volume": q["volume"]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-07-01")
    ap.add_argument("--until", default="2026-08-28")
    ap.add_argument("--out", default=os.path.join(OUTDIR, "bars_ohlcv.json"))
    a = ap.parse_args()

    os.makedirs(OUTDIR, exist_ok=True)
    symbols = json.load(open(os.path.join(OUTDIR, "universe.json")))

    # Yahooの5分足は「直近60日以内」しか返さない。超えると全銘柄が422で落ちる
    # （実測: 64日を要求して "The requested range must be within the last 60 days"）。
    limit = (datetime.now(JST) - timedelta(days=59)).strftime("%Y-%m-%d")
    if a.since < limit:
        print(f"  5分足は直近60日までのため開始日を {a.since} → {limit} に詰めます")
        a.since = limit

    p1 = int(datetime.strptime(a.since, "%Y-%m-%d").replace(tzinfo=JST).timestamp())
    p2 = int((datetime.strptime(a.until, "%Y-%m-%d").replace(tzinfo=JST)
              + timedelta(days=1)).timestamp())
    print(f"{len(symbols)}銘柄 / {a.since}〜{a.until} を取得します")

    data, errors = {}, []
    for i, s in enumerate(symbols, 1):
        try:
            data[s] = fetch(s, p1, p2)
        except Exception as e:
            errors.append((s, str(e)[:40]))
        if i % 25 == 0 or i == len(symbols):
            print(f"  {i}/{len(symbols)}")
        time.sleep(0.45)

    json.dump(data, open(a.out, "w"))
    days = set()
    for d in data.values():
        for ts in d["ts"]:
            days.add(datetime.fromtimestamp(ts, JST).strftime("%Y-%m-%d"))
    print(f"保存: {len(data)}銘柄 / {len(days)}営業日 -> {a.out}")
    if errors:
        print(f"  取得できず {len(errors)}件: " + ", ".join(s for s, _ in errors[:10]))


if __name__ == "__main__":
    main()
