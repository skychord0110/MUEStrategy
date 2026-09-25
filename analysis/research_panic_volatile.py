# -*- coding: utf-8 -*-
"""投げ売り検知 × ボラ拡大銘柄 × 寄り後の下落 のバックテスト（読み取り専用）。

利用者の構想（2026-09-25）:
  監視銘柄のうち「直近でボラティリティが大きくなっている（急騰・急落を含む）」かつ
  「前日の売買代金が1億円を超える」銘柄を対象に、寄付き後に下落する局面で、
  上に指してあった売り板が
    (B) 下落と共に買い気配へぶつけられる          … 投げ売り検知の DUMP
    (A) 売り気配に指し直されてから食われる          … 投げ売り検知の ABSORBED
  ことで需給が改善したタイミングを買う。利確・損切りの幅は検証で決める。

このスクリプトは、過去ログに残っている投げ売り検知（DUMP / ABSORBED）を
Yahoo Finance の5分足・日足で検証し、上の各条件がどれだけ効くかを測る。

先読みの排除:
  - 約定は「検知の直後の5分足の始値」。検知した足の終値は使わない
  - 同じ足で利確と損切りの両方に触れたら損切りを先とみなす（weekly_report.simulate）
  - 銘柄の絞り込み（ボラ・売買代金）は**前日までの日足だけ**で判定する
  - 1銘柄1日1件の重複排除は、条件を適用した**あと**に行う

口座には触れない。株価は Yahoo Finance の公開値。

実行:
    python analysis/research_panic_volatile.py              株価を取得して検証
    python analysis/research_panic_volatile.py --no-fetch   取得済みの株価で再計算
出力:
    analysis/output/<日付>/panic_volatile_backtest_<日付>.md
    analysis/output/<日付>/bars_5m_panic_<日付>.json / bars_1d_panic_<日付>.json
"""
import argparse
import io
import json
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime, timedelta

BASE = os.path.dirname(os.path.abspath(__file__))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

import weekly_report as wr  # noqa: E402  simulate / fetch_bars / load_bars / binom_p を共用

# 絞り込みの判定はランナー側の共通モジュールを使う（検証と実運用で定義をずらさないため）
_RUNNER_SRC = os.path.normpath(os.path.join(BASE, "..", "strategies", "runner", "src"))
if _RUNNER_SRC not in sys.path:
    sys.path.insert(0, _RUNNER_SRC)
import volatility_screen as vs  # noqa: E402

OKU = 100_000_000
COST = wr.COST

RE_PANIC = re.compile(
    r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}),\d+ \[INFO\] "
    r"\[投げ売り/(買い気配へぶつけ|投げ売り吸収)\] (\d+) \S+: .*?現在値([\d.]+)円")
STAGE = {"買い気配へぶつけ": "DUMP", "投げ売り吸収": "ABSORBED"}


# ── 1. 検知ログの抽出 ────────────────────────────────────────────────
def parse_alerts(since):
    alerts, disc = [], Counter()
    for name in sorted(os.listdir(wr.LOGDIR)):
        if not (name.startswith("runner_") and name.endswith(".log")):
            continue
        day = name[7:17]
        if day < since:
            continue
        with open(os.path.join(wr.LOGDIR, name), encoding="utf-8", errors="replace") as f:
            for line in f:
                if wr.RE_DISCONNECT.search(line):
                    disc[day] += 1
                    continue
                if "IF補完" in line or "後追い再現" in line:
                    continue
                m = RE_PANIC.match(line)
                if m:
                    d, t, lab, sym, px = m.groups()
                    alerts.append({"date": d, "time": t, "stage": STAGE[lab],
                                   "symbol": sym, "price": float(px)})
    return alerts, disc


# ── 2. 日足 ──────────────────────────────────────────────────────────
def fetch_daily(symbols, since, until, path, log=print):
    data, errors = {}, []
    for i, sym in enumerate(symbols, 1):
        try:
            data[sym] = vs.fetch_daily(sym, since, until)
        except Exception as e:
            errors.append((sym, str(e)[:50]))
        if i % 20 == 0 or i == len(symbols):
            log(f"    日足取得 {i}/{len(symbols)}")
        time.sleep(0.4)
    with open(path, "w") as f:
        json.dump(data, f)
    return data, errors


features = vs.features


# ── 3. 候補づくり ────────────────────────────────────────────────────
def build(alerts, bars5, daily):
    cands, skipped = [], Counter()
    for a in alerts:
        db = bars5.get(a["symbol"], {}).get(a["date"])
        if not db:
            skipped["5分足なし"] += 1
            continue
        t = datetime.strptime(f"{a['date']} {a['time']}",
                              "%Y-%m-%d %H:%M:%S").replace(tzinfo=wr.JST)
        i0 = next((i for i, b in enumerate(db) if b[0] > t), None)
        if i0 is None or i0 >= len(db) - 1:
            skipped["引け間際で次の足なし"] += 1
            continue
        day_open = db[0][1]
        f = features(daily.get(a["symbol"]) or [], a["date"])
        cands.append({
            **a, "db": db, "i0": i0, "entry_px": db[i0][1],
            "open_dev": (a["price"] / day_open - 1) * 100 if day_open else 0.0,
            "hour": int(a["time"][:2]), "f": f,
        })
    cands.sort(key=lambda e: (e["date"], e["time"]))
    return cands, skipped


# ── 4. 条件 ──────────────────────────────────────────────────────────
def _need(f):
    return f is not None


UNIVERSES = [
    ("A 制限なし（参考）", lambda f: True),
    ("B 前日代金1億↑のみ", lambda f: _need(f) and f["turnover"] >= OKU),
    ("C 代金＋前日値幅5%↑", lambda f: _need(f) and f["turnover"] >= OKU and f["prev_range"] >= 5),
    ("D 代金＋3日内に±5%↑の急騰落",
     lambda f: _need(f) and f["turnover"] >= OKU and f["maxabsret3"] >= 5),
    ("E 代金＋ボラ拡大(3日/20日≥1.5)",
     lambda f: _need(f) and f["turnover"] >= OKU and f["vol_ratio"] >= 1.5),
    ("F 代金＋5日で±10%↑", lambda f: _need(f) and f["turnover"] >= OKU and abs(f["ret5"]) >= 10),
    ("G 代金＋(急騰落 or ボラ拡大)", lambda f: vs.passes(f)),   # 実運用と同じ判定
]
OPENS = [("寄り比 問わず", None), ("寄り比 0%以下", 0.0),
         ("寄り比 -1%以下", -1.0), ("寄り比 -2%以下", -2.0)]
STAGES = [("両方", {"DUMP", "ABSORBED"}), ("ぶつけ(DUMP)", {"DUMP"}),
          ("吸収(ABSORBED)", {"ABSORBED"})]
EXITS = [(tp, sl) for tp in (1.0, 1.5, 2.0, 3.0, None) for sl in (1.0, 1.5, 2.0, None)]


def select(cands, uni, open_th, stages, min_price=0.0, invert_uni=False):
    out, seen = [], set()
    for e in cands:
        if e["stage"] not in stages:
            continue
        ok = uni(e["f"])
        if invert_uni:
            ok = (not ok) and e["f"] is not None
        if not ok:
            continue
        if open_th is not None and e["open_dev"] > open_th:
            continue
        if e["entry_px"] < min_price:
            continue
        key = (e["date"], e["symbol"])
        if key in seen:
            continue
        seen.add(key)
        out.append(e)
    return out


def rets(sel, tp, sl):
    return [r for r in (wr.simulate(e["db"], e["i0"], tp, sl) for e in sel) if r is not None]


def stat(vals, sel=None):
    n = len(vals)
    if not n:
        return None
    w = sum(1 for v in vals if v > 0)
    ev = sum(vals) / n - COST
    s = {"n": n, "win": w / n * 100, "mean": sum(vals) / n, "ev": ev,
         "cum": ev * n, "p": wr.binom_p(w, n)}
    if sel:
        c = Counter(e["symbol"] for e in sel)
        s["syms"] = len(c)
        s["top3"] = sum(x for _, x in c.most_common(3)) / len(sel) * 100
    return s


def row(label, s):
    if not s:
        return f"| {label} | 0 | | | | | |"
    star = "*" if s["p"] < 0.05 else ""
    tail = f" | {s['syms']} | {s['top3']:.0f}%" if "syms" in s else ""
    return (f"| {label} | {s['n']} | {s['win']:.1f}%{star} | {s['mean']:+.2f}% | "
            f"**{s['ev']:+.2f}%** | {s['cum']:+.1f}%{tail} |")


def exit_label(tp, sl):
    a = f"利確+{tp:g}%" if tp is not None else "利確なし"
    b = f"損切り-{sl:g}%" if sl is not None else "損切りなし"
    return "大引け持ち切り" if tp is None and sl is None else f"{a} / {b}"


# ── 5. 本体 ──────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=datetime.now(wr.JST).date().isoformat())
    ap.add_argument("--no-fetch", action="store_true")
    ap.add_argument("--min-price", type=float, default=0.0)
    args = ap.parse_args()

    today = args.date
    since5 = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=58)).date().isoformat()
    outdir = os.path.join(BASE, "output", today)
    os.makedirs(outdir, exist_ok=True)
    p5 = os.path.join(outdir, f"bars_5m_panic_{today}.json")
    p1 = os.path.join(outdir, f"bars_1d_panic_{today}.json")

    alerts_all, _ = parse_alerts("2000-01-01")
    alerts, disc = parse_alerts(since5)
    syms = sorted({a["symbol"] for a in alerts})
    print(f"投げ売り検知: 全期間 {len(alerts_all)}件 / 5分足の取れる {since5}以降 {len(alerts)}件"
          f"（{len(syms)}銘柄）")

    if args.no_fetch and os.path.exists(p5) and os.path.exists(p1):
        daily = json.load(open(p1))
    else:
        print("  5分足を取得…")
        wr.fetch_bars(syms, since5, today, p5)
        since1 = (datetime.strptime(since5, "%Y-%m-%d") - timedelta(days=60)).date().isoformat()
        print("  日足を取得…")
        daily, _ = fetch_daily(syms, since1, today, p1)
    bars5 = wr.load_bars(p5)

    cands, skipped = build(alerts, bars5, daily)
    print(f"  検証できた候補 {len(cands)}件  除外: {dict(skipped)}")

    L = []
    p = L.append
    p(f"# 投げ売り検知 × ボラ拡大銘柄 × 寄り後の下落 バックテスト {today}")
    p("")
    p("`analysis/research_panic_volatile.py` が機械的に出した数字。解釈は別途。")
    p("")
    p("## データ")
    p("")
    p(f"- 投げ売り検知（ログ）: 全期間 {len(alerts_all)}件、5分足の取れる {since5}以降 "
      f"{len(alerts)}件・{len(syms)}銘柄")
    p(f"- 検証できた候補: {len(cands)}件（除外 {dict(skipped)}）")
    p(f"- 段階の内訳: {dict(Counter(e['stage'] for e in cands))}")
    p(f"- 往復コスト {COST}% を控除。`*` は勝率が五分と違う（二項検定 p<0.05）")
    p("- 約定は検知直後の足の始値。同じ足で利確・損切りの両方に触れたら損切り優先")
    p("- 絞り込み（ボラ・売買代金）は**前日までの日足**のみで判定（先読みなし）")
    if args.min_price:
        p(f"- 株価 {args.min_price:.0f}円以上に限定")
    if disc:
        p(f"- 接続断のあった日（検知を取りこぼしている可能性）: "
          + ", ".join(f"{d}({n})" for d, n in sorted(disc.items())))
    p("")

    ref_exits = [(2.0, 2.0), (None, None)]
    hdr = "| 区分 | 件数 | 勝率 | 平均 | 期待値 | 累計 | 銘柄数 | 上位3占有 |"
    sep = "|---|---:|---:|---:|---:|---:|---:|---:|"

    # 表1: 銘柄の絞り込み × 寄り後の下落（段階=両方）
    for tp, sl in ref_exits:
        p(f"## 1. 絞り込み × 寄り後の下落（段階=両方・{exit_label(tp, sl)}）")
        p("")
        p(hdr)
        p(sep)
        for un, uf in UNIVERSES:
            for on, oth in OPENS:
                sel = select(cands, uf, oth, STAGES[0][1], args.min_price)
                p(row(f"{un}／{on}", stat(rets(sel, tp, sl), sel)))
        p("")

    # 表2: 段階の比較
    p("## 2. 検知の段階（利確+2%/損切り-2%）")
    p("")
    p(hdr)
    p(sep)
    for un, uf in (UNIVERSES[0], UNIVERSES[1], UNIVERSES[6]):
        for sn, ss in STAGES:
            for on, oth in (OPENS[0], OPENS[1]):
                sel = select(cands, uf, oth, ss, args.min_price)
                p(row(f"{un}／{sn}／{on}", stat(rets(sel, 2.0, 2.0), sel)))
    p("")

    # 表3: 利確・損切りの格子（主要な組み合わせごと）
    focus = [
        ("A 制限なし・寄り比 問わず・両方", UNIVERSES[0][1], None),
        ("B 代金1億↑・寄り比0%以下・両方", UNIVERSES[1][1], 0.0),
        ("G 代金＋(急騰落 or ボラ拡大)・寄り比0%以下・両方", UNIVERSES[6][1], 0.0),
        ("G 代金＋(急騰落 or ボラ拡大)・寄り比-1%以下・両方", UNIVERSES[6][1], -1.0),
    ]
    for fn, uf, oth in focus:
        sel = select(cands, uf, oth, STAGES[0][1], args.min_price)
        p(f"## 3. 利確・損切りの格子: {fn}（n={len(sel)}）")
        p("")
        p("| 利確＼損切り | -1% | -1.5% | -2% | なし |")
        p("|---|---:|---:|---:|---:|")
        for tp in (1.0, 1.5, 2.0, 3.0, None):
            cells = []
            for sl in (1.0, 1.5, 2.0, None):
                s = stat(rets(sel, tp, sl))
                cells.append("—" if not s else
                             f"{s['ev']:+.2f}% ({s['win']:.0f}%)")
            lab = f"+{tp:g}%" if tp is not None else "なし"
            p(f"| {lab} | " + " | ".join(cells) + " |")
        p("")
        p("セルは「期待値（勝率）」。利確なし・損切りなし＝大引け持ち切り。")
        p("")

    # 表4: 頑健性（前半・後半／除外された側）
    p("## 4. 頑健性（利確+2%/損切り-2% と 大引け持ち切り）")
    p("")
    p(hdr)
    p(sep)
    for fn, uf, oth in focus[1:]:
        sel = select(cands, uf, oth, STAGES[0][1], args.min_price)
        if not sel:
            continue
        mid = sel[len(sel) // 2]["date"]
        early = [e for e in sel if e["date"] < mid]
        late = [e for e in sel if e["date"] >= mid]
        exc = select(cands, uf, oth, STAGES[0][1], args.min_price, invert_uni=True)
        for tp, sl in ref_exits:
            tag = exit_label(tp, sl)
            p(row(f"{fn}／{tag}／全体", stat(rets(sel, tp, sl), sel)))
            p(row(f"　前半（〜{mid}）", stat(rets(early, tp, sl), early)))
            p(row(f"　後半（{mid}〜）", stat(rets(late, tp, sl), late)))
            p(row("　**除外された側**（絞り込みを満たさない）", stat(rets(exc, tp, sl), exc)))
    p("")

    # 表5: 時間帯（参考）
    p("## 5. 時間帯（G・寄り比0%以下・両方・利確+2%/損切り-2%）")
    p("")
    p(hdr)
    p(sep)
    sel = select(cands, UNIVERSES[6][1], 0.0, STAGES[0][1], args.min_price)
    for lab, lo, hi in (("9時台", 9, 9), ("10時台", 10, 10), ("11-12時", 11, 12),
                        ("13時台", 13, 13), ("14時台", 14, 14)):
        sub = [e for e in sel if lo <= e["hour"] <= hi]
        p(row(lab, stat(rets(sub, 2.0, 2.0), sub)))
    p("")

    # 付録: 候補一覧（G・寄り比0%以下・両方）
    p("## 付録: 候補一覧（G・寄り比0%以下・両方）")
    p("")
    p("| 日付 | 時刻 | 銘柄 | 段階 | 寄り比 | 前日代金 | 3日最大騰落 | ボラ拡大率 | 大引け | +2/-2 |")
    p("|---|---|---|---|---:|---:|---:|---:|---:|---:|")
    for e in sel:
        f = e["f"]
        r_close = wr.simulate(e["db"], e["i0"], None, None)
        r22 = wr.simulate(e["db"], e["i0"], 2.0, 2.0)
        p(f"| {e['date']} | {e['time'][:5]} | {e['symbol']} | {e['stage']} | "
          f"{e['open_dev']:+.1f}% | {f['turnover'] / OKU:.1f}億 | {f['maxabsret3']:.1f}% | "
          f"{f['vol_ratio']:.2f} | {r_close:+.2f}% | {r22:+.2f}% |")
    p("")

    path = os.path.join(outdir, f"panic_volatile_backtest_{today}.md")
    with io.open(path, "w", encoding="utf-8") as fo:
        fo.write("\n".join(L) + "\n")
    print(f"  → {path}")


if __name__ == "__main__":
    main()
