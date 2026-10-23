# -*- coding: utf-8 -*-
"""コントロールパネルの「資産残高」の計算を固定するテスト。

資産残高 = 買付余力 + 未約定の現物買い注文の金額 + 現物の評価額 + 信用の評価損益
（kabuステーションAPIに預り金・資産合計のフィールドが無いため手元で組み立てる。
  詳細: strategies/runner/src/account_snapshot.py）

外部ライブラリもネットワークも使わない。

実行:
    python tests/test_account_assets.py
"""
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.normpath(os.path.join(BASE, ".."))
p = os.path.join(REPO, "strategies", "runner", "src")
if p not in sys.path:
    sys.path.insert(0, p)

import account_snapshot as snap   # noqa: E402


class FakeClient:
    def __init__(self, wallet, orders, positions):
        self.wallet = wallet
        self.orders = orders
        self.positions = positions
        self.orders_product = None

    def get_wallet_cash(self):
        return self.wallet

    def get_orders(self, product=None, **kw):
        self.orders_product = product
        return self.orders

    def get_positions(self, product=None):
        return self.positions


def order(side="2", state=1, qty=100, cum=0, price=1000.0):
    return {"Side": side, "State": state, "OrderQty": qty, "CumQty": cum, "Price": price}


def cash_pos(qty=400, price=425.0, current=347.0, valuation=None, pl=None):
    return {"Symbol": "4263", "SymbolName": "サスメド", "LeavesQty": qty, "Price": price,
            "CurrentPrice": current,
            "Valuation": current * qty if valuation is None else valuation,
            "ProfitLoss": (current - price) * qty if pl is None else pl,
            "ProfitLossRate": None, "Side": "2"}


# ── 未約定の買い注文 ──────────────────────────────────────────────

def test_limit_buy_adds_price_times_unfilled():
    amount, market = snap.pending_buy_amount([order(qty=300, cum=100, price=826.0)])
    assert amount == 826.0 * 200, amount
    assert market == 0


def test_finished_orders_are_ignored():
    """State=5（全約定・取消・失効）は余力を拘束していない。失効しても OrderQty は0にならない。"""
    amount, _ = snap.pending_buy_amount([order(state=5, qty=100, cum=0)])
    assert amount == 0


def test_sell_orders_are_ignored():
    amount, _ = snap.pending_buy_amount([order(side="1")])
    assert amount == 0


def test_cancel_in_flight_still_counts():
    """State=4（訂正取消送信中）は取消が通るまで拘束が残る。"""
    amount, _ = snap.pending_buy_amount([order(state=4, price=500.0)])
    assert amount == 500.0 * 100


def test_market_buy_is_counted_not_priced():
    amount, market = snap.pending_buy_amount([order(price=0.0)])
    assert amount == 0 and market == 1


# ── 資産残高の合計 ────────────────────────────────────────────────

def test_total_assets_cash_position_and_pending_order():
    c = FakeClient({snap.BUYING_POWER_FIELD: 500000},
                   [order(qty=100, price=1000.0)], [cash_pos()])
    s = snap.build(c)
    assert c.orders_product == "1", "現物の注文だけを見る（product=1）"
    assert s["pending_buy"] == 100000
    assert s["cash_valuation"] == 347.0 * 400
    assert s["total_assets"] == 500000 + 100000 + 138800, s["total_assets"]


def test_margin_position_adds_only_profit_loss():
    """信用の評価金額は建てた総額なので足さない。評価損益だけ足す。"""
    margin = cash_pos(qty=100, price=1000.0, current=1100.0)
    margin["MarginTradeType"] = 1
    c = FakeClient({snap.BUYING_POWER_FIELD: 500000}, [], [margin])
    s = snap.build(c)
    assert s["margin_pl"] == 10000
    assert s["total_assets"] == 510000, s["total_assets"]


def test_valuation_missing_falls_back_to_current_price():
    pos = cash_pos()
    pos["Valuation"] = None
    s = snap.build(FakeClient({snap.BUYING_POWER_FIELD: 0}, [], [pos]))
    assert s["total_assets"] == 347.0 * 400


def test_unknown_value_shows_dash_instead_of_understated_total():
    pos = cash_pos()
    pos["Valuation"] = None
    pos["CurrentPrice"] = None
    s = snap.build(FakeClient({snap.BUYING_POWER_FIELD: 500000}, [], [pos]))
    assert s["total_assets"] is None


def test_no_buying_power_shows_dash():
    s = snap.build(FakeClient({}, [], [cash_pos()]))
    assert s["total_assets"] is None


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            failed += 1
            print(f"  NG  {t.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"  NG  {t.__name__}: {type(e).__name__} {e}")
        else:
            print(f"  ok  {t.__name__}")
    print(f"\n{len(tests) - failed}/{len(tests)} 成功")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
