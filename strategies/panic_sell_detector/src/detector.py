"""OVERからの売り降ろし（投げ売り）検知ロジック。

検知する現象:
  当日安値圏で、板のOVER（売り気配10本より上に隠れている売り注文の合計 = OverSellQty）が
  一気に数千株以上減少し、その後短時間内に「ほぼ同数」の売りが
    (A) 売り気配周辺（Sell1〜N）に指し直され、さらに買われていく（吸収）
    (B) 買い気配に直接ぶつけられる（売り方起点の約定として出来高に出る）
  のいずれかで市場に降りてきた場合、「上で待っていた大口売り手が投げに来た」とみなして通知する。

  OVER減少だけでは「売り注文の取り消し（むしろ強気材料）」と区別できないため、
  減少量とほぼ同数が市場に現れたことの突き合わせを必須条件とする。

板の窓シフトによる偽検知の回避:
  価格が動くと表示10本の窓がずれ、OVERとの間で注文が機械的に出入りする。
  窓が上にずれて現れる注文は売り気配の上のほう（Sell7〜10側）に出現するため、
  「売り直し」の判定は最良売り気配に近い下位レベル（既定: Sell1〜3）の数量増加のみを数える。
  また数量の照合は価格ごとの辞書比較で行い、表示位置のずれの影響を受けない。

前提:
  kabuステーションAPIのPUSH配信スナップショットの差分に基づく近似。
  歩み値（1約定ごとのデータ）は取得できないため、出来高差分・板差分で推定する。
  詳細は ../README.md を参照。
"""
from dataclasses import dataclass, field


@dataclass
class PendingEvent:
    """OVER急減イベント。減少量と同数の売りが降りてくるかを監視時間窓の間追跡する。"""
    qty_removed: float
    start_time: object                 # datetime
    requote_added: float = 0.0         # 売り気配周辺に上乗せされた数量の累計
    requoted: bool = False             # ほぼ同数の売り直しを確認済みか
    buyer_volume_after_requote: float = 0.0  # 売り直し確認後の買い方起点の約定数量累計
    seller_volume: float = 0.0         # イベント発生以降の売り方起点の約定数量累計
    done: bool = False                 # 通知済み（DUMP/ABSORBED）


@dataclass
class SymbolState:
    prev_over_sell: float = None
    prev_sell_board: dict = field(default_factory=dict)  # {price: qty} Sell1..Sell10
    prev_sell1_price: float = None
    prev_buy1_price: float = None
    prev_volume: float = None
    events: list = field(default_factory=list)


class PanicSellDetector:
    def __init__(self, over_drop_threshold: float, match_tolerance: float,
                 match_window_seconds: float, low_zone_pct: float,
                 requote_levels: int = 3, requote_consumed_fraction: float = 0.5,
                 price_tolerance_ticks: int = 0, tick_size: float = 1.0,
                 large_over_threshold: float = 100000, large_over_drop_pct: float = 0.10):
        self.over_drop_threshold = over_drop_threshold
        self.large_over_threshold = large_over_threshold
        self.large_over_drop_pct = large_over_drop_pct
        self.match_tolerance = match_tolerance
        self.match_window = match_window_seconds
        self.low_zone_pct = low_zone_pct
        self.requote_levels = requote_levels
        self.requote_consumed_fraction = requote_consumed_fraction
        self.price_tolerance = price_tolerance_ticks * tick_size
        self.states = {}

    def _state(self, symbol: str) -> SymbolState:
        return self.states.setdefault(symbol, SymbolState())

    def update(self, symbol: str, msg_time, current_price, low_price,
               over_sell_qty, sell_levels, buy1_price, sell1_price, trading_volume):
        """1件のPUSHメッセージを処理し、発火したアラートのリストを返す。

        sell_levels: [(price, qty), ...] Sell1..Sell10（気配の低い順）
        """
        state = self._state(symbol)
        alerts = []

        cur_board = {p: q for p, q in sell_levels if p is not None and q is not None}

        # --- 監視時間窓を過ぎたイベントを破棄 ---
        if msg_time is not None:
            state.events = [
                e for e in state.events
                if not e.done and (msg_time - e.start_time).total_seconds() <= self.match_window
            ]

        first_message = state.prev_over_sell is None and not state.prev_sell_board
        if not first_message:
            # --- 差分の計算（すべて「直前のPUSH時点」との比較） ---
            volume_delta = 0.0
            if trading_volume is not None and state.prev_volume is not None:
                volume_delta = max(0.0, trading_volume - state.prev_volume)

            # 約定の売買方向の分類（直前の最良気配と比較）
            seller_delta = 0.0
            buyer_delta = 0.0
            if volume_delta > 0 and current_price is not None:
                if state.prev_buy1_price is not None and current_price <= state.prev_buy1_price + self.price_tolerance:
                    seller_delta = volume_delta
                elif state.prev_sell1_price is not None and current_price >= state.prev_sell1_price - self.price_tolerance:
                    buyer_delta = volume_delta

            # 売り気配周辺（下位requote_levels本）への数量上乗せ。価格ごとに比較する
            near_prices = [p for p, q in sell_levels[:self.requote_levels] if p is not None]
            near_ask_increase = sum(
                max(0.0, cur_board[p] - state.prev_sell_board.get(p, 0.0)) for p in near_prices
            )

            # --- 新しいOVER急減イベントの検知 ---
            if (over_sell_qty is not None and state.prev_over_sell is not None
                    and current_price is not None and low_price is not None and low_price > 0
                    and msg_time is not None):
                over_drop = state.prev_over_sell - over_sell_qty
                in_low_zone = current_price <= low_price * (1 + self.low_zone_pct)
                # 閾値の条件分岐: OVERが大きい銘柄（直前のOVER > large_over_threshold）では
                # 絶対株数ではなく「直前のOVERに対する割合」で判定する
                if state.prev_over_sell > self.large_over_threshold:
                    drop_threshold = state.prev_over_sell * self.large_over_drop_pct
                else:
                    drop_threshold = self.over_drop_threshold
                if over_drop >= drop_threshold and in_low_zone:
                    state.events.append(PendingEvent(qty_removed=over_drop, start_time=msg_time))

            # --- 各イベントへ差分を反映し、成立条件を判定 ---
            # （同一メッセージ内でOVER減少と売り直しが同時に起きるケースを拾うため、
            #   イベント生成後に反映する）
            min_match = lambda qty: qty * (1 - self.match_tolerance)
            for event in state.events:
                if event.done:
                    continue

                # ケースB: 買い気配へのぶつけ（売り方起点の約定）が累計でほぼ同数に達した
                event.seller_volume += seller_delta
                if event.seller_volume >= min_match(event.qty_removed):
                    event.done = True
                    alerts.append({
                        "symbol": symbol, "stage": "DUMP",
                        "qty_removed": event.qty_removed,
                        "matched_qty": event.seller_volume,
                        "price": current_price, "time": msg_time,
                    })
                    continue

                # ケースA-1: 売り気配周辺への売り直しが累計でほぼ同数に達した
                # （内部判定のみ。通知はABSORBED成立時にまとめて行う）
                if not event.requoted:
                    event.requote_added += near_ask_increase
                    if event.requote_added >= min_match(event.qty_removed):
                        event.requoted = True
                else:
                    # ケースA-2: 売り直し確認後、買い方起点の約定で消化されていく（吸収）
                    event.buyer_volume_after_requote += buyer_delta
                    if event.buyer_volume_after_requote >= event.qty_removed * self.requote_consumed_fraction:
                        event.done = True
                        alerts.append({
                            "symbol": symbol, "stage": "ABSORBED",
                            "qty_removed": event.qty_removed,
                            "matched_qty": event.buyer_volume_after_requote,
                            "price": current_price, "time": msg_time,
                        })

        # --- スナップショットを更新 ---
        if over_sell_qty is not None:
            state.prev_over_sell = over_sell_qty
        if cur_board:
            state.prev_sell_board = cur_board
        if sell1_price is not None:
            state.prev_sell1_price = sell1_price
        if buy1_price is not None:
            state.prev_buy1_price = buy1_price
        if trading_volume is not None:
            state.prev_volume = trading_volume

        return alerts
