# -*- coding: utf-8 -*-
"""有償ストックオプション付与の発表と、その前後の株価の関係を検証する。

仮説（ユーザー提示）: 発表以前半年間で一定程度以上下落していた銘柄＋業績条件などの
条件があれば、発表後3〜6ヶ月のリターンに期待が持てるのではないか。

データソース:
  - 適時開示（TDnet）: 非公式ミラーAPI「やのしんTDnet WEB-API」
    (https://webapi.yanoshin.jp/tdnet/) をキーワード検索なしで日付範囲取得し、
    タイトルに「有償」と「ストックオプション」を含むものをクライアント側で抽出。
    2015年以降のデータが取得できることを確認済み（本スクリプトはCLAUDE.mdの方針どおり
    kabuステーションAPIとは無関係の外部公開情報のみを使い、発注・口座には一切触れない）。
  - 株価: Yahoo Finance 日足（無料・口座には触れない）。5分足と違い長期間遡れる。

先読みの排除: 「発表前6ヶ月の下落率」は発表日**より前**の終値だけを使う。
発表後リターンは発表日の**翌営業日始値**を基準にする（発表日当日の値動きは
まだ買えないため）。

実行:
    python analysis/research_paid_so.py --refetch-tdnet   TDnetから開示を取り直す
    python analysis/research_paid_so.py                    キャッシュ済みを使う
出力:
    analysis/output/research/paid_so_events.csv
    analysis/output/research/paid_so_report.md
"""
import argparse
import json
import os
import re
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone
from math import comb

BASE = os.path.dirname(os.path.abspath(__file__))
OUTDIR = os.path.join(BASE, "output", "research")
JST = timezone(timedelta(hours=9))

TDNET_CACHE = os.path.join(OUTDIR, "tdnet_paid_so_raw.json")
BARS_CACHE = os.path.join(OUTDIR, "paid_so_daily_bars.json")

TDNET_URL = "https://webapi.yanoshin.jp/webapi/tdnet/list/{rng}.json?limit=20000"

# 「発行に関するお知らせ」＝新規付与の初回announcement。
# 内容確定・訂正・変更・消滅・行使・消却・放棄・割当の経過報告は除外する
# （同一事象の後追い開示であり、独立したイベントではないため）。
EXCLUDE_WORDS = ("内容確定", "内容の確定", "訂正", "変更", "消滅", "行使",
                 "消却", "放棄", "割当に関する内容等確定")


def fetch_tdnet_range(rng):
    req = urllib.request.Request(TDNET_URL.format(rng=rng),
                                  headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def collect_tdnet(start, end, chunk_days=7):
    """日付範囲を分割して開示一覧を集め、有償ストックオプションの初回発行公告だけ残す。"""
    events = []
    d = start
    while d <= end:
        e = min(d + timedelta(days=chunk_days - 1), end)
        rng = f"{d:%Y%m%d}-{e:%Y%m%d}"
        try:
            data = fetch_tdnet_range(rng)
        except Exception as ex:
            print(f"  警告: {rng} 取得失敗 {ex}")
            time.sleep(1)
            d = e + timedelta(days=1)
            continue
        for it in data.get("items", []):
            t = it.get("Tdnet", {})
            title = t.get("title", "")
            if "有償" not in title or "ストックオプション" not in title:
                continue
            if any(w in title for w in EXCLUDE_WORDS):
                continue
            if "発行に関するお知らせ" not in title:
                continue
            events.append({
                "date": t.get("pubdate", "")[:10],
                "code4": t.get("company_code", "")[:4],
                "name": t.get("company_name", ""),
                "title": title,
            })
        d = e + timedelta(days=1)
        time.sleep(0.15)
    # 同一銘柄・同一日の重複（同時複数回号の発行など）は1件に丸める
    seen, out = set(), []
    for ev in events:
        k = (ev["date"], ev["code4"])
        if k in seen:
            continue
        seen.add(k)
        out.append(ev)
    out.sort(key=lambda x: x["date"])
    return out


def fetch_daily_bars(code4, since_ts, until_ts):
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{code4}.T"
           f"?period1={since_ts}&period2={until_ts}&interval=1d")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=25) as resp:
        j = json.load(resp)
    res = j["chart"]["result"][0]
    ts = res["timestamp"]
    q = res["indicators"]["quote"][0]
    closes = q["close"]
    opens = q["open"]
    days = []
    for i, t in enumerate(ts):
        c, o = closes[i], opens[i]
        if c is None or o is None:
            continue
        days.append((datetime.fromtimestamp(t, JST).strftime("%Y-%m-%d"), o, c))
    return days


def binom_p(k, n):
    if n == 0:
        return 1.0
    tail = sum(comb(n, i) for i in range(n + 1)
               if abs(i - n / 2) >= abs(k - n / 2))
    return min(1.0, tail / (2 ** n))


def stats(vals):
    n = len(vals)
    if not n:
        return None
    w = sum(1 for v in vals if v > 0)
    m = sum(vals) / n
    return {"n": n, "wr": w / n * 100, "mean": m, "p": binom_p(w, n)}


def row(label, s):
    if not s:
        return f"| {label} | — | | |"
    star = "*" if s["p"] < 0.05 else ""
    return f"| {label} | {s['n']} | {s['wr']:.1f}%{star} | {s['mean']:+.1f}% |"


HEAD = "| 区分 | 件数 | 勝率 | 平均リターン |\n|---|---:|---:|---:|"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2018-01-01")
    ap.add_argument("--until", default=None)
    ap.add_argument("--refetch-tdnet", action="store_true")
    ap.add_argument("--refetch-bars", action="store_true")
    args = ap.parse_args()
    until = args.until or datetime.now(JST).strftime("%Y-%m-%d")

    os.makedirs(OUTDIR, exist_ok=True)

    if args.refetch_tdnet or not os.path.exists(TDNET_CACHE):
        print(f"[1/3] TDnetから有償ストックオプション開示を収集 "
              f"({args.since} 〜 {until}) …")
        start = datetime.strptime(args.since, "%Y-%m-%d").date()
        end = datetime.strptime(until, "%Y-%m-%d").date()
        events = collect_tdnet(start, end)
        json.dump(events, open(TDNET_CACHE, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
    else:
        print(f"[1/3] キャッシュを使用: {TDNET_CACHE}")
        events = json.load(open(TDNET_CACHE, encoding="utf-8"))
    print(f"      初回発行公告 {len(events)}件"
          f"（{events[0]['date']} 〜 {events[-1]['date']}）" if events else "0件")

    print("[2/3] 日足を取得 …")
    if args.refetch_bars or not os.path.exists(BARS_CACHE):
        codes = sorted({e["code4"] for e in events})
        bars = {}
        for i, code in enumerate(codes, 1):
            # イベント日の前後、余裕をもって前9ヶ月〜後7ヶ月分を取得
            ev_dates = [e["date"] for e in events if e["code4"] == code]
            lo = min(ev_dates)
            hi = max(ev_dates)
            since_ts = int((datetime.strptime(lo, "%Y-%m-%d") - timedelta(days=290))
                           .replace(tzinfo=JST).timestamp())
            until_ts = int((datetime.strptime(hi, "%Y-%m-%d") + timedelta(days=220))
                          .replace(tzinfo=JST).timestamp())
            try:
                bars[code] = fetch_daily_bars(code, since_ts, until_ts)
            except Exception as e:
                print(f"  取得失敗 {code}: {str(e)[:60]}")
            if i % 20 == 0 or i == len(codes):
                print(f"      {i}/{len(codes)}")
            time.sleep(0.4)
        json.dump(bars, open(BARS_CACHE, "w", encoding="utf-8"))
    else:
        bars = json.load(open(BARS_CACHE, encoding="utf-8"))
        bars = {k: [tuple(x) for x in v] for k, v in bars.items()}

    print("[3/3] 前後リターンを計算 …")

    def price_on_or_before(days, d):
        best = None
        for dd, o, c in days:
            if dd <= d:
                best = c
            else:
                break
        return best

    def next_trading_open(days, d):
        for dd, o, c in days:
            if dd > d:
                return o
        return None

    def price_after(days, start_d, calendar_days):
        target = (datetime.strptime(start_d, "%Y-%m-%d")
                  + timedelta(days=calendar_days)).strftime("%Y-%m-%d")
        best = None
        for dd, o, c in days:
            if dd <= target:
                best = c
            else:
                break
        return best

    rows = []
    for ev in events:
        days = bars.get(ev["code4"])
        if not days:
            continue
        d = ev["date"]
        p_now = price_on_or_before(days, d)
        p_6mo_before = price_on_or_before(
            days, (datetime.strptime(d, "%Y-%m-%d") - timedelta(days=182))
            .strftime("%Y-%m-%d"))
        entry = next_trading_open(days, d)
        p_3mo_after = price_after(days, d, 91)
        p_6mo_after = price_after(days, d, 182)
        if None in (p_now, p_6mo_before, entry):
            continue
        pre_ret = (p_now / p_6mo_before - 1) * 100
        row_ = {**ev, "pre_6mo_pct": pre_ret, "entry_open": entry}
        if p_3mo_after is not None:
            row_["fwd_3mo_pct"] = (p_3mo_after / entry - 1) * 100
        if p_6mo_after is not None:
            row_["fwd_6mo_pct"] = (p_6mo_after / entry - 1) * 100
        rows.append(row_)

    csv_path = os.path.join(OUTDIR, "paid_so_events.csv")
    import csv as csv_mod
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv_mod.DictWriter(f, fieldnames=[
            "date", "code4", "name", "title", "pre_6mo_pct", "entry_open",
            "fwd_3mo_pct", "fwd_6mo_pct"])
        w.writeheader()
        w.writerows(rows)

    out = ["# 有償ストックオプション付与と前後リターン", "",
           f"開示件数（初回発行公告・重複除去後）: {len(events)}件 "
           f"（{args.since} 〜 {until}）",
           f"株価が取得できて分析に使えたもの: {len(rows)}件", "",
           "**先読みなし**: 発表前6ヶ月の下落率は発表日以前の終値のみ使用。",
           "発表後リターンは**翌営業日の始値**を基準（発表日当日には買えない）。", ""]

    thresholds = [(None, "全体"), (-10, "6ヶ月で10%以上下落"),
                  (-20, "6ヶ月で20%以上下落"), (-30, "6ヶ月で30%以上下落"),
                  (-40, "6ヶ月で40%以上下落")]
    out += ["## 発表前の下落幅で区切った、発表後リターン", "",
            "### 3ヶ月後", "", HEAD]
    for th, lbl in thresholds:
        vals = [r["fwd_3mo_pct"] for r in rows if "fwd_3mo_pct" in r
                and (th is None or r["pre_6mo_pct"] <= th)]
        out.append(row(lbl, stats(vals)))
    out.append("")
    out += ["### 6ヶ月後", "", HEAD]
    for th, lbl in thresholds:
        vals = [r["fwd_6mo_pct"] for r in rows if "fwd_6mo_pct" in r
                and (th is None or r["pre_6mo_pct"] <= th)]
        out.append(row(lbl, stats(vals)))
    out.append("")

    # 逆側（下落していなかった/上昇していた銘柄）も出す。フィルタが
    # 効いているかを見るため、除外された側の数字も並べて確認する
    out += ["## 比較: 発表前に下落していなかった銘柄（除外された側）", "",
            "### 3ヶ月後", "", HEAD]
    vals = [r["fwd_3mo_pct"] for r in rows if "fwd_3mo_pct" in r
            and r["pre_6mo_pct"] > 0]
    out.append(row("発表前6ヶ月で上昇していた", stats(vals)))
    out.append("")
    out += ["### 6ヶ月後", "", HEAD]
    vals = [r["fwd_6mo_pct"] for r in rows if "fwd_6mo_pct" in r
            and r["pre_6mo_pct"] > 0]
    out.append(row("発表前6ヶ月で上昇していた", stats(vals)))
    out.append("")

    # 銘柄の偏り確認
    from collections import Counter
    c = Counter(r["code4"] for r in rows)
    top5 = c.most_common(5)
    out += ["## 銘柄の偏り", "",
            f"対象{len(rows)}件は{len(c)}銘柄から。上位5銘柄: "
            + " / ".join(f"{code}({n}件)" for code, n in top5), ""]

    # 時期で前半・後半に分けて再現性を見る
    dates_sorted = sorted({r["date"] for r in rows})
    if len(dates_sorted) >= 10:
        mid = dates_sorted[len(dates_sorted) // 2]
        early = [r for r in rows if r["date"] < mid]
        late = [r for r in rows if r["date"] >= mid]
        out += ["## 前半期間・後半期間での再現性（6ヶ月で20%以上下落 → 6ヶ月後）", "",
                HEAD]
        for lbl, sub in (("前半", early), ("後半", late)):
            vals = [r["fwd_6mo_pct"] for r in sub if "fwd_6mo_pct" in r
                    and r["pre_6mo_pct"] <= -20]
            out.append(row(lbl, stats(vals)))
        out.append("")

    path = os.path.join(OUTDIR, "paid_so_report.md")
    open(path, "w", encoding="utf-8", newline="\n").write("\n".join(out) + "\n")
    print(f"\n書き出し: {path}")
    print(f"        {csv_path}")


if __name__ == "__main__":
    main()
