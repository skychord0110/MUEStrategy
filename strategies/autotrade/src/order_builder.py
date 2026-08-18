"""kabuステーションAPI /sendorder のリクエストを組み立てる。API非依存の純ロジック。

出典: kabuステーションAPI OpenAPI仕様 v1.5 の RequestSendOrder

現物取引の必須項目と本システムでの値:
  Symbol         銘柄コード
  Exchange       1（東証）
  SecurityType   1（株式）
  Side           "1"=売 / "2"=買
  CashMargin     1（現物）
  DelivType      現物買=2（お預り金） / 現物売=0（指定なし）
  FundType       現物買="02"（保護） / 現物売="  "（半角スペース2つ）
  AccountType    4（特定）など。config の account_type で指定
  Qty            注文数量
  FrontOrderType 執行条件（10=成行 / 20=指値 / 16=引成(後場) など）
  Price          注文価格（成行は0）
  ExpireDay      0（当日）

注文パスワードの入力は不要（/token で得たトークンで認証する）。

【重要】本モジュールは辞書を組み立てるだけで、送信は一切行わない。
"""
from decimal import Decimal

# 執行条件（FrontOrderType）— 仕様書の定義値
FRONT_MARKET = 10          # 成行
FRONT_LIMIT = 20           # 指値
FRONT_MOC_AFTERNOON = 16   # 引成（後場）
FRONT_LOC_AFTERNOON = 24   # 引指（後場）
FRONT_REVERSE_LIMIT = 30   # 逆指値

SIDE_SELL = "1"
SIDE_BUY = "2"

CASH_MARGIN_CASH = 1       # 現物
SECURITY_TYPE_STOCK = 1    # 株式

# /orders・/positions の product。CashMargin とは別の体系なので取り違えに注意。
#   product     0=すべて 1=現物 2=信用 3=先物 4=OP
#   CashMargin  1=現物   2=信用新規 3=信用返済
# 実際に取り違えていた（現物を"2"としていた）ため、2026-08-18に
# 発注失敗後の突き合わせが信用注文を探してしまい、現物の注文を
# 見つけられなかった。定義はここ1箇所に集約する。
PRODUCT_ALL = "0"
PRODUCT_CASH = "1"
PRODUCT_MARGIN = "2"
PRODUCT_FUTURE = "3"
PRODUCT_OPTION = "4"


def product_for(cash_margin) -> str:
    """CashMargin から /orders・/positions の product を求める。"""
    return PRODUCT_CASH if int(cash_margin or 1) == CASH_MARGIN_CASH else PRODUCT_MARGIN


def product_for_config(name) -> str:
    """config の capital.product（"cash" / "margin"）を product に直す。"""
    return PRODUCT_CASH if (name or "cash") == "cash" else PRODUCT_MARGIN

# 現物買/現物売で必須値が変わる（仕様書より）
DELIV_TYPE_BUY = 2         # お預り金
DELIV_TYPE_SELL = 0        # 指定なし
FUND_TYPE_BUY = "02"       # 保護
FUND_TYPE_SELL = "  "      # 半角スペース2つ


def _price_value(price):
    """Price は数値で送る。成行は0。"""
    if price is None:
        return 0
    d = Decimal(str(price))
    # 0.1円刻みの銘柄があるため、整数に丸めずそのまま数値化する
    return float(d) if d != d.to_integral_value() else int(d)


def build_cash_order(symbol, side, qty, front_order_type, price=None,
                     exchange=1, account_type=4, expire_day=0):
    """現物注文のリクエストボディを組み立てる。

    side: SIDE_BUY / SIDE_SELL
    front_order_type: FRONT_MARKET / FRONT_LIMIT / FRONT_MOC_AFTERNOON など
    price: 指値価格。成行・引成の場合は None（0が入る）
    """
    if side not in (SIDE_BUY, SIDE_SELL):
        raise ValueError(f"不正な売買区分: {side}")
    if not qty or int(qty) <= 0:
        raise ValueError(f"不正な数量: {qty}")
    if front_order_type == FRONT_LIMIT and price is None:
        raise ValueError("指値注文には価格が必要です")
    if front_order_type in (FRONT_MARKET, FRONT_MOC_AFTERNOON) and price not in (None, 0):
        raise ValueError(f"成行・引成に価格は指定できません: {price}")

    is_buy = side == SIDE_BUY
    return {
        "Symbol": str(symbol),
        "Exchange": int(exchange),
        "SecurityType": SECURITY_TYPE_STOCK,
        "Side": side,
        "CashMargin": CASH_MARGIN_CASH,
        "DelivType": DELIV_TYPE_BUY if is_buy else DELIV_TYPE_SELL,
        "FundType": FUND_TYPE_BUY if is_buy else FUND_TYPE_SELL,
        "AccountType": int(account_type),
        "Qty": int(qty),
        "FrontOrderType": int(front_order_type),
        "Price": _price_value(price),
        "ExpireDay": int(expire_day),
    }


# ── 本システムの各場面に対応した組み立て ──

def entry_market_buy(symbol, qty, **kw):
    """エントリー: 成行で新規買い。"""
    o = build_cash_order(symbol, SIDE_BUY, qty, FRONT_MARKET, None, **kw)
    o["_intent"] = "エントリー(成行買い)"
    return o


def entry_limit_buy(symbol, qty, price, **kw):
    """エントリー: 指値で新規買い。"""
    o = build_cash_order(symbol, SIDE_BUY, qty, FRONT_LIMIT, price, **kw)
    o["_intent"] = f"エントリー(指値買い @{price})"
    return o


def take_profit_sell(symbol, qty, target_price, **kw):
    """利確: 目標価格に指値売り（気配が近づいてから出す）。"""
    o = build_cash_order(symbol, SIDE_SELL, qty, FRONT_LIMIT, target_price, **kw)
    o["_intent"] = f"利確(指値売り @{target_price})"
    return o


def stop_hit_bid_sell(symbol, qty, bid_price, **kw):
    """損切り(A): 買い気配にぶつける指値売り。"""
    o = build_cash_order(symbol, SIDE_SELL, qty, FRONT_LIMIT, bid_price, **kw)
    o["_intent"] = f"損切りA(買い気配へぶつけ @{bid_price})"
    return o


def stop_mid_price_sell(symbol, qty, price, step=0, **kw):
    """損切り(B): 仲値（または指し直し後の価格）に指値売り。"""
    o = build_cash_order(symbol, SIDE_SELL, qty, FRONT_LIMIT, price, **kw)
    o["_intent"] = f"損切りB(仲値-{step}ティック @{price})" if step else \
                   f"損切りB(仲値 @{price})"
    return o


def close_out_sell(symbol, qty, price, pct=None, **kw):
    """引け際の強制手仕舞い: 買い気配から一定率下を貫く指値売り。

    価格は tick_size.shift_pct(買い気配, -aggression_pct) で求めた値を渡す。
    15:25〜15:30はクロージング・オークションのため即約定せず、
    大引けの単一価格で約定する（価格下限付きの引成として機能する）。
    深く貫いていても、実際の約定は大引け価格になるため不当に不利にはならない。
    """
    o = build_cash_order(symbol, SIDE_SELL, qty, FRONT_LIMIT, price, **kw)
    label = f"買い気配-{pct}%" if pct is not None else "気配貫き"
    o["_intent"] = f"引け手仕舞い({label} 指値売り @{price})"
    return o


def close_out_moc_sell(symbol, qty, **kw):
    """引け際の強制手仕舞い: 引成（後場）。価格指定なし。"""
    o = build_cash_order(symbol, SIDE_SELL, qty, FRONT_MOC_AFTERNOON, None, **kw)
    o["_intent"] = "引け手仕舞い(引成・後場)"
    return o


def describe(order: dict) -> str:
    """注文内容を人が読める1行にする（dry_runのログ用）。"""
    side = "売" if order["Side"] == SIDE_SELL else "買"
    ft = {FRONT_MARKET: "成行", FRONT_LIMIT: "指値",
          FRONT_MOC_AFTERNOON: "引成(後場)", FRONT_LOC_AFTERNOON: "引指(後場)",
          FRONT_REVERSE_LIMIT: "逆指値"}.get(order["FrontOrderType"], str(order["FrontOrderType"]))
    px = "—" if not order["Price"] else f"{order['Price']:,}円"
    return (f"{order['Symbol']} {side} {order['Qty']}株 {ft} {px} "
            f"[{order.get('_intent', '')}]")


def to_payload(order: dict) -> dict:
    """送信用のボディ（内部用の _intent を除いたもの）を返す。"""
    return {k: v for k, v in order.items() if not k.startswith("_")}
