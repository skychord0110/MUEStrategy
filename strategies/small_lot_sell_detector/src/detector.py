"""気配値に対してぶつかった小口売りの連続検知ロジック。

判定の考え方:
  「直前のPUSH時点の最良買い気配」と「新しく発生した約定の価格」を比較する。
  - 約定価格 <= 直前の買い気配 (+許容幅) → 売り方起点の約定（気配にぶつかった売り。
    買い気配を食い破って下の価格で約定したケースも含む）
  - それ以外（売り気配を買い上がる約定など） → 売りの連続が途切れたとみなしリセット
  買い気配の価格が動いてもストリークは維持し、「その時々の買い気配に小口売りが
  ぶつけられ続けているか」を数える。価格下落による制限は設けない（ユーザー確認済み）。

前提（公式ドキュメント・GitHub Issue #268で確認済み）:
  kabuステーションAPIのPUSH配信には歩み値（1約定ごとの約定株数）を返す
  専用フィールドが存在しない。そのため、当日累計出来高(TradingVolume)の
  差分を「直近の推定約定株数」として扱う近似ロジックとする。
  複数の約定が1回のPUSH更新に合算される場合があり、この近似には限界がある。
  詳細は ../README.md の「重要な前提」を参照。
"""
from dataclasses import dataclass, field


@dataclass
class SymbolState:
    last_trading_volume: int = None
    prev_buy_price: float = None   # 直前のPUSH時点の最良買い気配
    last_small_sell_time: object = None  # 直前に観測した小口売りの時刻（カウント外のバースト分も更新する）
    streak: int = 0
    fired_tiers: set = field(default_factory=set)


class SmallLotSellDetector:
    def __init__(self, small_lot_threshold: int, price_tolerance_ticks: int, alert_tiers: list,
                 tick_size: float = 1.0, min_hit_interval_seconds: float = 1.0):
        self.small_lot_threshold = small_lot_threshold
        self.price_tolerance = price_tolerance_ticks * tick_size
        self.alert_tiers = sorted(alert_tiers, key=lambda t: t["consecutive"])
        self.min_hit_interval = min_hit_interval_seconds
        self.states = {}

    def _state(self, symbol: str) -> SymbolState:
        return self.states.setdefault(symbol, SymbolState())

    def update(self, symbol: str, current_price, trading_volume, buy_price, trade_time=None):
        """1件のPUSHメッセージを処理し、発火したアラート（あれば）のリストを返す。

        buy_price: このメッセージに載っている最良買い気配（Buy1.Price）
        trade_time: この約定の時刻（datetime）。PUSHのTradingVolumeTime由来を想定。
        """
        state = self._state(symbol)
        alerts = []

        if trading_volume is None:
            return alerts

        if state.last_trading_volume is None:
            state.last_trading_volume = trading_volume
            state.prev_buy_price = buy_price
            return alerts

        volume_delta = trading_volume - state.last_trading_volume
        state.last_trading_volume = trading_volume

        # 約定の分類には「約定前の板」＝直前のPUSH時点の買い気配を使う。
        # このメッセージの気配は約定後の状態なので、次回の分類用に保存して入れ替える。
        prev_buy = state.prev_buy_price
        if buy_price is not None:
            state.prev_buy_price = buy_price

        if volume_delta <= 0:
            # 出来高が増えていない=約定を伴わない板更新。ストリークは維持したまま何もしない。
            return alerts
        if current_price is None or prev_buy is None:
            return alerts

        is_sell_initiated = current_price <= prev_buy + self.price_tolerance
        is_small_lot = volume_delta <= self.small_lot_threshold

        if is_sell_initiated and is_small_lot:
            # アルゴによる分散売り対策:
            # 直前の小口売り（カウントされなかったバースト分も含む）から
            # min_hit_interval秒以上離れていない場合は、カウントもリセットもしない。
            # last_small_sell_timeはバースト継続の検知のため毎回更新する。
            # → 0.5秒間隔などで連射されている間はカウントが進まず、
            #   売りが1秒以上途切れてから来た次の小口売りで初めて次のカウントが入る。
            prev_small_sell_time = state.last_small_sell_time
            state.last_small_sell_time = trade_time
            if (trade_time is not None and prev_small_sell_time is not None
                    and (trade_time - prev_small_sell_time).total_seconds() < self.min_hit_interval):
                return alerts

            state.streak += 1
            for tier in self.alert_tiers:
                if state.streak == tier["consecutive"] and tier["consecutive"] not in state.fired_tiers:
                    state.fired_tiers.add(tier["consecutive"])
                    alerts.append({
                        "symbol": symbol,
                        "tier": tier["label"],
                        "streak": state.streak,
                        "price": current_price,
                        "buy_price": prev_buy,
                        "last_volume_delta": volume_delta,
                    })
        else:
            # 大口の約定、または売り気配を買い上がる約定（買い方起点）→ 連続が途切れた
            self._reset(state)

        return alerts

    def _reset(self, state: SymbolState):
        state.streak = 0
        state.fired_tiers = set()
