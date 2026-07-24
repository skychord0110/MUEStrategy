# -*- coding: utf-8 -*-
"""runnerログに実際に記録されたAI仮想売買（フォワード検証）の実現損益を集計する。

今週(07-21〜24)は更新後のコードで稼働しており、AIストラテジーの仮想エントリー/決済が
ログに残っている。その実現リターンをそのまま集計する（5分足による近似ではなく、
稼働中のロジックが出した実際の結果）。

入力: strategies/runner/logs/runner_2026-07-2[1-4].log
実行: python -X utf8 paper_trade_results_2026-07-24.py
"""
import re
import glob
import os
import statistics
from collections import defaultdict

BASE = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.normpath(os.path.join(BASE, "..", "strategies", "runner", "logs"))

# 例:
# [AI午後引け戻り/エントリー] 4840 4840: UNDER急増 を検知、579.0円で仮想買い（…）
# [AI午後引け戻り/決済:利確] 4840 4840: 仮想決済 579.0円→591.0円 (+2.07%)
entry_re = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2}) (?P<time>\d{2}:\d{2}:\d{2}),\d+ \[INFO\] "
    r"\[AI(?P<strat>[^/]+)/エントリー\] (?P<symbol>\d{4})")
exit_re = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2}) (?P<time>\d{2}:\d{2}:\d{2}),\d+ \[INFO\] "
    r"\[AI(?P<strat>[^/]+)/決済:(?P<reason>[^\]]+)\] (?P<symbol>\d{4}) \d{4}: "
    r"仮想決済 [\d.]+円→[\d.]+円 \((?P<ret>[+-][\d.]+)%\)")

exits = []  # {strat, date, symbol, reason, ret}
entries = []
for path in sorted(glob.glob(os.path.join(LOG_DIR, "runner_2026-07-2[1-4].log"))):
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            em = entry_re.match(line)
            if em:
                entries.append(em.groupdict())
                continue
            xm = exit_re.match(line)
            if xm:
                d = xm.groupdict()
                d["ret"] = float(d["ret"])
                exits.append(d)

print(f"エントリー {len(entries)}件 / 決済 {len(exits)}件\n")

def report(rows, name):
    if not rows:
        print(f"== {name}: 取引なし ==\n")
        return
    rets = [r["ret"] for r in rows]
    wins = sum(1 for v in rets if v > 0)
    losses = sum(1 for v in rets if v < 0)
    flats = sum(1 for v in rets if v == 0)
    print(f"== {name} ==")
    print(f"  取引数: {len(rets)}  勝ち {wins} / 負け {losses} / 引分 {flats}")
    decisive = wins + losses
    if decisive:
        print(f"  勝率(引分除く): {wins/decisive*100:.1f}%   勝率(引分含む): {wins/len(rets)*100:.1f}%")
    print(f"  期待値(平均): {statistics.mean(rets):+.3f}%/回   中央値: {statistics.median(rets):+.3f}%")
    print(f"  合計リターン(等金額): {sum(rets):+.2f}%")
    win_rets = [v for v in rets if v > 0]
    loss_rets = [v for v in rets if v < 0]
    if win_rets and loss_rets:
        pf = sum(win_rets) / abs(sum(loss_rets))
        print(f"  平均利益 {statistics.mean(win_rets):+.2f}% / 平均損失 {statistics.mean(loss_rets):+.2f}% / プロフィットファクター {pf:.2f}")
    print(f"  → 30万円/回運用時: 期待値 約{statistics.mean(rets)/100*300000:+,.0f}円/回, 週合計 約{sum(rets)/100*300000:+,.0f}円")
    # 決済理由別
    by_reason = defaultdict(list)
    for r in rows:
        by_reason[r["reason"]].append(r["ret"])
    print("  決済理由別:", {k: f"{len(v)}件 平均{statistics.mean(v):+.2f}%" for k, v in by_reason.items()})
    # 日別
    by_date = defaultdict(list)
    for r in rows:
        by_date[r["date"]].append(r["ret"])
    for d in sorted(by_date):
        vs = by_date[d]
        print(f"    {d}: {len(vs)}件 平均 {statistics.mean(vs):+.3f}% 合計 {sum(vs):+.2f}%")
    print()

# ストラテジー別
report([r for r in exits if r["strat"] == "午後引け戻り"], "AI午後引け戻り（afternoon_reversal）実績")
report([r for r in exits if r["strat"] == "複合シグナル"], "AI複合シグナル（confluence）実績")
report(exits, "AI全ストラテジー合算")

# 明細
print("=== 全取引明細 ===")
for r in sorted(exits, key=lambda x: (x["date"], x["symbol"])):
    print(f"  {r['date']} {r['symbol']} [{r['strat']}/{r['reason']}] {r['ret']:+.2f}%")
