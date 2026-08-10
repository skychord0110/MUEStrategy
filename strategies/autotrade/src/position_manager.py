"""建玉1件の執行を管理する状態機械。API非依存の純ロジック。

「状態機械が判断し、Executorが実行する」という分担にしてある。
on_tick() は板情報と時刻を受け取り、**実行すべきアクションのリスト**を返すだけで、
自分では発注しない。これにより kabuステーションが無くても全経路をテストできる。

状態遷移:

    HOLDING ──(売り気配が目標のNティック以内)──→ TP_PLACED
       │                                            │
       │←────────(利確を取消して損切りへ)───────────┤
       │                                            │
       ├──(現在値が損切りトリガーに到達)──→ STOP_CHASING
       │                                     （気配を追いかけて指し直し）
       │                                            │
       └──(15:26 到達)──→ CLOSING_OUT ←─────────────┘
                          （買い気配−3%で強制手仕舞い）

    どの状態からでも約定すれば CLOSED。

執行仕様（config の take_profit / stop_loss / close_out に対応）:
  利確  : 建玉と同時に注文を出さない。売り気配が目標価格の reveal_within_ticks 以内に
          接近してから目標価格に指値（他者のアルゴに手の内を読まれないため）
  損切り: トリガー到達後、そのときの買い気配の位置で執行方法が分岐する
          (A) 買い気配がトリガーの hit_bid_within_ticks 以内 → 買い気配にぶつける
              約定せず気配が下がったら、同じ条件で買い気配に指し直す
          (B) それより下 → 仲値に指値。retry秒ごとに1ティックずつ下げて指し直す
          いずれも エントリー価格×(1−max_chase_pct_from_entry/100) を下限とする
  引け  : deadline までに決済できなければ force_time に 買い気配−aggression_pct% で指値
"""
from dataclasses import dataclass, field
from datetime import datetime, time as dtime
from decimal import Decimal

import order_builder as ob
import tick_size as T

# 状態
HOLDING = "HOLDING"            # 建玉あり・決済注文なし
TP_PLACED = "TP_PLACED"        # 利確の指値を出している
STOP_CHASING = "STOP_CHASING"  # 損切りで気配を追いかけている
CLOSING_OUT = "CLOSING_OUT"    # 引け際の強制手仕舞い
CLOSED = "CLOSED"              # 決済済み

# 損切りの執行モード
MODE_HIT_BID = "A"    # 買い気配にぶつける
MODE_MID = "B"        # 仲値から下げて追いかける


@dataclass
class Snapshot:
    """PUSHから作る板のスナップショット。"""
    time: datetime
    price: float = None   # 現在値
    bid: float = None     # 買い気配（Buy1）
    ask: float = None     # 売り気配（Sell1）


def _hhmm(s):
    if isinstance(s, dtime):
        return s
    h, m = str(s).split(":")[:2]
    return dtime(int(h), int(m))


@dataclass
class PositionManager:
    symbol: str
    entry_price: float
    qty: int
    config: dict                      # autotrade の config 全体
    price_range_group: str = "10000"
    account_type: int = 4
    strategy: str = ""
    log: object = None

    state: str = HOLDING
    stop_mode: str = None
    order_id: str = None              # 現在出している決済注文
    last_order_price: Decimal = None
    stop_retry_step: int = 0          # 仲値から何ティック下げたか
    last_stop_order_time: datetime = None
    stop_triggered: bool = False
    fills: list = field(default_factory=list)

    # ── 価格の基準値 ──

    def _cfg(self, section, key, default=None):
        return (self.config.get(section) or {}).get(key, default)

    @property
    def take_profit_pct(self):
        return self._cfg("take_profit", "pct") or self._strategy_pct("take_profit_pct", 2.0)

    @property
    def stop_loss_pct(self):
        return self._cfg("stop_loss", "pct") or self._strategy_pct("stop_loss_pct", 2.0)

    def _strategy_pct(self, key, default):
        """戦略側の設定（runner/config.yaml の strategies）を使う場合の受け皿。"""
        return (self.config.get("_strategy_params") or {}).get(key, default)

    @property
    def target_price(self) -> Decimal:
        """利確の目標価格（呼値に切り上げ＝有利側に置かない）。"""
        return T.shift_pct(self.entry_price, float(self.take_profit_pct),
                           self.price_range_group, mode="up")

    @property
    def stop_trigger_price(self) -> Decimal:
        """損切りの発動価格。"""
        return T.shift_pct(self.entry_price, -float(self.stop_loss_pct),
                           self.price_range_group, mode="down")

    @property
    def chase_floor(self) -> Decimal:
        """追いかけの下限価格（エントリー価格から max_chase_pct_from_entry% 下）。"""
        pct = float(self._cfg("stop_loss", "max_chase_pct_from_entry", 3.0))
        return T.shift_pct(self.entry_price, -pct, self.price_range_group, mode="down")

    def _tick(self, price):
        return T.tick_size(price, self.price_range_group)

    def _clamp_floor(self, price: Decimal) -> Decimal:
        """下限より下には出さない。"""
        floor = self.chase_floor
        return floor if Decimal(str(price)) < floor else Decimal(str(price))

    # ── アクション生成のヘルパ ──

    @staticmethod
    def _place(order, intent):
        return {"type": "place", "order": order, "intent": intent}

    @staticmethod
    def _cancel(order_id, reason):
        return {"type": "cancel", "order_id": order_id, "reason": reason}

    def _sell(self, price, kind, **kw):
        price = self._clamp_floor(price)
        builder = {"tp": ob.take_profit_sell, "hit": ob.stop_hit_bid_sell,
                   "mid": ob.stop_mid_price_sell, "close": ob.close_out_sell}[kind]
        return builder(self.symbol, self.qty, price,
                       account_type=self.account_type, **kw)

    # ── メイン ──

    def on_tick(self, snap: Snapshot) -> list:
        """板の更新1件を処理し、実行すべきアクションのリストを返す。"""
        if self.state == CLOSED:
            return []
        acts = []

        # 1) 引け際の強制手仕舞い（最優先）
        force_t = _hhmm(self._cfg("close_out", "force_time", "15:26"))
        if snap.time.time() >= force_t and self.state != CLOSING_OUT:
            if self.order_id:
                acts.append(self._cancel(self.order_id, "引け手仕舞いへ切替"))
            pct = float(self._cfg("close_out", "aggression_pct", 3.0))
            base = snap.bid if snap.bid else snap.price
            if base:
                # 引け手仕舞いは確実性が目的なので追いかけ下限を適用しない
                px = T.shift_pct(base, -pct, self.price_range_group, mode="down")
                acts.append(self._place(
                    ob.close_out_sell(self.symbol, self.qty, px, pct=pct,
                                      account_type=self.account_type),
                    "引け手仕舞い"))
                self.state = CLOSING_OUT
                self.last_order_price = px
            return acts

        if self.state == CLOSING_OUT:
            return acts  # 板寄せ中。約定を待つ

        # 2) 損切りトリガーの判定（未発動なら）
        if not self.stop_triggered and snap.price is not None \
                and Decimal(str(snap.price)) <= self.stop_trigger_price:
            self.stop_triggered = True
            if self.order_id:
                acts.append(self._cancel(self.order_id, "損切りのため利確を取消"))
                self.order_id = None
            acts += self._start_stop(snap)
            return acts

        # 3) 損切りの追いかけ中
        if self.state == STOP_CHASING:
            return acts + self._chase_stop(snap)

        # 4) 利確の開示判定（まだ出していないとき）
        if self.state == HOLDING and snap.ask is not None:
            reveal = int(self._cfg("take_profit", "reveal_within_ticks", 2))
            tick = self._tick(self.target_price)
            threshold = self.target_price - tick * reveal
            if Decimal(str(snap.ask)) >= threshold:
                order = self._sell(self.target_price, "tp")
                acts.append(self._place(order, "利確の指値を開示"))
                self.state = TP_PLACED
                self.last_order_price = self.target_price
        return acts

    def _start_stop(self, snap: Snapshot) -> list:
        """損切り発動。買い気配の位置で (A)/(B) を決める。"""
        trigger = self.stop_trigger_price
        within = int(self._cfg("stop_loss", "hit_bid_within_ticks", 1))
        self.state = STOP_CHASING
        self.stop_retry_step = 0
        self.last_stop_order_time = snap.time

        bid = Decimal(str(snap.bid)) if snap.bid else None
        if bid is not None:
            tick = self._tick(bid)
            if bid >= trigger - tick * within:
                # (A) 買い気配がトリガーの N ティック以内 → ぶつける
                self.stop_mode = MODE_HIT_BID
                px = self._clamp_floor(bid)
                self.last_order_price = px
                return [self._place(self._sell(bid, "hit"),
                                    f"損切りA(買い気配{bid}にぶつけ)")]
        # (B) 気配が離れている（または気配不明）→ 仲値
        self.stop_mode = MODE_MID
        px = self._mid(snap)
        if px is None:
            return []
        self.last_order_price = px
        return [self._place(self._sell(px, "mid"), f"損切りB(仲値{px})")]

    def _mid(self, snap: Snapshot, step: int = 0):
        """仲値からstepティック下げた価格（呼値に切り下げ）。"""
        if snap.bid is None or snap.ask is None:
            base = snap.bid if snap.bid is not None else snap.price
            if base is None:
                return None
            mid = Decimal(str(base))
        else:
            mid = (Decimal(str(snap.bid)) + Decimal(str(snap.ask))) / 2
        mode = self._cfg("stop_loss", "mid_price_rounding", "down")
        px = T.round_to_tick(mid, self.price_range_group, mode)
        if step:
            px = T.shift_ticks(px, -step, self.price_range_group)
        return self._clamp_floor(px)

    def _chase_stop(self, snap: Snapshot) -> list:
        """損切りの指し直し判定。"""
        if self.stop_mode == MODE_HIT_BID:
            # 気配が動いたら、その気配に指し直す（同じ条件で追いかける）
            if snap.bid is None:
                return []
            new_px = self._clamp_floor(Decimal(str(snap.bid)))
            if self.last_order_price is not None and new_px == self.last_order_price:
                return []
            acts = []
            if self.order_id:
                acts.append(self._cancel(self.order_id, "気配が動いたため指し直し"))
                self.order_id = None
            self.last_order_price = new_px
            self.last_stop_order_time = snap.time
            acts.append(self._place(self._sell(new_px, "hit"),
                                    f"損切りA(気配{new_px}へ指し直し)"))
            return acts

        # (B) 一定秒ごとに1ティックずつ下げる
        retry = float(self._cfg("stop_loss", "mid_price_retry_seconds", 10))
        if self.last_stop_order_time is None:
            self.last_stop_order_time = snap.time
            return []
        if (snap.time - self.last_stop_order_time).total_seconds() < retry:
            return []
        max_steps = int(self._cfg("stop_loss", "mid_price_max_steps", 10))
        step_ticks = int(self._cfg("stop_loss", "mid_price_step_ticks", 1))
        next_step = min(self.stop_retry_step + step_ticks, max_steps)
        new_px = self._mid(snap, next_step)
        if new_px is None:
            return []
        self.last_stop_order_time = snap.time
        # 下限に張り付いたら価格は変えず、同じ価格で出し続ける（指し直しは不要）
        if self.last_order_price is not None and new_px == self.last_order_price:
            self.stop_retry_step = next_step
            return []
        acts = []
        if self.order_id:
            acts.append(self._cancel(self.order_id, f"{retry:.0f}秒約定せず指し直し"))
            self.order_id = None
        self.stop_retry_step = next_step
        self.last_order_price = new_px
        acts.append(self._place(self._sell(new_px, "mid", step=next_step),
                                f"損切りB(仲値-{next_step}ティック={new_px})"))
        return acts

    # ── 外部からの通知 ──

    def on_order_placed(self, order_id):
        self.order_id = order_id

    def on_order_canceled(self, order_id=None):
        if order_id is None or order_id == self.order_id:
            self.order_id = None

    def on_filled(self, qty, price, time=None):
        """決済が約定した。"""
        self.fills.append({"qty": qty, "price": price, "time": time})
        filled = sum(f["qty"] for f in self.fills)
        if filled >= self.qty:
            self.state = CLOSED
            self.order_id = None
        return self.state

    def pnl_pct(self):
        if not self.fills:
            return None
        total = sum(f["qty"] * f["price"] for f in self.fills)
        n = sum(f["qty"] for f in self.fills)
        return (total / n - self.entry_price) / self.entry_price * 100

    def describe(self):
        return (f"{self.symbol} {self.qty}株 @{self.entry_price} "
                f"[{self.state}] 目標{self.target_price} 損切り{self.stop_trigger_price} "
                f"下限{self.chase_floor}")
