"""発注数量の計算と、発注可否のチェック。API非依存の純ロジック。

数量の決め方:
    使用可能額 = min(設定の使用上限, 1銘柄あたり上限, 買付余力)
    数量      = 使用可能額 ÷ 現在値 を 売買単位(TradingUnit)の倍数に切り捨て

買付余力は kabuステーションAPI の GET /wallet/cash の
**AuKCStockAccountWallet（うち、三菱UFJ eスマート証券可能額）** を使う。
StockAccountWallet（現物買付可能額の合計）ではない点に注意。
"""
from dataclasses import dataclass


@dataclass
class SizingResult:
    """数量計算の結果。ok=False のときは reason に見送り理由が入る。"""
    ok: bool
    quantity: int = 0
    amount: float = 0.0          # 想定約定金額（数量 × 価格）
    budget: float = 0.0          # 実際に使えると判断した上限額
    reason: str = ""
    limited_by: str = ""         # 上限を決めた要因（設定/銘柄上限/余力）


def calc_quantity(price, buying_power, cfg, trading_unit: int = 100,
                  open_positions: int = 0) -> SizingResult:
    """発注数量を計算する。

    price:         現在値（PUSHで受信した値を使う）
    buying_power:  買付余力（AuKCStockAccountWallet）
    cfg:           config.yaml の capital セクション（dict）
    trading_unit:  売買単位（GET /symbol/{symbol} の TradingUnit）
    open_positions: 現在の建玉数
    """
    max_use = float(cfg.get("max_use_amount", 0))
    max_per_symbol = float(cfg.get("max_amount_per_symbol", max_use))
    max_positions = int(cfg.get("max_positions", 1))
    min_free = float(cfg.get("min_free_margin", 0))
    unit = int(trading_unit or cfg.get("lot_size", 100) or 100)

    if price is None or float(price) <= 0:
        return SizingResult(False, reason="現在値が取得できていない")
    price = float(price)

    if buying_power is None:
        return SizingResult(False, reason="買付余力が取得できていない")
    buying_power = float(buying_power)

    if buying_power < min_free:
        return SizingResult(
            False, reason=f"買付余力 {buying_power:,.0f}円 が下限 {min_free:,.0f}円 を下回る")

    if open_positions >= max_positions:
        return SizingResult(
            False, reason=f"建玉が上限に達している（{open_positions}/{max_positions}件）")

    # 使える金額の上限を決める（3つの制約のうち最小）
    candidates = [("設定の使用上限", max_use),
                  ("1銘柄あたり上限", max_per_symbol),
                  ("買付余力", buying_power)]
    limited_by, budget = min(candidates, key=lambda x: x[1])

    if budget <= 0:
        return SizingResult(False, reason=f"使用可能額が0以下（{limited_by}）")

    lots = int(budget // (price * unit))
    qty = lots * unit
    if qty <= 0:
        return SizingResult(
            False, budget=budget, limited_by=limited_by,
            reason=(f"1単元も買えない（1単元={unit}株×{price:,.0f}円="
                    f"{unit*price:,.0f}円 > 使用可能額 {budget:,.0f}円）"))

    return SizingResult(True, quantity=qty, amount=qty * price,
                        budget=budget, limited_by=limited_by)


def count_open_positions(positions: list, symbol: str = None) -> int:
    """GET /positions の結果から保有中の建玉数を数える。

    残数量(LeavesQty)が0のものは決済済みなので除外する。
    symbol を指定するとその銘柄のみを数える。
    """
    n = 0
    for p in positions or []:
        if symbol is not None and str(p.get("Symbol")) != str(symbol):
            continue
        try:
            if float(p.get("LeavesQty") or 0) > 0:
                n += 1
        except (TypeError, ValueError):
            continue
    return n
