# -*- coding: utf-8 -*-
"""発注前点検 — 実際に送るボディを組み立てて、仕様と設定を突き合わせる。

2026-08中に実発注が4回連続で失敗した。原因はいずれも**送信前に分かったはずの
パラメータの誤り**だった。

    08-26  Code=100031  FundType="02"（この口座は保護預りが0円）
    08-28  Code=100378  Exchange=1（東証は新規発注に使えない）

発注してから500で気づくのではなく、**場が開く前にここで気づく**ためのスクリプト。
APIは呼ばない（口座にもネットワークにも触れない）ので、いつでも安全に実行できる。

実行:
    python strategies/autotrade/preflight.py

出典: kabuステーションAPI仕様書 RequestSendOrder
      https://kabucom.github.io/kabusapi/reference/index.html
"""
import json
import logging
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(BASE, "src")
RUNNER_SRC = os.path.normpath(os.path.join(BASE, "..", "runner", "src"))
for p in (SRC, RUNNER_SRC):
    if p not in sys.path:
        sys.path.insert(0, p)

import yaml                      # noqa: E402
import order_builder as ob       # noqa: E402

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CONFIG = os.path.join(BASE, "config.yaml")

# 仕様書の定義値。「なぜその値でなければならないか」を根拠ごと持たせる。
SPEC = {
    "Exchange": {
        "valid": {1: "東証", 3: "名証", 5: "福証", 6: "札証", 9: "SOR", 27: "東証+"},
        "note": "「通常時に東証を指定しての新規発注はできません」"
                "（SOR/東証+のメンテナンス中のみ現物で東証が使える）",
        "warn_if": {1: "東証(1)は新規発注に使えない。Code=100378で弾かれる"},
    },
    "SecurityType": {"valid": {1: "株式"}, "note": "株式は1"},
    "Side": {"valid": {"1": "売", "2": "買"}, "note": "文字列で送る"},
    "CashMargin": {"valid": {1: "現物", 2: "信用新規", 3: "信用返済"}, "note": ""},
    "AccountType": {"valid": {2: "一般", 4: "特定", 12: "法人"},
                    "note": "実際の口座種別と一致していること（違うとCode=2）"},
    "DelivType": {
        "valid": {0: "指定なし", 2: "お預り金", 3: "auマネーコネクト"},
        "note": "現物買は指定必須（2）／現物売は0（指定なし）を設定",
    },
    "FundType": {
        "valid": {"  ": "現物売", "02": "保護", "AA": "信用代用", "11": "信用取引"},
        "note": "現物買は指定必須／現物売は半角スペース2つを指定必須",
    },
    "FrontOrderType": {
        "valid": {10: "成行", 13: "寄成", 16: "引成(後場)", 20: "指値",
                  21: "寄指", 24: "引指(後場)", 30: "逆指値"},
        "note": "成行・引成のときPriceは0",
    },
    "ExpireDay": {"valid": None, "note": "0=当日。当日中で完結する戦略なので0"},
}

REQUIRED = ("Symbol", "Exchange", "SecurityType", "Side", "CashMargin",
            "DelivType", "AccountType", "Qty", "Price", "ExpireDay",
            "FrontOrderType")

ok_count = ng_count = warn_count = 0


def check(cond, msg, level="NG"):
    global ok_count, ng_count, warn_count
    if cond:
        ok_count += 1
        print(f"    OK   {msg}")
    elif level == "WARN":
        warn_count += 1
        print(f"    警告 {msg}")
    else:
        ng_count += 1
        print(f"    NG   {msg}")


def check_payload(name, o):
    p = ob.to_payload(o)
    print(f"\n  【{name}】")
    print(f"    {json.dumps(p, ensure_ascii=False)}")

    missing = [k for k in REQUIRED if k not in p]
    check(not missing, f"必須項目がそろっている（欠け: {missing or 'なし'}）")

    for key, rule in SPEC.items():
        if key not in p:
            continue
        v = p[key]
        valid = rule["valid"]
        if valid is not None:
            label = valid.get(v)
            check(label is not None,
                  f"{key}={v!r} {'(' + label + ')' if label else '← 仕様外の値'}")
            if v in (rule.get("warn_if") or {}):
                check(False, rule["warn_if"][v], level="WARN")

    is_buy = p["Side"] == ob.SIDE_BUY
    if p["CashMargin"] == 1:
        if is_buy:
            check(p["DelivType"] == 2, "現物買のDelivTypeは2(お預り金)")
            check(p["FundType"] in ("02", "AA"),
                  f"現物買のFundTypeが指定されている（{p['FundType']!r}）")
        else:
            check(p["DelivType"] == 0, "現物売のDelivTypeは0(指定なし)")
            check(p["FundType"] == "  ",
                  f"現物売のFundTypeは半角スペース2つ（{p['FundType']!r}）")

    if p["FrontOrderType"] in (10, 13, 16):
        check(p["Price"] == 0, "成行・引成のPriceは0")
    else:
        check(p["Price"] > 0, f"指値のPriceが入っている（{p['Price']}）")
    check(int(p["Qty"]) > 0, f"数量が正（{p['Qty']}）")
    return p


def main():
    logging.disable(logging.CRITICAL)
    cfg = yaml.safe_load(open(CONFIG, encoding="utf-8"))
    cap = cfg.get("capital") or {}
    ft = cap.get("fund_type_cash_buy")
    at = cap.get("account_type", 4)

    # trader を作らずに市場だけ反映する（AutoTrader は口座接続を要求するため）
    ob.configure_exchange((cfg.get("market") or {}).get("exchange"))

    print("=" * 74)
    print("発注前点検")
    print("=" * 74)
    print("\n■ いまの運転状態")
    on = [k for k, v in (cfg.get("strategies") or {}).items() if v]
    print(f"    自動売買 enabled : {cfg.get('enabled')}")
    print(f"    dry_run          : {cfg.get('dry_run')}"
          f"{'   ← 実際に発注されます' if cfg.get('dry_run') is False else ''}")
    print(f"    実売買の対象     : {on or 'なし'}")
    print(f"    発注先の市場     : {ob.order_exchange()} "
          f"({SPEC['Exchange']['valid'].get(ob.order_exchange(), '?')})")

    print("\n■ 送信するボディの検査")
    payloads = [
        ("新規・指値買い", ob.entry_limit_buy("9999", 100, 1000.0,
                                              account_type=at, fund_type=ft)),
        ("新規・成行買い", ob.entry_market_buy("9999", 100,
                                              account_type=at, fund_type=ft)),
        ("利確・指値売り", ob.take_profit_sell("9999", 100, 1020.0, account_type=at)),
        ("損切・気配売り", ob.stop_hit_bid_sell("9999", 100, 980.0, account_type=at)),
        ("引け・貫き売り", ob.close_out_sell("9999", 100, 970.0, account_type=at)),
    ]
    built = [check_payload(n, o) for n, o in payloads]

    print("\n■ 新規と決済で市場が一致しているか")
    exch = {p["Exchange"] for p in built}
    check(len(exch) == 1,
          f"すべて同じ市場（{exch}）。仕様上、東証の建玉はSOR/東証+で返済できない")

    print("\n■ 資金設定から見た実際の買える範囲")
    lot = int(cap.get("lot_size", 100))
    per = int(cap.get("max_amount_per_symbol", 0))
    use = int(cap.get("max_use_amount", 0))
    minf = int(cap.get("min_free_margin", 0))
    check(per <= use, f"1銘柄あたり({per:,}円) <= 使用上限({use:,}円)")
    if per and lot:
        cap_price = per // lot
        print(f"    → 1単元={lot}株なので、買えるのは **{cap_price:,}円以下** の銘柄だけ")
        print(f"       （{cap_price:,}円を超える銘柄は1単元も買えず見送りになる）")
    check(minf < use, f"余力下限({minf:,}円) < 使用上限({use:,}円)", level="WARN")

    print("\n" + "=" * 74)
    print(f"OK {ok_count}件 / 警告 {warn_count}件 / NG {ng_count}件")
    if ng_count:
        print("NGがあります。このまま発注すると弾かれる可能性が高いので直してください。")
    elif warn_count:
        print("仕様違反はありませんが、警告の内容を確認してください。")
    else:
        print("仕様上の問題は見つかりませんでした。")
    print("=" * 74)
    return 1 if ng_count else 0


if __name__ == "__main__":
    sys.exit(main())
