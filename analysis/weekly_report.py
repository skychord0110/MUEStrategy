# -*- coding: utf-8 -*-
"""週次のストラテジー分析を1本にまとめたパイプライン。

これまでは週ごとに parse_alerts_YYYY-MM-DD.py / evaluate_YYYY-MM-DD.py … を
コピーして作っていたが、中身はほぼ同じで差分は日付だけだった。
このスクリプトは日付を引数に取り、同じ処理を何度でも回せるようにしたもの。

やること（数字を出すところまで。解釈は人／AIが上に書く）
  1. 場中ログから仮想売買のトレードを抽出          -> trades_<date>.csv
  2. 週次・戦略別・決済理由別に集計                -> metrics_<date>.md
  3. 場中ログから需給シグナルのアラートを抽出       -> alerts_<date>.csv
  4. アラート銘柄の5分足をYahoo Financeから取得    -> bars_5m_<date>.json
  5. シグナルを「そのとき買っていたら」で検証       -> metrics_<date>.md

実行:
    python analysis/weekly_report.py                  今日の日付で
    python analysis/weekly_report.py --date 2026-08-28
    python analysis/weekly_report.py --no-fetch       株価取得を省く（オフライン）
    python analysis/weekly_report.py --refetch        取得済みでも取り直す

出力先: analysis/output/<date>/

【この処理で踏んだ落とし穴】あとから同じ間違いをしないための注意
  ・重複排除は**群ごと**に行う。全期間で先に1銘柄1件へ潰すと、午前で枠を
    使った銘柄の午後シグナルが消え、「午後だけ見る」検証ができなくなる。
  ・約定はアラートと**同じ足の終値ではなく、次の足の始値**で取る。
    同じ足の終値だと、その足の中の値動きを見てから買ったことになる。
  ・利確と損切りに同じ足で両方触れたら、**損切りを先**とみなす（不利側に倒す）。
  ・接続断（WinError 10061）が多発した日はデータが欠けている。件数を出して
    レポートに明記すること。
"""
import argparse
import csv
import json
import os
import re
import statistics as st
import sys
import time
import urllib.request
from collections import Counter, defaultdict, deque
from datetime import date, datetime, timedelta, timezone
from math import comb

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = os.path.dirname(os.path.abspath(__file__))
LOGDIR = os.path.normpath(os.path.join(BASE, "..", "strategies", "runner", "logs"))
JST = timezone(timedelta(hours=9))

COST = 0.15            # 往復コスト（手数料＋スリッページ）の想定
MIN_PRICE = 500.0      # 自動売買の下限に合わせる
LOOKBACK_DAYS = 32     # Yahooの5分足が遡れる範囲に収まる日数

RE_ENTRY = re.compile(
    r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}),\d+ \[INFO\] "
    r"\[([^\]/]+)/エントリー\] (\d+) \S+: (.+?) を検知、([\d.]+)円で仮想買い")
RE_EXIT = re.compile(
    r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}),\d+ \[INFO\] "
    r"\[([^\]/]+)/決済:([^\]]+)\] (\d+) \S+: 仮想決済 ([\d.]+)円→([\d.]+)円 "
    r"\(([+-][\d.]+)%\)")
RE_DISCONNECT = re.compile(r"WinError 10061|WebSocket切断|接続が拒否")

ALERT_PATS = [
    ("UNDER急増", re.compile(
        r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}),\d+ \[INFO\] "
        r"\[UNDER急増\] (\d+) .*?急増 \(\+(\d+)株, \+([\d.]+)%\)"
        r".*?現在値([\d.]+)円")),
    ("小口売り連続", re.compile(
        r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}),\d+ \[INFO\] "
        r"\[小口売り連続/(\w+)\] (\d+) \S+: 買い気配([\d.]+)円に小口売り(\d+)回連続")),
    ("定期買い集め", re.compile(
        r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}),\d+ \[INFO\] "
        r"\[定期買い集め/(\w+)\] (\d+) \S+: .*?z値 ([\d.]+) / 該当(\d+)件")),
]


# ── 共通の統計 ──────────────────────────────────────────────────────
def binom_p(k, n):
    """勝ちがk回／n回。五分と違うと言えるか（両側二項検定）。"""
    if n == 0:
        return 1.0
    tail = sum(comb(n, i) for i in range(n + 1)
               if abs(i - n / 2) >= abs(k - n / 2))
    return min(1.0, tail / (2 ** n))


def stat_row(label, vals, width=26):
    n = len(vals)
    if not n:
        return f"| {label} | — | | | | |"
    w = sum(1 for v in vals if v > 0)
    mean = sum(vals) / n
    star = "*" if binom_p(w, n) < 0.05 else ""
    return (f"| {label} | {n} | {w / n * 100:.1f}%{star} | {mean:+.2f}% | "
            f"{mean - COST:+.2f}% | {sum(vals):+.1f}% |")


HEADER = ("| 区分 | 件数 | 勝率 | 平均 | 期待値 | 累計 |\n"
          "|---|---:|---:|---:|---:|---:|")


def week_of(d):
    y, m, dd = (int(v) for v in d.split("-"))
    iso = date(y, m, dd).isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


# ── 1. 仮想売買の抽出 ───────────────────────────────────────────────
def parse_trades(exclude_if_estimated=False):
    """exclude_if_estimated=True で、接続断時にログへ直接追記した『もし稼働して
    いたら』の推計トレード（トリガーに [IF補完] が付く）を除外する。
    実約定・実ログ由来のトレードだけと、推計込みの両方を見比べたい週に使う。
    """
    trades, open_pos = [], defaultdict(deque)
    disconnects = Counter()
    for name in sorted(os.listdir(LOGDIR)):
        if not (name.startswith("runner_") and name.endswith(".log")):
            continue
        with open(os.path.join(LOGDIR, name), encoding="utf-8",
                  errors="replace") as f:
            for line in f:
                if RE_DISCONNECT.search(line):
                    disconnects[name[7:17]] += 1
                    continue
                m = RE_ENTRY.match(line)
                if m:
                    d, t, strat, sym, trig, px = m.groups()
                    if exclude_if_estimated and "[IF補完]" in trig:
                        continue
                    open_pos[(d, strat, sym)].append(
                        {"date": d, "entry_time": t, "strategy": strat,
                         "symbol": sym, "trigger": trig, "entry_px": float(px)})
                    continue
                m = RE_EXIT.match(line)
                if not m:
                    continue
                d, t, strat, why, sym, _epx, xpx, pct = m.groups()
                q = open_pos.get((d, strat, sym))
                if not q:
                    continue
                rec = q.popleft()
                rec.update(exit_time=t, exit_reason=why, exit_px=float(xpx),
                           pct=float(pct), week=week_of(d))
                trades.append(rec)
    trades.sort(key=lambda r: (r["date"], r["entry_time"]))
    return trades, disconnects


def write_trades(trades, outdir, tag):
    cols = ["date", "entry_time", "exit_time", "strategy", "symbol", "trigger",
            "entry_px", "exit_px", "pct", "exit_reason", "week"]
    path = os.path.join(outdir, f"trades_{tag}.csv")
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(trades)
    return path


# ── 2. 仮想売買の集計 ───────────────────────────────────────────────
def drawdown(rows):
    peak = cum = worst = 0.0
    for r in sorted(rows, key=lambda x: (x["date"], x["entry_time"])):
        cum += r["pct"] - COST
        peak = max(peak, cum)
        worst = min(worst, cum - peak)
    return worst, cum


def summarize_trades(trades, disconnects, out):
    p = lambda s="": out.append(s)          # noqa: E731
    weeks = sorted({t["week"] for t in trades})
    last = weeks[-1] if weeks else "—"
    pv = lambda rows: [r["pct"] for r in rows]      # noqa: E731

    p(f"## 仮想売買の成績（{len(trades)}件 / {len(weeks)}週 / コスト{COST}%控除）")
    p()
    p(f"### 今週 {last}")
    p(HEADER)
    tw = [r for r in trades if r["week"] == last]
    p(stat_row("**全戦略**", pv(tw)))
    for s in sorted({r["strategy"] for r in tw}):
        p(stat_row(s, pv([r for r in tw if r["strategy"] == s])))
    p()
    p("### 累計")
    p(HEADER)
    p(stat_row("**全戦略**", pv(trades)))
    for s in sorted({r["strategy"] for r in trades}):
        p(stat_row(s, pv([r for r in trades if r["strategy"] == s])))
    p()
    p("### 週ごとの推移")
    p(HEADER)
    for w in weeks:
        p(stat_row(w, pv([r for r in trades if r["week"] == w])))
    p()
    p("### 決済理由")
    p(HEADER)
    for why in sorted({r["exit_reason"] for r in trades}):
        p(stat_row(why, pv([r for r in trades if r["exit_reason"] == why])))
    p()
    p("### 検知トリガー")
    p(HEADER)
    for t in sorted({r["trigger"] for r in trades}):
        p(stat_row(t, pv([r for r in trades if r["trigger"] == t])))
    p()
    p("### 資金曲線")
    p("| 戦略 | 累積(コスト後) | 最大ドローダウン |")
    p("|---|---:|---:|")
    for s in [None] + sorted({r["strategy"] for r in trades}):
        sub = trades if s is None else [r for r in trades if r["strategy"] == s]
        if len(sub) < 5:
            continue
        dd, cum = drawdown(sub)
        p(f"| {s or '全戦略'} | {cum:+.1f}% | {dd:.1f}% |")
    if disconnects:
        p()
        p("### 接続断（この日のデータは欠けている可能性）")
        p("| 日付 | 件数 |")
        p("|---|---:|")
        for d, n in sorted(disconnects.items()):
            if n >= 5:
                p(f"| {d} | {n} |")


# ── 3. アラートの抽出 ───────────────────────────────────────────────
def parse_alerts(since):
    rows = []
    for name in sorted(os.listdir(LOGDIR)):
        if not (name.startswith("runner_") and name.endswith(".log")):
            continue
        if name[7:17] < since:
            continue
        with open(os.path.join(LOGDIR, name), encoding="utf-8",
                  errors="replace") as f:
            for line in f:
                for kind, pat in ALERT_PATS:
                    m = pat.match(line)
                    if not m:
                        continue
                    g = m.groups()
                    if kind == "UNDER急増":
                        # g = 日付,時刻,銘柄,増加株数,増加率,現在値
                        rows.append({"kind": kind, "level": "", "date": g[0],
                                     "time": g[1], "symbol": g[2],
                                     "price": g[5], "v1": g[4]})
                    else:
                        # g = 日付,時刻,水準,銘柄,価格またはz値,件数
                        rows.append({"kind": kind, "level": g[2], "date": g[0],
                                     "time": g[1], "symbol": g[3],
                                     "price": g[4] if kind == "小口売り連続" else "",
                                     "v1": g[4] if kind == "定期買い集め" else g[5]})
                    break
    return rows


def write_alerts(rows, outdir, tag):
    path = os.path.join(outdir, f"alerts_{tag}.csv")
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["kind", "level", "date", "time",
                                          "symbol", "price", "v1"])
        w.writeheader()
        w.writerows(rows)
    return path


# ── 4. 株価の取得 ───────────────────────────────────────────────────
def fetch_bars(symbols, since, until, path, log=print):
    """Yahoo Financeの5分足。公開株価を読むだけで口座には触れない。"""
    p1 = int(datetime.strptime(since, "%Y-%m-%d").replace(tzinfo=JST).timestamp())
    p2 = int((datetime.strptime(until, "%Y-%m-%d").replace(tzinfo=JST)
              + timedelta(days=1)).timestamp())
    data, errors = {}, []
    for i, sym in enumerate(symbols, 1):
        url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}.T"
               f"?period1={p1}&period2={p2}&interval=5m")
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            with urllib.request.urlopen(req, timeout=25) as resp:
                j = json.load(resp)
            res = j["chart"]["result"][0]
            q = res["indicators"]["quote"][0]
            data[sym] = {"ts": res["timestamp"], "open": q["open"],
                         "high": q["high"], "low": q["low"], "close": q["close"]}
        except Exception as e:
            errors.append((sym, str(e)[:50]))
        if i % 20 == 0 or i == len(symbols):
            log(f"    株価取得 {i}/{len(symbols)}")
        time.sleep(0.5)
    with open(path, "w") as f:
        json.dump(data, f)
    return data, errors


def load_bars(path):
    with open(path) as f:
        raw = json.load(f)
    bars = defaultdict(lambda: defaultdict(list))
    for sym, d in raw.items():
        for i, ts in enumerate(d["ts"]):
            o, h, l, c = d["open"][i], d["high"][i], d["low"][i], d["close"][i]
            if None in (o, h, l, c):
                continue
            t = datetime.fromtimestamp(ts, JST)
            bars[sym][t.strftime("%Y-%m-%d")].append((t, o, h, l, c))
    for sym in bars:
        for day in bars[sym]:
            bars[sym][day].sort()
    return bars


# ── 5. シグナルの検証 ───────────────────────────────────────────────
def simulate(day_bars, i0, tp, sl):
    """i0の足の始値で買い、損切り→利確→大引けの順に判定する。"""
    entry = day_bars[i0][1]
    if entry <= 0:
        return None
    for _, o, h, l, c in day_bars[i0:]:
        if sl is not None and l <= entry * (1 - sl / 100):
            return -sl
        if tp is not None and h >= entry * (1 + tp / 100):
            return tp
    return (day_bars[-1][4] / entry - 1) * 100


def horizon(day_bars, i0, minutes):
    entry = day_bars[i0][1]
    limit = day_bars[i0][0] + timedelta(minutes=minutes)
    px = day_bars[-1][4]
    for t, o, h, l, c in day_bars[i0:]:
        if t >= limit:
            px = c
            break
    return (px / entry - 1) * 100


def build_candidates(alerts, bars):
    """アラートに「直後の足」を紐づける。ここでは重複を落とさない。"""
    out, skipped = [], Counter()
    for a in alerts:
        db = bars.get(a["symbol"], {}).get(a["date"])
        if not db:
            skipped["5分足なし"] += 1
            continue
        t = datetime.strptime(f"{a['date']} {a['time']}",
                              "%Y-%m-%d %H:%M:%S").replace(tzinfo=JST)
        i0 = next((i for i, b in enumerate(db) if b[0] > t), None)
        if i0 is None or i0 >= len(db) - 1:
            skipped["引け間際で次の足なし"] += 1
            continue
        if db[i0][1] < MIN_PRICE:
            skipped[f"{MIN_PRICE:.0f}円未満"] += 1
            continue
        out.append({"a": a, "db": db, "i0": i0, "hour": int(a["time"][:2])})
    return out, skipped


def evaluate_signals(cands, skipped, out):
    p = lambda s="": out.append(s)          # noqa: E731

    def sub(pred):
        """条件に合う候補を、その日その銘柄の最初の1件だけに絞る。

        絞り込みは必ず条件を適用した**あと**に行う。先に全期間で潰すと、
        午前で枠を使った銘柄の午後シグナルが消えてしまう。
        """
        seen, res = set(), []
        for e in cands:
            if not pred(e):
                continue
            k = (e["a"]["date"], e["a"]["symbol"], e["a"]["kind"], e["a"]["level"])
            if k in seen:
                continue
            seen.add(k)
            res.append(e)
        return res

    groups = [
        ("UNDER急増 全体", lambda e: e["a"]["kind"] == "UNDER急増"),
        ("UNDER急増 午前(〜12時)",
         lambda e: e["a"]["kind"] == "UNDER急増" and e["hour"] < 12),
        ("UNDER急増 午後(13時〜)",
         lambda e: e["a"]["kind"] == "UNDER急増" and e["hour"] >= 13),
        ("小口売り連続 STRONG",
         lambda e: e["a"]["kind"] == "小口売り連続" and e["a"]["level"] == "STRONG"),
        ("小口売り連続 WATCH",
         lambda e: e["a"]["kind"] == "小口売り連続" and e["a"]["level"] == "WATCH"),
        ("定期買い集め STRONG",
         lambda e: e["a"]["kind"] == "定期買い集め" and e["a"]["level"] == "STRONG"),
        ("定期買い集め WATCH",
         lambda e: e["a"]["kind"] == "定期買い集め" and e["a"]["level"] == "WATCH"),
    ]
    resolved = [(name, sub(pred)) for name, pred in groups]

    p("## 需給シグナルの検証（Yahoo 5分足・先読みなし）")
    p()
    p(f"候補 {len(cands)}件。除外: "
      + " / ".join(f"{k} {v}件" for k, v in skipped.items()))
    p()
    for lbl, mins in (("15分後", 15), ("30分後", 30), ("60分後", 60),
                      ("大引け", 600)):
        p(f"### {lbl}に決済")
        p(HEADER)
        for name, es in resolved:
            p(stat_row(name, [horizon(e["db"], e["i0"], mins) for e in es]))
        p()
    for tp, sl in ((2.0, 1.0), (2.0, 2.0), (1.5, 1.0), (3.0, 1.5), (None, 2.0)):
        title = (f"利確+{tp}% / 損切り-{sl}%" if tp else f"利確なし / 損切り-{sl}%")
        p(f"### {title}")
        p(HEADER)
        for name, es in resolved:
            vals = [v for v in (simulate(e["db"], e["i0"], tp, sl) for e in es)
                    if v is not None]
            p(stat_row(name, vals))
        p()
    p("### UNDER急増を時間帯で刻む（利確+2%/損切り-2%）")
    p(HEADER)
    for lo, hi, lbl in ((9, 10, "09時台"), (10, 11, "10時台"), (11, 13, "11-12時"),
                        (13, 14, "13時台"), (14, 15, "14時台"), (15, 16, "15時台")):
        es = sub(lambda e, lo=lo, hi=hi: e["a"]["kind"] == "UNDER急増"
                 and lo <= e["hour"] < hi)
        vals = [v for v in (simulate(e["db"], e["i0"], 2.0, 2.0) for e in es)
                if v is not None]
        p(stat_row(lbl, vals))
    p()
    p("### 銘柄の偏り（少数銘柄に集中していないかの確認）")
    p("| シグナル | 件数 | 銘柄数 | 上位3銘柄の占有 |")
    p("|---|---:|---:|---:|")
    for name, es in resolved:
        if not es:
            continue
        c = Counter(e["a"]["symbol"] for e in es)
        top3 = sum(n for _, n in c.most_common(3))
        p(f"| {name} | {len(es)} | {len(c)} | {top3 / len(es) * 100:.0f}% |")


# ── 実行 ────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="週次のストラテジー分析")
    ap.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"),
                    help="レポートの日付（出力ディレクトリ名になる）")
    ap.add_argument("--no-fetch", action="store_true", help="株価を取得しない")
    ap.add_argument("--refetch", action="store_true", help="取得済みでも取り直す")
    ap.add_argument("--lookback", type=int, default=LOOKBACK_DAYS,
                    help="シグナル検証で遡る日数（Yahooの5分足は約60日まで）")
    ap.add_argument("--exclude-if-estimated", action="store_true",
                    help="接続断のif推計トレード（トリガーに[IF補完]）を除いて集計する")
    ap.add_argument("--out-tag", default=None,
                    help="出力ディレクトリ/ファイル名に使うタグ（省略時は--date）。"
                         "同じ--dateで条件違いの2パターンを別々に出したいときに使う")
    args = ap.parse_args()

    tag = args.out_tag or args.date
    outdir = os.path.join(BASE, "output", tag)
    os.makedirs(outdir, exist_ok=True)
    since = (datetime.strptime(args.date, "%Y-%m-%d")
             - timedelta(days=args.lookback)).strftime("%Y-%m-%d")
    out = [f"# 週次メトリクス {tag}", "",
           "このファイルは `analysis/weekly_report.py` が機械的に出した**数字だけ**。",
           "解釈・提案は `analysis_result_" + tag + ".md` に人が書く。", ""]
    if args.exclude_if_estimated:
        out.append("_接続断のif推計トレード（[IF補完]）を除外して集計。"
                    "実約定・実ログ由来のトレードのみ。_")
        out.append("")

    print(f"[1/5] 仮想売買の抽出 …")
    trades, disc = parse_trades(exclude_if_estimated=args.exclude_if_estimated)
    print(f"      {len(trades)}件 -> {write_trades(trades, outdir, tag)}")

    print(f"[2/5] 集計 …")
    if trades:
        summarize_trades(trades, disc, out)
    else:
        out.append("仮想売買のトレードが1件も見つからなかった。")
    out.append("")

    print(f"[3/5] アラートの抽出（{since} 以降）…")
    alerts = parse_alerts(since)
    symbols = sorted({a["symbol"] for a in alerts})
    print(f"      {len(alerts)}件 / {len(symbols)}銘柄 "
          f"-> {write_alerts(alerts, outdir, tag)}")

    bars_path = os.path.join(outdir, f"bars_5m_{tag}.json")
    if args.no_fetch and not os.path.exists(bars_path):
        print("[4/5] 株価取得を省略。シグナル検証はできないので飛ばす")
        out.append("_株価を取得していないため、シグナル検証は未実施。_")
    else:
        if os.path.exists(bars_path) and not args.refetch:
            print(f"[4/5] 取得済みを使う: {bars_path}")
        else:
            print(f"[4/5] 5分足を取得（{len(symbols)}銘柄）…")
            _, errs = fetch_bars(symbols, since, args.date, bars_path)
            if errs:
                print(f"      取得できず {len(errs)}件: "
                      + ", ".join(s for s, _ in errs[:8]))
        print("[5/5] シグナルの検証 …")
        cands, skipped = build_candidates(alerts, load_bars(bars_path))
        evaluate_signals(cands, skipped, out)

    path = os.path.join(outdir, f"metrics_{tag}.md")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(out) + "\n")
    print(f"\n数字を書き出しました: {path}")
    print("この上に解釈と提案を書いて analysis_result_%s.md にすること。" % tag)


if __name__ == "__main__":
    main()
