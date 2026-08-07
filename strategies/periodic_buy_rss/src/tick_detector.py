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
    """ティックルールで約定方向を推定する。"buy"/"sell"/"flat"/"unknown" を返す。

    アップティック→buy、ダウンティック→sell、同値（ゼロティック）→"flat"。
    "flat" を独立の値にしているのは、買い集めアルゴが同じ売り気配を叩き続けると
    価格が動かず、直前の方向を引き継ぐ従来実装では検知できなかったため
    （直前が売りなら買いを売りと誤判定してしまう）。
    ゼロティックを買いとみなすかは buy_side_mode で選択する。
    """
    if price is None or last_price is None:
        return "unknown"
    if price > last_price:
        return "buy"
    if price < last_price:
        return "sell"
    return "flat"


def is_buy_like(side, mode="non_down"):
    """「買い側」とみなすか。歩み値には売買の別がないための近似。

    strict   : アップティックのみ（価格が動かないアルゴ買いは取りこぼす）
    non_down : アップティック＋同値（既定。売り気配を叩き続ける買いを拾える）
    any      : 方向を問わない（最も緩い。誤検知は増える）
    """
    if mode == "any":
        return side != "unknown"
    if mode == "strict":
        return side == "buy"
    return side in ("buy", "flat")


def is_trigger_like(side, trigger_side):
    """トリガー約定とみなすか。"""
    if trigger_side == "any":
        return side != "unknown"
    if trigger_side == "sell":
        # 売り方起点。価格が動かない売りも拾えるよう flat を含める
        return side in ("sell", "flat")
    return side == trigger_side


@dataclass
class SymbolState:
    history: deque = field(default_factory=deque)  # (time, side) の直近履歴（lookback用）
    day: object = None
    occurrences: int = 0
    delay_sum: float = 0.0
    lot_sum: float = 0.0
    last_occurrence_time: object = None
    fired_tiers: set = field(default_factory=set)


class PeriodicBuyTickDetector:
    def __init__(self, delay_seconds: float = 10.0,
                 delay_tolerance_seconds: float = 0.0,
                 trigger_side: str = "any",
                 alert_tiers: list = None,
                 min_lot: int = 0,
                 min_occurrence_gap_seconds: float = 2.0,
                 buy_side_mode: str = "non_down",
                 lot_similarity_pct: float = 0.0):
        self.delay = delay_seconds
        self.delay_tol = delay_tolerance_seconds
        self.trigger_side = trigger_side  # "sell" / "buy" / "any"
        self.buy_side_mode = buy_side_mode      # "strict" / "non_down" / "any"
        self.lot_similarity_pct = lot_similarity_pct  # >0でロットの揃い方も要求
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
        return is_trigger_like(side, self.trigger_side)

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
            state.lot_sum = 0.0
            state.last_occurrence_time = None
            state.fired_tiers = set()
            state.history.clear()

        if is_buy_like(side, self.buy_side_mode) and (volume is None or volume >= self.min_lot):
            # 歩み値の時刻は秒未満切り捨てのため、tol=0 なら「表示上ちょうどN秒差」だけを数える。
            # 浮動小数の誤差で取りこぼさないよう微小なイプシロンを見込む。
            eps = 1e-6
            lo = self.delay - self.delay_tol - eps
            hi = self.delay + self.delay_tol + eps
            best_lag = None
            for h_time, h_side, h_vol in state.history:
                lag = (trade_time - h_time).total_seconds()
                if not (lo <= lag <= hi) or not self._trigger_matches(h_side):
                    continue
                # ロットの揃い方も要求する場合（アルゴは同じ株数で刻むことが多い）
                if self.lot_similarity_pct > 0 and volume and state.lot_sum and state.occurrences:
                    avg = state.lot_sum / state.occurrences
                    if abs(volume - avg) > avg * self.lot_similarity_pct:
                        continue
                if best_lag is None or abs(lag - self.delay) < abs(best_lag - self.delay):
                    best_lag = lag
            if best_lag is not None:
                if (state.last_occurrence_time is None
                        or (trade_time - state.last_occurrence_time).total_seconds()
                        >= self.min_occurrence_gap):
                    state.occurrences += 1
                    state.delay_sum += best_lag
                    state.last_occurrence_time = trade_time
                    if volume:
                        state.lot_sum += volume
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

        # 履歴には方向が判定できた約定をすべて残す（flat も含む＝同値約定もトリガーになりうる）
        if side != "unknown":
            state.history.append((trade_time, side, volume))
        while state.history and (trade_time - state.history[0][0]).total_seconds() > self.history_window:
            state.history.popleft()

        return alerts


class TickDeduper:
    """RssTickList は毎回「直近N本」を返し重複するため、新規約定だけを取り出す。

    バッチは古い順（oldest→newest）に正規化した [(time_key, volume, price), ...] を渡す。

    照合の考え方:
      1行だけを目印にすると、同じ (時刻,出来高,約定値) が再出現したときに誤った位置で
      一致してしまい、間の約定を取りこぼす。買い集めアルゴは「同じ株数を同値で刻む」ため
      この重複がまさに起こりやすい。そこで**直前バッチの末尾 tail_size 行の並び**を
      目印にし、その並びが現れる位置を末尾側から探す（複数行一致なら誤検出しにくい）。
    """

    def __init__(self, tail_size: int = 5):
        self.tail_size = max(1, tail_size)
        self.tail = []        # 直近に emit した末尾の並び
        self.last_time = None  # 取りこぼし時のフォールバック用

    @staticmethod
    def _find_last_subsequence(batch, tail):
        """batch の中で tail（連続した並び）が最後に出現する開始位置を返す。無ければ None。"""
        n, m = len(batch), len(tail)
        if m == 0 or m > n:
            return None
        for i in range(n - m, -1, -1):
            if batch[i:i + m] == tail:
                return i
        return None

    def new_trades(self, batch: list) -> list:
        if not batch:
            return []
        if not self.tail:
            emitted = list(batch)
        else:
            idx = self._find_last_subsequence(batch, self.tail)
            if idx is not None:
                emitted = batch[idx + len(self.tail):]
            else:
                # オーバーラップを見失った（N本を超えて進んだ／板寄せで飛んだ等）。
                # 時刻での best-effort に切り替える。
                emitted = ([t for t in batch if t[0] > self.last_time]
                           if self.last_time is not None else list(batch))
        self.tail = batch[-self.tail_size:]
        self.last_time = batch[-1][0]
        return emitted
