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
# 現物買いの預り区分（FundType）。仕様上の有効値は "02"(保護預り) / "AA"(信用代用)。
# どちらが通るかは口座設定に依存し、合っていないと Code=100031「預り区分をご確認ください」
# で弾かれる（HTTP 500）。
# 本口座は現物株式が全額「信用保証金代用」で保有され保護預りは 0 円（2026-08-25 残高で確認）。
# よって "02"(保護預り) は必ず 100031 で弾かれ、"AA"(信用代用) が正しい。
# 実際 2026-08-26 に "02" が2回連続失敗した。既定を "AA" とし、
# config(capital.fund_type_cash_buy)で上書きできるようにしてある（口座が変わった場合のみ）。
FUND_TYPE_BUY = "AA"       # 信用代用（本口座の現物株式の預り区分に一致）
FUND_TYPE_SELL = "  "      # 半角スペース2つ
VALID_FUND_TYPES_BUY = ("02", "AA")

# 発注先の市場（Exchange）。仕様書 RequestSendOrder より:
#   1=東証 / 3=名証 / 5=福証 / 6=札証 / 9=SOR / 27=東証+
# そして次の但し書きが付いている。
#   「※SORまたは、東証+がメンテナンス中は現物のみ東証への指定が可能です。
#     通常時に東証を指定しての新規発注はできません。」
# つまり **1（東証）は新規発注には使えない**。2026-08-28に4199と6217が
# Code=100378「指定された市場でのお取引はお受けできません」で連続失敗したのはこれが原因で、
# それ以前の実発注が一度も成立していなかったのも同じ理由と考えられる。
#
# 注意: 板・銘柄・歩み値（/board /symbol /timeandsales）は逆に
#   「※SOR市場は取扱っておりません」と明記されている。**市場データ側は 1 のまま**にすること。
#   ここで定義しているのは注文の宛先だけ。
EXCHANGE_TSE = 1           # 東証。新規発注には使えない（メンテナンス時の現物のみ例外）
EXCHANGE_NSE = 3           # 名証
EXCHANGE_FSE = 5           # 福証
EXCHANGE_SSE = 6           # 札証
EXCHANGE_SOR = 9           # SOR（複数市場から有利な方へ自動執行）
EXCHANGE_TSE_PLUS = 27     # 東証+
VALID_EXCHANGES = (EXCHANGE_TSE, EXCHANGE_NSE, EXCHANGE_FSE, EXCHANGE_SSE,
                   EXCHANGE_SOR, EXCHANGE_TSE_PLUS)

# 実際に使う市場。設定から一度だけ差し替える（configure_exchange）。
#
# なぜモジュール1箇所で持つか: 仕様書に
#   「東証で保有している建玉をSORまたは東証+では返済できませんのでご注意ください」
# とある。新規と決済で市場が食い違うと**建てた玉を決済できなくなる**。
# 新規は trader.py、決済は position_manager.py と組み立て場所が分かれているため、
# 引数で渡す方式だと片方だけ変え忘れる余地が残る。値を1つしか持てない形にして、
# 食い違いを構造的に起こせなくしている。
_ORDER_EXCHANGE = EXCHANGE_SOR


def configure_exchange(value, log=None):
    """発注先の市場を設定する。起動時に1回だけ呼ぶ。

    None や空を渡した場合は既定（SOR）のまま。
    """
    global _ORDER_EXCHANGE
    if value in (None, ""):
        return _ORDER_EXCHANGE
    v = int(value)
    if v not in VALID_EXCHANGES:
        raise ValueError(
            f"不正な市場コード: {v}（有効値: {VALID_EXCHANGES}）")
    if v == EXCHANGE_TSE and log is not None:
        log.warning(
            "発注先の市場に東証(1)が指定されています。仕様上、SOR/東証+が"
            "メンテナンス中でない限り新規発注は Code=100378 で拒否されます")
    _ORDER_EXCHANGE = v
    return v


def order_exchange() -> int:
    """いま発注に使う市場コード。"""
    return _ORDER_EXCHANGE


def _price_value(price):
    """Price は数値で送る。成行は0。"""
    if price is None:
        return 0
    d = Decimal(str(price))
    # 0.1円刻みの銘柄があるため、整数に丸めずそのまま数値化する
    return float(d) if d != d.to_integral_value() else int(d)


def build_cash_order(symbol, side, qty, front_order_type, price=None,
                     exchange=None, account_type=4, expire_day=0, fund_type=None):
    """現物注文のリクエストボディを組み立てる。

    side: SIDE_BUY / SIDE_SELL
    front_order_type: FRONT_MARKET / FRONT_LIMIT / FRONT_MOC_AFTERNOON など
    price: 指値価格。成行・引成の場合は None（0が入る）
    exchange: 発注先の市場。None なら configure_exchange() で設定した値（既定SOR）。
              新規と決済で食い違うと建玉を返済できなくなるため、通常は指定しない。
    fund_type: 現物買いの預り区分を上書きする（"02" / "AA"）。None なら既定を使う。
               現物売りは常に半角スペース2つ（仕様）で、ここでは指定できない。
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
    if is_buy:
        fund = fund_type or FUND_TYPE_BUY
        if fund not in VALID_FUND_TYPES_BUY:
            # 仕様外の値は 100031 で弾かれるだけなので、送る前に気づけるようにする
            raise ValueError(
                f"現物買いの FundType は {VALID_FUND_TYPES_BUY} のいずれか: {fund!r}")
    else:
        fund = FUND_TYPE_SELL
    ex = _ORDER_EXCHANGE if exchange is None else int(exchange)
    if ex not in VALID_EXCHANGES:
        raise ValueError(f"不正な市場コード: {ex}（有効値: {VALID_EXCHANGES}）")
    return {
        "Symbol": str(symbol),
        "Exchange": ex,
        "SecurityType": SECURITY_TYPE_STOCK,
        "Side": side,
        "CashMargin": CASH_MARGIN_CASH,
        "DelivType": DELIV_TYPE_BUY if is_buy else DELIV_TYPE_SELL,
        "FundType": fund,
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
