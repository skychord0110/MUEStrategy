"""歩み値（1約定ごとのティック）を入力にした「定期買い集め」検知ロジック。

データ源は楽天証券マーケットスピードII RSS の RssTickList 関数（時刻・出来高・約定値）。
kabuステーション版（strategies/periodic_buy_detector）と検知の狙いは同じだが、
入力が「当日累計出来高の差分」ではなく「1約定ごとの正確な時刻・株数」になる点が異なる。

検知する現象（kabu版と同一）:
  ある約定（多くは売り方起点）の丁度 delay_seconds 秒後（既定10秒）に、買い方起点の約定が
  入る——という「N秒後の買い」イベントが、1日のうちに何度も（既定5回以上）繰り返される状態。

売買方向の推定（重要な近似）:
  RssTickList の歩み値には売買の別が含まれない。そこで **ティックルール** で近似する:
    - 約定値が直前の約定値より高い（アップティック） → 買い方起点 "buy"
    - 約定値が直前の約定値より低い（ダウンティック） → 売り方起点 "sell"
    - 同値（ゼロティック） → 直前の方向を引き継ぐ
  気配（板）と突き合わせる方式より精度は落ちるが、歩み値だけで判定できる標準的手法。
  詳細と限界は ../README.md を参照。
"""
from collections import deque
from dataclasses import dataclass, field


def classify_tick(price, last_price, last_side):
    """ティックルールで約定方向を推定する。"buy"/"sell"/"unknown" を返す。"""
    if price is None:
        return "unknown"
    if last_price is None:
        return "unknown"
    if price > last_price:
        return "buy"
    if price < last_price:
        return "sell"
    return last_side or "unknown"  # ゼロティックは直前の方向を引き継ぐ


@dataclass
class SymbolState:
    history: deque = field(default_factory=deque)  # (time, side) の直近履歴（lookback用）
    day: object = None
    occurrences: int = 0
    delay_sum: float = 0.0
    last_occurrence_time: object = None
    fired_tiers: set = field(default_factory=set)


class PeriodicBuyTickDetector:
    def __init__(self, delay_seconds: float = 10.0,
                 delay_tolerance_seconds: float = 1.0,
                 trigger_side: str = "sell",
                 alert_tiers: list = None,
                 min_lot: int = 0,
                 min_occurrence_gap_seconds: float = 2.0):
        self.delay = delay_seconds
        self.delay_tol = delay_tolerance_seconds
        self.trigger_side = trigger_side  # "sell" / "buy" / "any"
        default_tiers = [{"occurrences": 5, "label": "WATCH"},
                         {"occurrences": 10, "label": "STRONG"}]
        self.alert_tiers = sorted(alert_tiers or default_tiers, key=lambda t: t["occurrences"])
        self.min_lot = min_lot
        self.min_occurrence_gap = min_occurrence_gap_seconds
        self.history_window = delay_seconds + delay_tolerance_seconds + 5.0
        self.states = {}

    def _state(self, symbol: str) -> SymbolState:
        return self.states.setdefault(symbol, SymbolState())

    def _trigger_matches(self, side: str) -> bool:
        if self.trigger_side == "any":
            return True
        return side == self.trigger_side

    def on_trade(self, symbol: str, trade_time, price, volume, side) -> list:
        """1約定を処理し、発火したアラート（あれば）のリストを返す。

        trade_time: datetime（歩み値の時刻＋当日日付）
        side: "buy"/"sell"/"unknown"（ティックルール等で推定済み）
        """
        state = self._state(symbol)
        alerts = []
        if trade_time is None or price is None:
            return alerts

        day = trade_time.date()
        if state.day != day:
            state.day = day
            state.occurrences = 0
            state.delay_sum = 0.0
            state.last_occurrence_time = None
            state.fired_tiers = set()
            state.history.clear()

        if side == "buy" and (volume is None or volume >= self.min_lot):
            lo = self.delay - self.delay_tol
            hi = self.delay + self.delay_tol
            best_lag = None
            for h_time, h_side in state.history:
                lag = (trade_time - h_time).total_seconds()
                if lo <= lag <= hi and self._trigger_matches(h_side):
                    if best_lag is None or abs(lag - self.delay) < abs(best_lag - self.delay):
                        best_lag = lag
            if best_lag is not None:
                if (state.last_occurrence_time is None
                        or (trade_time - state.last_occurrence_time).total_seconds()
                        >= self.min_occurrence_gap):
                    state.occurrences += 1
                    state.delay_sum += best_lag
                    state.last_occurrence_time = trade_time
                    for tier in self.alert_tiers:
                        if (state.occurrences == tier["occurrences"]
                                and tier["occurrences"] not in state.fired_tiers):
                            state.fired_tiers.add(tier["occurrences"])
                            alerts.append({
                                "symbol": symbol,
                                "tier": tier["label"],
                                "occurrences": state.occurrences,
                                "avg_delay": state.delay_sum / state.occurrences,
                                "trigger_side": self.trigger_side,
                                "price": price,
                            })

        if side != "unknown" or self.trigger_side == "any":
            state.history.append((trade_time, side))
        while state.history and (trade_time - state.history[0][0]).total_seconds() > self.history_window:
            state.history.popleft()

        return alerts


class TickDeduper:
    """RssTickList は毎回「直近N本」を返し重複するため、新規約定だけを取り出す。

    バッチは古い順（oldest→newest）に正規化した [(time_key, price, volume), ...] を渡す。
    time_key は歩み値の時刻文字列など、同一約定を識別できる値。
    """

    def __init__(self):
        self.last_key = None  # 直近に emit した約定の (time_key, price, volume)

    def new_trades(self, batch: list) -> list:
        if not batch:
            return []
        if self.last_key is None:
            emitted = list(batch)
        else:
            idx = None
            for i in range(len(batch) - 1, -1, -1):  # 末尾側から一致位置を探す
                if batch[i] == self.last_key:
                    idx = i
                    break
            if idx is not None:
                emitted = batch[idx + 1:]
            else:
                # オーバーラップを見失った（N本を超えて進んだ等）。時刻での best-effort。
                last_time = self.last_key[0]
                emitted = [t for t in batch if t[0] > last_time]
        self.last_key = batch[-1]
        return emitted
