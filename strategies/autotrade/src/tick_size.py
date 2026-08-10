"""呼値（ティック）の単位を判定する。API非依存の純ロジック。

出典: kabuステーションAPI OpenAPI仕様 v1.5 の SymbolSuccess.PriceRangeGroup
      （元は JPX の呼値の単位 https://www.jpx.co.jp/equities/trading/domestic/07.html）

呼値グループは銘柄情報API（GET /symbol/{symbol}）の PriceRangeGroup で取得できる:
  10000: 株式（通常の呼値単位の銘柄）
  10003: 株式（TOPIX500構成銘柄／売買単位10口以上のETF等を含む）
  10004: 株式（売買単位が1口のETF等）

執行ロジック（「気配の2ティック以内」「1ティック下」「10ティック貫く」など）は
すべてこの単位に依存するため、銘柄ごとに正しく引く必要がある。
特に 10003 は1000円以下で0.1円刻みと、通常銘柄（1円刻み）と10倍違う。
"""
from decimal import Decimal

# (この価格以下, 呼値単位) を安い順に並べたもの。最後は上限なし（None）。
# 単位は Decimal で持つ（0.1/0.5 の二進浮動小数誤差を避けるため）
_TABLES = {
    "10000": [
        (Decimal("3000"), Decimal("1")),
        (Decimal("5000"), Decimal("5")),
        (Decimal("30000"), Decimal("10")),
        (Decimal("50000"), Decimal("50")),
        (Decimal("300000"), Decimal("100")),
        (Decimal("500000"), Decimal("500")),
        (Decimal("3000000"), Decimal("1000")),
        (Decimal("5000000"), Decimal("5000")),
        (Decimal("30000000"), Decimal("10000")),
        (Decimal("50000000"), Decimal("50000")),
        (None, Decimal("100000")),
    ],
    "10003": [
        (Decimal("1000"), Decimal("0.1")),
        (Decimal("3000"), Decimal("0.5")),
        (Decimal("10000"), Decimal("1")),
        (Decimal("30000"), Decimal("5")),
        (Decimal("100000"), Decimal("10")),
        (Decimal("300000"), Decimal("50")),
        (Decimal("1000000"), Decimal("100")),
        (Decimal("3000000"), Decimal("500")),
        (Decimal("10000000"), Decimal("1000")),
        (Decimal("30000000"), Decimal("5000")),
        (None, Decimal("10000")),
    ],
    "10004": [
        (Decimal("10000"), Decimal("1")),
        (Decimal("30000"), Decimal("5")),
        (Decimal("100000"), Decimal("10")),
        (Decimal("300000"), Decimal("50")),
        (Decimal("1000000"), Decimal("100")),
        (Decimal("3000000"), Decimal("500")),
        (Decimal("10000000"), Decimal("1000")),
        (Decimal("30000000"), Decimal("5000")),
        (None, Decimal("10000")),
    ],
}

DEFAULT_GROUP = "10000"


class UnknownPriceRangeGroup(Exception):
    """未知の呼値グループ。黙って既定値で計算すると価格がずれるため例外にする。"""


def tick_size(price, price_range_group=DEFAULT_GROUP) -> Decimal:
    """価格と呼値グループから呼値の単位（Decimal）を返す。

    price: 基準となる価格（この価格が属する水準の刻みを返す）
    price_range_group: GET /symbol/{symbol} の PriceRangeGroup（文字列でも数値でも可）
    """
    group = str(price_range_group).strip()
    table = _TABLES.get(group)
    if table is None:
        raise UnknownPriceRangeGroup(
            f"未知の呼値グループです: {price_range_group}。"
            f"対応済み: {sorted(_TABLES)}（先物・オプションは本モジュールの対象外）")
    p = Decimal(str(price))
    for upper, unit in table:
        if upper is None or p <= upper:
            return unit
    return table[-1][1]


def round_to_tick(price, price_range_group=DEFAULT_GROUP, mode="down") -> Decimal:
    """価格を呼値の刻みに丸める。

    mode: "down"=切り下げ / "up"=切り上げ / "nearest"=最も近い刻み
    売り指値を不利側に置くなら "down"、買い指値を不利側に置くなら "up"。
    """
    unit = tick_size(price, price_range_group)
    p = Decimal(str(price))
    q = p / unit
    if mode == "down":
        n = q.to_integral_value(rounding="ROUND_FLOOR")
    elif mode == "up":
        n = q.to_integral_value(rounding="ROUND_CEILING")
    elif mode == "nearest":
        n = q.to_integral_value(rounding="ROUND_HALF_UP")
    else:
        raise ValueError(f"不正なmode: {mode}")
    return n * unit


def shift_ticks(price, ticks: int, price_range_group=DEFAULT_GROUP) -> Decimal:
    """価格を指定ティック数ぶん動かす（正=上、負=下）。

    刻みが変わる水準をまたぐ場合に備え、1ティックずつ動かして都度単位を引き直す。
    例: 10003グループで1000円から+1ティック → 1000円は0.1円刻みなので1000.1円。
    """
    p = round_to_tick(price, price_range_group, "nearest")
    step = 1 if ticks >= 0 else -1
    for _ in range(abs(int(ticks))):
        unit = tick_size(p, price_range_group)
        if step < 0:
            # 下げるときは「1つ下の刻み水準」に入る場合があるので、
            # 動かした後の価格で単位を引き直す（境界での刻み違いを吸収）
            nxt = p - unit
            if nxt > 0 and tick_size(nxt, price_range_group) != unit:
                nxt = round_to_tick(nxt, price_range_group, "down")
            p = nxt
        else:
            p = p + unit
        if p <= 0:
            return Decimal("0")
    return p


def shift_pct(price, pct, price_range_group=DEFAULT_GROUP, mode=None) -> Decimal:
    """価格を pct%（正=上、負=下）動かし、呼値の刻みに丸める。

    mode 未指定なら「不利側」に丸める:
      下方向（売りを確実に約定させたい）→ 切り下げ
      上方向（買いを確実に約定させたい）→ 切り上げ
    例: 買い気配1000円から -3% → 970円（1円刻みならそのまま）
    """
    p = Decimal(str(price)) * (Decimal("1") + Decimal(str(pct)) / Decimal("100"))
    if mode is None:
        mode = "down" if Decimal(str(pct)) < 0 else "up"
    return round_to_tick(p, price_range_group, mode)


def ticks_between(price_a, price_b, price_range_group=DEFAULT_GROUP) -> Decimal:
    """2つの価格が何ティック離れているかの概算（絶対値）。

    水準をまたぐと厳密な整数にならないため、判定用の目安として使う。
    """
    a, b = Decimal(str(price_a)), Decimal(str(price_b))
    unit = tick_size(min(a, b), price_range_group)
    return abs(a - b) / unit
