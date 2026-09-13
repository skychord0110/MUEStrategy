# -*- coding: utf-8 -*-
"""現行の監視50銘柄を「流動性の高いデイトレ向き」条件で絞り込む研究スクリプト。

目的（利用者の要望）:
  資金が増えてもマーケットインパクトを受けにくいよう、監視50銘柄のうち
    (1) 時価総額 150億円以上
    (2) 直近5営業日の 日足(高値-安値)/前日終値 の平均が 3%以上
    (3) 直近5営業日すべてで 売買代金(終値×出来高) が 1億円以上
  を満たす銘柄だけを抽出し、その部分集合の規模を確かめる。

これは**読み取り専用の調査**。口座には触れない。株価は Yahoo Finance の日足
（公開値）。時価総額は extracted_stocks/*_export.csv の MCap（¥100M）を使い、
取れないものは「不明」として区別する（ライブでは kabu PUSH の TotalMarketValue が
権威データ。本スクリプトはあくまで事前の当たり付け）。

実行:
    python analysis/research_liquid_universe.py
    python analysis/research_liquid_universe.py --days 5 --mcap 150 --range 3 --turnover 1
出力:
    画面表示のみ（判断材料）。CSV等は作らない。
"""
import argparse
import csv
import glob
import io
import json
import os
import time
import urllib.request
from datetime import datetime, timedelta, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.normpath(os.path.join(BASE, ".."))
JST = timezone(timedelta(hours=9))


def load_symbols():
    import yaml
    path = os.path.join(REPO, "strategies", "symbols.yaml")
    data = yaml.safe_load(io.open(path, encoding="utf-8"))
    out = []
    for s in data["symbols"]:
        out.append((str(s["symbol"]), s.get("name") or ""))
    return out


def load_mcap():
    """extracted_stocks の全exportから ticker -> (MCap億, Name) を集める（新しい値で上書き）。"""
    mcap, name = {}, {}
    for p in sorted(glob.glob(os.path.join(REPO, "extracted_stocks", "*_export.csv"))):
        for r in csv.DictReader(io.open(p, encoding="utf-8-sig")):
            t = str(r.get("Ticker") or "").strip()
            m = r.get("MCap (¥100M)")
            if t and m:
                try:
                    mcap[t] = float(m)
                    name[t] = r.get("Name") or name.get(t, "")
                except ValueError:
                    pass
    return mcap, name


def fetch_daily(sym, days_back=20):
    """Yahoo日足を取得。[(date, high, low, close, volume)] を古い順で返す。"""
    p2 = int((datetime.now(JST) + timedelta(days=1)).timestamp())
    p1 = int((datetime.now(JST) - timedelta(days=days_back + 10)).timestamp())
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}.T"
           f"?period1={p1}&period2={p2}&interval=1d")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=25) as resp:
        j = json.load(resp)
    res = j["chart"]["result"][0]
    ts = res["timestamp"]
    q = res["indicators"]["quote"][0]
    rows = []
    for i, t in enumerate(ts):
        hi, lo, cl, vo = q["high"][i], q["low"][i], q["close"][i], q["volume"][i]
        if None in (hi, lo, cl, vo):
            continue
        d = datetime.fromtimestamp(t, JST).date().isoformat()
        rows.append((d, hi, lo, cl, vo))
    return rows


def screen(rows, n=5):
    """直近n営業日の 高値安値差%平均 と 売買代金(各日) を返す。

    高値安値差% = (高値-安値)/前日終値 * 100。前日終値が要るので n+1 本使う。
    """
    if len(rows) < n + 1:
        return None
    recent = rows[-(n + 1):]
    ranges, turnovers = [], []
    for i in range(1, len(recent)):
        _, hi, lo, cl, vo = recent[i]
        prev_close = recent[i - 1][3]
        if prev_close:
            ranges.append((hi - lo) / prev_close * 100)
        turnovers.append(cl * vo)          # 売買代金（円）
    return {
        "range_avg": sum(ranges) / len(ranges) if ranges else 0.0,
        "turnover_min": min(turnovers) if turnovers else 0.0,
        "turnover_avg": sum(turnovers) / len(turnovers) if turnovers else 0.0,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=5, help="直近何営業日で判定するか")
    ap.add_argument("--mcap", type=float, default=150, help="時価総額の下限（億円）")
    ap.add_argument("--range", type=float, default=3.0, help="日足高安差パーセントの平均下限")
    ap.add_argument("--turnover", type=float, default=1.0, help="売買代金の下限（億円/日）")
    ap.add_argument("--write", action="store_true",
                    help="合格銘柄を strategies/runner/state/liquid_universe.json に書く"
                         "（流動UNDER急増戦略が起動時に読む）")
    args = ap.parse_args()

    symbols = load_symbols()
    mcap, exname = load_mcap()
    oku = 100_000_000  # 1億

    print(f"現行 symbols.yaml: {len(symbols)}銘柄 を判定")
    print(f"条件: 時価総額≥{args.mcap:.0f}億 / 直近{args.days}日の高安差平均≥{args.range}% "
          f"/ 直近{args.days}日すべてで売買代金≥{args.turnover:.0f}億\n")
    print(f"{'コード':<6}{'名称':<16}{'時価総額':>8}{'高安差%':>8}{'売買代金min':>12}"
          f"{'売買代金avg':>12}  判定")
    print("-" * 78)

    passed, unknown_mcap, fetch_err = [], [], []
    for code, yname in symbols:
        nm = (exname.get(code) or yname or "")[:14]
        try:
            rows = fetch_daily(code)
            s = screen(rows, args.days)
        except Exception as e:
            fetch_err.append((code, str(e)[:40]))
            print(f"{code:<6}{nm:<16}{'?':>8}{'取得失敗':>8}")
            time.sleep(0.4)
            continue
        if s is None:
            print(f"{code:<6}{nm:<16}{'?':>8}{'日足不足':>8}")
            time.sleep(0.4)
            continue

        mc = mcap.get(code)
        mc_ok = (mc is not None and mc >= args.mcap)
        rng_ok = s["range_avg"] >= args.range
        tv_ok = s["turnover_min"] >= args.turnover * oku
        if mc is None:
            unknown_mcap.append(code)

        mc_s = f"{mc:.0f}億" if mc is not None else "不明"
        verdict_parts = []
        verdict_parts.append("時価" + ("✓" if mc_ok else ("?" if mc is None else "✗")))
        verdict_parts.append("ボラ" + ("✓" if rng_ok else "✗"))
        verdict_parts.append("代金" + ("✓" if tv_ok else "✗"))
        all_ok = mc_ok and rng_ok and tv_ok
        if all_ok:
            passed.append(code)
        print(f"{code:<6}{nm:<16}{mc_s:>8}{s['range_avg']:>7.1f}%"
              f"{s['turnover_min']/oku:>10.1f}億{s['turnover_avg']/oku:>10.1f}億  "
              f"{'★合格' if all_ok else ' '.join(verdict_parts)}")
        time.sleep(0.4)

    print("-" * 78)
    print(f"\n■ 全条件を満たす銘柄: {len(passed)}件  {passed}")
    if args.write:
        state_dir = os.path.join(REPO, "strategies", "runner", "state")
        os.makedirs(state_dir, exist_ok=True)
        path = os.path.join(state_dir, "liquid_universe.json")
        payload = {
            "date": datetime.now(JST).date().isoformat(),
            "criteria": {"mcap_oku": args.mcap, "range_pct": getattr(args, "range"),
                         "turnover_oku": args.turnover, "days": args.days},
            "symbols": passed,
        }
        tmp = path + ".tmp"
        with io.open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
        print(f"  → {path} に書き出しました（流動UNDER急増戦略が起動時に読む）")
    if unknown_mcap:
        print(f"■ 時価総額が不明で判定保留: {len(unknown_mcap)}件  {unknown_mcap}")
        print("  （ライブでは kabu PUSH の TotalMarketValue で埋められる）")
    if fetch_err:
        print(f"■ 日足取得に失敗: {len(fetch_err)}件  {[c for c, _ in fetch_err]}")


if __name__ == "__main__":
    main()
