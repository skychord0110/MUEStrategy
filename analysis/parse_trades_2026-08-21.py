# -*- coding: utf-8 -*-
"""ランナーの場中ログから、仮想売買の全トレードを取り出してCSVにする。

ログは1トレードにつき2行を残す。
    [AI午後引け戻り/エントリー] 4199 4199: UNDER急増 を検知、800.0円で仮想買い（損切り-2.0%/利確+2.0%/…）
    [AI午後引け戻り/決済:利確]   4199 4199: 仮想決済 800.0円→819.0円 (+2.38%)
エントリーと決済は「同じ日・同じ戦略・同じ銘柄」で古い順に突き合わせる
（同一銘柄の建玉は戦略ごとに1つしか持たない作りなので、これで一意に決まる）。

決済が無いままログが終わっているものは未決済として落とす（ランナーの
異常終了や接続断で起きる。当日の引けまで到達していないため損益が確定しない）。

実行:
    python analysis/parse_trades_2026-08-21.py
出力:
    analysis/output/2026-08-21/trades_2026-08-21.csv
"""
import csv
import os
import re
from collections import defaultdict, deque

BASE = os.path.dirname(os.path.abspath(__file__))
LOGDIR = os.path.normpath(os.path.join(BASE, "..", "strategies", "runner", "logs"))
OUTDIR = os.path.join(BASE, "output", "2026-08-21")

RE_ENTRY = re.compile(
    r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}),\d+ \[INFO\] "
    r"\[([^\]/]+)/エントリー\] (\d+) \S+: (.+?) を検知、([\d.]+)円で仮想買い"
    r"（損切り(-[\d.]+)%/利確\+([\d.]+)%")
RE_EXIT = re.compile(
    r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}),\d+ \[INFO\] "
    r"\[([^\]/]+)/決済:([^\]]+)\] (\d+) \S+: 仮想決済 ([\d.]+)円→([\d.]+)円 "
    r"\(([+-][\d.]+)%\)")
# 接続断はその日のデータの信頼性に関わるので数えておく
RE_DISCONNECT = re.compile(r"WinError 10061|WebSocket切断|接続が拒否")


def parse():
    trades, open_pos = [], defaultdict(deque)
    disconnects = defaultdict(int)
    for name in sorted(os.listdir(LOGDIR)):
        if not name.startswith("runner_") or not name.endswith(".log"):
            continue
        with open(os.path.join(LOGDIR, name), encoding="utf-8",
                  errors="replace") as f:
            for line in f:
                if RE_DISCONNECT.search(line):
                    disconnects[name[7:17]] += 1
                    continue
                m = RE_ENTRY.match(line)
                if m:
                    d, t, strat, sym, trig, px, sl, tp = m.groups()
                    open_pos[(d, strat, sym)].append(
                        {"date": d, "entry_time": t, "strategy": strat,
                         "symbol": sym, "trigger": trig,
                         "entry_px": float(px), "sl_pct": float(sl),
                         "tp_pct": float(tp)})
                    continue
                m = RE_EXIT.match(line)
                if not m:
                    continue
                d, t, strat, why, sym, epx, xpx, pct = m.groups()
                q = open_pos.get((d, strat, sym))
                if not q:
                    continue            # エントリーが無い決済（起動前の建玉など）
                rec = q.popleft()
                rec.update(exit_time=t, exit_reason=why, exit_px=float(xpx),
                           pct=float(pct))
                trades.append(rec)
    unclosed = sum(len(v) for v in open_pos.values())
    return trades, unclosed, disconnects


def main():
    os.makedirs(OUTDIR, exist_ok=True)
    trades, unclosed, disc = parse()
    trades.sort(key=lambda r: (r["date"], r["entry_time"]))
    cols = ["date", "entry_time", "exit_time", "strategy", "symbol", "trigger",
            "entry_px", "exit_px", "pct", "exit_reason", "sl_pct", "tp_pct"]
    path = os.path.join(OUTDIR, "trades_2026-08-21.csv")
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(trades)

    days = sorted({t["date"] for t in trades})
    print(f"決済まで到達したトレード: {len(trades)}件")
    print(f"  対象日: {len(days)}日（{days[0]} 〜 {days[-1]}）")
    print(f"  未決済のまま終了: {unclosed}件（集計から除外）")
    print(f"  出力: {path}")
    if disc:
        print("\n接続断が出た日（その日のデータは取りこぼしの疑いあり）:")
        for d, n in sorted(disc.items()):
            print(f"  {d}  {n:>5}件")


if __name__ == "__main__":
    main()
