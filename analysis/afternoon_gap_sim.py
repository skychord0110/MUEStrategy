"""接続断で欠損した午後セッションの「もしrunnerが動いていたら」if推計。

背景（2026-09-03の事例）:
  runnerは09:00〜13:04:34まで接続され正常に検知・仮想売買を記録していたが、
  13:04:34にkabuステーションが落ち（WinError 10054、PCメモリ不足）、以降大引けまで
  再接続できず板PUSHを1件も受信しなかった。そのため13:04以降のストラテジー検知・
  仮想売買ログが存在しない。

このスクリプトがやること / やらないこと:
  - 午後の板シグナル（UNDER急増・投げ売り）は受信されておらず、kabuステーションAPIに
    板/歩み値の遡及取得は無い。→ 実際のシグナルは復元できない。
  - 代わりに Yahoo Finance の5分足（既存の週次検証と同じ参照データ・口座に触れない）で
    午後の値動きを取得し、板トリガーの「価格代理」として **安値圏タッチ**（現在値が当日安値の
    low_zone_pct 以内）を用いてエントリーを近似する。UNDER急増は「下値に大口買い＝安値圏」で
    出るため、安値圏への到達を代理トリガーにする。
  - 決済は実戦略と同じルール: 損切り-SL% / 利確+TP% / 残りは大引け(15:30)。
    先読み排除のため約定は常に「次の5分足の始値/その後の高安」。同一足でSLとTPの両方に
    触れたらSL優先（不利側）。往復コスト0.15%を控除した純期待値で集計する。

  → 出てくる件数・勝率・期待値は「代理トリガーによる推計」であり、実ライブとは必ずずれる
    （実検知はOVER/UNDERの不均衡も見るため代理より選別的）。レポートに但し書きを必ず残す。

使い方:
  python analysis/afternoon_gap_sim.py --date 2026-09-03 --outage-from 13:05
  株価は取得済みなら再利用。--refetch で取り直し、--no-fetch でオフライン。
"""
import argparse
import csv
import datetime
import json
import os
import re
import urllib.request

import yaml

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SYMBOLS_YAML = os.path.join(BASE, "strategies", "symbols.yaml")
RUNNER_CFG = os.path.join(BASE, "strategies", "runner", "config.yaml")
LOG_DIR = os.path.join(BASE, "strategies", "runner", "logs")
JST = datetime.timezone(datetime.timedelta(hours=9))
CLOSE_TIME = datetime.time(15, 30)
COST_PCT = 0.15  # 往復コスト（週次分析と同じ仮定）


def load_symbols():
    data = yaml.safe_load(open(SYMBOLS_YAML, encoding="utf-8"))
    out = []
    for s in data["symbols"]:
        # コメントに銘柄名が入っている場合があるが確実でないのでコードのみ保持
        out.append(str(s["symbol"]))
    return out


def load_cfg():
    c = yaml.safe_load(open(RUNNER_CFG, encoding="utf-8"))["strategies"]
    return c


def fetch_bars(sym):
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}.T"
           f"?range=1d&interval=5m")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=25) as resp:
        d = json.load(resp)
    res = d["chart"]["result"][0]
    ts = res["timestamp"]
    q = res["indicators"]["quote"][0]
    rows = []
    for i, t in enumerate(ts):
        o, h, l, c, v = (q["open"][i], q["high"][i], q["low"][i],
                         q["close"][i], q["volume"][i])
        if None in (o, h, l, c):
            continue
        dt = datetime.datetime.fromtimestamp(t, JST)
        rows.append({"time": dt.strftime("%H:%M"), "o": o, "h": h,
                     "l": l, "c": c, "v": v or 0})
    return rows


def get_all_bars(symbols, date, refetch, no_fetch, outdir):
    path = os.path.join(outdir, f"bars_5m_{date}.json")
    if os.path.exists(path) and not refetch:
        return json.load(open(path, encoding="utf-8"))
    if no_fetch:
        raise SystemExit("株価キャッシュが無く --no-fetch 指定です")
    bars = {}
    for s in symbols:
        try:
            bars[s] = fetch_bars(s)
        except Exception as e:
            print(f"  取得失敗 {s}: {type(e).__name__} {str(e)[:80]}")
            bars[s] = []
    json.dump(bars, open(path, "w", encoding="utf-8"), ensure_ascii=False)
    return bars


# ── 午前ログからの実績抽出（文脈・レポート用） ──
def parse_morning(date):
    path = os.path.join(LOG_DIR, f"runner_{date}.log")
    text = open(path, encoding="utf-8", errors="replace").read()
    under = {}
    for m in re.finditer(r"\[UNDER急増\] (\d+) ", text):
        under[m.group(1)] = under.get(m.group(1), 0) + 1
    entries = re.findall(r"\[AI[^\]]*エントリー\][^\n]*", text)
    exits = re.findall(r"\[AI[^\]]*決済[^\]]*\][^\n]*", text)
    # 切断・復旧イベント
    disc = re.findall(r"(\d\d:\d\d:\d\d)[^\n]*WebSocket切断", text)
    return {"under": under, "entries": entries, "exits": exits,
            "n_under": sum(under.values()), "text": text}


# ── if推計: 安値圏タッチを板トリガーの代理にした引け戻り系 ──
def hhmm_to_time(s):
    h, m = map(int, s.split(":"))
    return datetime.time(h, m)


def simulate(bars, symbols, cfg, outage_from):
    """afternoon_reversal / ranked / vwap_discount_reversal を価格代理でif推計。"""
    low_zone = cfg["under_surge_detector"].get("low_zone_pct", 0.01)
    ranks = {s: i for i, s in enumerate(symbols, start=1)}
    outage_t = hhmm_to_time(outage_from)

    def run(name, sl, tp, min_price, entry_start, entry_end,
            need_discount=None, ranked=False, top_rank=25,
            late_after=datetime.time(14, 0)):
        trades = []
        for sym in symbols:
            rows = bars.get(sym, [])
            if not rows:
                continue
            day_low = None
            vwap_num = vwap_den = 0.0
            entered = None  # (idx, entry_price)
            for i, b in enumerate(rows):
                t = hhmm_to_time(b["time"])
                # 当日安値・VWAPを時系列に更新（先読みしない）
                day_low = b["l"] if day_low is None else min(day_low, b["l"])
                tp_price_typ = (b["h"] + b["l"] + b["c"]) / 3
                vwap_num += tp_price_typ * b["v"]
                vwap_den += b["v"]
                vwap = vwap_num / vwap_den if vwap_den else b["c"]

                if entered is None:
                    # エントリー判定はこの足まで。約定は次足始値（先読み排除）
                    if t < outage_t or not (entry_start <= t <= entry_end):
                        continue
                    if i + 1 >= len(rows):
                        continue
                    # 安値圏タッチ = この足の安値が当日安値の low_zone 以内
                    if b["l"] > day_low * (1 + low_zone):
                        continue
                    if ranked:
                        r = ranks.get(sym, 9999)
                        if r > top_rank and t < late_after:
                            continue
                    entry_price = rows[i + 1]["o"]
                    if entry_price < min_price:
                        continue
                    if need_discount is not None:
                        # 現在値がVWAPを need_discount% 以上下回っているか
                        if entry_price > vwap * (1 - need_discount / 100):
                            continue
                    entered = (i + 1, entry_price)
                    continue
                # 保有中: 次足以降で決済判定
                ei, ep = entered
                if i <= ei:
                    continue
                sl_price = ep * (1 - sl / 100)
                tp_price = ep * (1 + tp / 100)
                reason = exit_price = None
                if b["l"] <= sl_price:          # 同一足はSL優先（不利側）
                    reason, exit_price = "損切り", sl_price
                elif tp is not None and b["h"] >= tp_price:
                    reason, exit_price = "利確", tp_price
                elif t >= CLOSE_TIME:
                    reason, exit_price = "大引け", b["c"]
                if reason:
                    gross = (exit_price - ep) / ep * 100
                    trades.append({"strategy": name, "symbol": sym,
                                   "entry_time": rows[ei]["time"],
                                   "entry": round(ep, 1),
                                   "exit_time": b["time"],
                                   "exit": round(exit_price, 1),
                                   "reason": reason,
                                   "gross_pct": round(gross, 2),
                                   "net_pct": round(gross - COST_PCT, 2)})
                    entered = None
                    break
            # 未決済のまま最終足まで来たら大引け（entry_end後に安値圏未達で未エントリーは対象外）
            if entered is not None:
                ei, ep = entered
                last = rows[-1]
                gross = (last["c"] - ep) / ep * 100
                trades.append({"strategy": name, "symbol": sym,
                               "entry_time": rows[ei]["time"], "entry": round(ep, 1),
                               "exit_time": last["time"], "exit": round(last["c"], 1),
                               "reason": "大引け", "gross_pct": round(gross, 2),
                               "net_pct": round(gross - COST_PCT, 2)})
        return trades

    results = {}
    ar = cfg["afternoon_reversal"]
    results["afternoon_reversal"] = run(
        "afternoon_reversal", ar["stop_loss_pct"], ar["take_profit_pct"],
        ar["min_entry_price"], hhmm_to_time(ar["entry_start"]),
        hhmm_to_time(ar["entry_end"]))
    rk = cfg["afternoon_reversal_ranked"]
    results["afternoon_reversal_ranked"] = run(
        "afternoon_reversal_ranked", rk["stop_loss_pct"], rk["take_profit_pct"],
        rk["min_entry_price"], hhmm_to_time(rk["entry_start"]),
        hhmm_to_time(rk["entry_end"]), ranked=True,
        top_rank=rk.get("top_rank", 25),
        late_after=hhmm_to_time(rk.get("late_entry_after", "14:00")))
    vd = cfg["vwap_discount_reversal"]
    results["vwap_discount_reversal"] = run(
        "vwap_discount_reversal", vd["stop_loss_pct"], vd["take_profit_pct"],
        vd["min_entry_price"], hhmm_to_time(vd["entry_start"]),
        hhmm_to_time(vd["entry_end"]), need_discount=vd.get("min_discount_pct", 1.0))
    return results


def afternoon_tape(bars, symbols, outage_from):
    """午後（outage以降）の値動きサマリを銘柄別に返す。"""
    outage_t = hhmm_to_time(outage_from)
    out = []
    for sym in symbols:
        rows = [b for b in bars.get(sym, [])]
        if not rows:
            continue
        aft = [b for b in rows if hhmm_to_time(b["time"]) >= outage_t]
        if not aft:
            continue
        p0 = aft[0]["o"]
        lo = min(b["l"] for b in aft)
        hi = max(b["h"] for b in aft)
        cl = aft[-1]["c"]
        out.append({"symbol": sym, "p_resume": p0, "aft_low": lo,
                    "aft_high": hi, "close": cl,
                    "chg_pct": round((cl - p0) / p0 * 100, 2),
                    "drawup_pct": round((hi - p0) / p0 * 100, 2),
                    "drawdown_pct": round((lo - p0) / p0 * 100, 2)})
    return out


# 表示名（strategies/runner/src/notifier.py と一致させる。週次はこの名前でバケット化する）
DISPLAY = {
    "afternoon_reversal": "AI午後引け戻り",
    "afternoon_reversal_ranked": "AI午後引け戻り(順位優先)",
    "vwap_discount_reversal": "AI VWAP乖離反発",
}
IF_TRIGGER = {
    "afternoon_reversal": "UNDER急増[IF補完]",
    "afternoon_reversal_ranked": "UNDER急増[IF補完]",
    "vwap_discount_reversal": "UNDER急増+VWAP乖離[IF補完]",
}


def _build_if_lines(results, date, outage_from):
    """週次集計(weekly_report.py)が拾う仮想売買行を生成して返す。

    - 形式は notifier.py の実出力に一致（RE_ENTRY / RE_EXIT でパースされる）。
    - 実約定と区別できるよう、全トレード行のトリガーに [IF補完] を付与し、
      前後に # 注記ブロックを挟む（# 行はタイムスタンプ無しのため集計対象外）。
    """
    lines = [
        f"# ==== ここから 接続断で欠損した午後({outage_from}〜大引け) の "
        f"『もし稼働していたら』if推計（実約定ではない） ====",
        "# 板シグナルは復元不能のため、安値圏タッチを板トリガーの価格代理として推計。",
        "# 全トレード行のトリガーに [IF補完] を付与。往復コストは週次側で控除。",
        f"# 生成: analysis/afternoon_gap_sim.py --date {date} --emit-log / 詳細は "
        f"analysis/output/{date}/afternoon_gap_analysis_{date}.md",
    ]
    n = 0
    for key in ("afternoon_reversal", "afternoon_reversal_ranked",
                "vwap_discount_reversal"):
        disp = DISPLAY[key]
        trig = IF_TRIGGER[key]
        for t in sorted(results.get(key, []), key=lambda r: r["entry_time"]):
            sym = t["symbol"]
            et = f"{t['entry_time']}:00,000"
            xt = f"{t['exit_time']}:00,000"
            lines.append(
                f"{date} {et} [INFO] [{disp}/エントリー] {sym} {sym}: "
                f"{trig} を検知、{t['entry']:.1f}円で仮想買い"
                f"（if補完・接続断の推計・発注なし）")
            lines.append(
                f"{date} {xt} [INFO] [{disp}/決済:{t['reason']}] {sym} {sym}: "
                f"仮想決済 {t['entry']:.1f}円→{t['exit']:.1f}円 "
                f"({t['gross_pct']:+.2f}%)")
            n += 1
    lines.append("# ==== ここまで if推計 ====")
    return lines, n


def emit_runner_log(results, date, outage_from):
    """if推計トレードを日次の runner_<date>.log に直接追記する。

    稼働中プロセスが同じファイルに書き続けている可能性があるため、O_APPEND
    （バイナリ追記 'ab'）で末尾へ原子的に足す。既存行の巻き戻し・上書きはしない。
    本ログと同じ UTF-8(BOMなし)・CRLF に揃える。二重追記を避けるため、
    既に [IF補完] マーカーがある場合はスキップする。
    """
    path = os.path.join(LOG_DIR, f"runner_{date}.log")
    if os.path.exists(path):
        with open(path, encoding="utf-8", errors="replace") as f:
            if "[IF補完]" in f.read():
                return path, 0  # 追記済み。重複を避ける
    lines, n = _build_if_lines(results, date, outage_from)
    blob = ("\r\n".join(lines) + "\r\n").encode("utf-8")
    with open(path, "ab") as f:      # 'a' = O_APPEND。末尾追記のみ
        f.write(blob)
    return path, n


def summarize(trades):
    n = len(trades)
    if n == 0:
        return {"n": 0}
    wins = [t for t in trades if t["net_pct"] > 0]
    return {"n": n, "win_pct": round(len(wins) / n * 100, 1),
            "avg_net": round(sum(t["net_pct"] for t in trades) / n, 2),
            "reasons": {r: sum(1 for t in trades if t["reason"] == r)
                        for r in ("利確", "損切り", "大引け")}}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True)
    ap.add_argument("--outage-from", default="13:05")
    ap.add_argument("--refetch", action="store_true")
    ap.add_argument("--no-fetch", action="store_true")
    ap.add_argument("--emit-log", action="store_true",
                    help="週次集計が拾う runner_<date>_if_afternoon.log を書き出す")
    a = ap.parse_args()

    outdir = os.path.join(BASE, "analysis", "output", a.date)
    os.makedirs(outdir, exist_ok=True)

    symbols = load_symbols()
    cfg = load_cfg()
    print(f"銘柄 {len(symbols)}件 / 5分足を取得中…")
    bars = get_all_bars(symbols, a.date, a.refetch, a.no_fetch, outdir)
    got = sum(1 for s in symbols if bars.get(s))
    print(f"  5分足あり {got}/{len(symbols)}銘柄")

    morning = parse_morning(a.date)
    results = simulate(bars, symbols, cfg, a.outage_from)
    tape = afternoon_tape(bars, symbols, a.outage_from)

    # 出力: トレードCSV
    all_trades = [t for ts in results.values() for t in ts]
    with open(os.path.join(outdir, f"if_trades_{a.date}.csv"), "w",
              encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["strategy", "symbol", "entry_time",
                                          "entry", "exit_time", "exit", "reason",
                                          "gross_pct", "net_pct"])
        w.writeheader()
        w.writerows(all_trades)
    # 午後の値動きCSV
    with open(os.path.join(outdir, f"afternoon_tape_{a.date}.csv"), "w",
              encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["symbol", "p_resume", "aft_low",
                                          "aft_high", "close", "chg_pct",
                                          "drawup_pct", "drawdown_pct"])
        w.writeheader()
        w.writerows(tape)

    summ = {k: summarize(v) for k, v in results.items()}
    json.dump({"morning_under_total": morning["n_under"],
               "morning_entries": morning["entries"],
               "morning_exits": morning["exits"],
               "summary": summ},
              open(os.path.join(outdir, f"if_summary_{a.date}.json"), "w",
                   encoding="utf-8"), ensure_ascii=False, indent=2)

    # コンソール要約
    print("\n=== 午前(実績・09:00-13:04) ===")
    print(f"  UNDER急増 総数: {morning['n_under']}件 / 銘柄:{len(morning['under'])}")
    for e in morning["entries"]:
        print("  ENTRY:", e.split("] ", 1)[-1][:70])
    for e in morning["exits"]:
        print("  EXIT :", e.split("] ", 1)[-1][:70])
    print("\n=== 午後 if推計（価格代理トリガー・往復0.15%控除後） ===")
    for k, s in summ.items():
        if s["n"] == 0:
            print(f"  {k}: 該当エントリーなし")
        else:
            print(f"  {k}: n={s['n']} 勝率{s['win_pct']}% 平均{s['avg_net']}% "
                  f"利確/損切/引け={s['reasons']['利確']}/{s['reasons']['損切り']}/{s['reasons']['大引け']}")
    up = sum(1 for t in tape if t["chg_pct"] > 0)
    print(f"\n=== 午後の地合い: {len(tape)}銘柄中 引けが再開値より高い {up}銘柄 "
          f"({round(up/len(tape)*100)}%) ===")
    print(f"\n出力先: {outdir}")

    if a.emit_log:
        lp, ln = emit_runner_log(results, a.date, a.outage_from)
        if ln == 0:
            print(f"\n本ログは既に [IF補完] を含むため追記スキップ: {lp}")
        else:
            print(f"\n本ログに if補完トレードを直接追記: {lp}")
            print(f"  追記 {ln}件（全行に [IF補完] マーカー付き・O_APPENDで末尾追記）")


if __name__ == "__main__":
    main()
