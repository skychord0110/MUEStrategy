"""自動売買の調整役。シグナル → エントリー → 執行 → 決済 を繋ぐ。

役割分担:
  AIStrategys の各戦略  … いつ入るか（シグナル）を決める。従来どおり仮想売買も続ける
  sizing / account      … いくら買えるか
  PositionManager       … 建玉をどう決済するか（状態機械・判断のみ）
  Executor              … 実際に発注・取消する（安全弁つき）
  AutoTrader（本モジュール）… 上記を繋ぐ

dry_run のとき:
  実際には発注しないため注文番号が返らない。そこで擬似的な注文番号を振り、
  「その価格に相場が届いたら約定した」とみなして状態を進める。
  これにより **実弾を動かさずに1日の流れをログで検証できる**。
"""
import logging
from datetime import datetime, timedelta

import order_builder as ob
import sizing
import tick_size as T
from position_manager import PositionManager, Snapshot, CLOSED


class AutoTrader:
    def __init__(self, config: dict, executor, account_view, log=None,
                 strategy_params: dict = None):
        self.config = config or {}
        self.executor = executor
        self.account = account_view          # AccountView（余力・建玉・銘柄マスタ）
        self.log = log or logging.getLogger("autotrade")
        self.strategy_params = strategy_params or {}
        self.positions = {}                  # symbol -> PositionManager
        self.pending_entries = {}            # order_id -> {"symbol","strategy","qty"}
        self._dry_seq = 0
        self._last_poll = None
        self._last_balance_refresh = None

    # ── 有効判定 ──

    def _active(self, strategy: str) -> bool:
        if not self.config.get("enabled"):
            return False
        return bool((self.config.get("strategies") or {}).get(strategy))

    def any_active(self) -> bool:
        return bool(self.config.get("enabled")) and \
            any((self.config.get("strategies") or {}).values())

    # ── エントリー ──

    def on_signal(self, strategy: str, alert: dict, now: datetime):
        """AIストラテジーの ENTRY シグナルを受けて新規建てする。

        alert は AIStrategys の ENTRY アラート（symbol / price を含む）。
        """
        if alert.get("type") != "ENTRY" or not self._active(strategy):
            return
        symbol = str(alert["symbol"])
        price = alert.get("price")

        if symbol in self.positions and self.positions[symbol].state != CLOSED:
            self.log.info("[自動売買] %s は既に建玉あり。エントリーを見送り", symbol)
            return
        if any(p["symbol"] == symbol for p in self.pending_entries.values()):
            self.log.info("[自動売買] %s は発注中。重複エントリーを見送り", symbol)
            return

        cap = self.config.get("capital", {})
        # 余力は都度取り直す（他の約定で変動しているため）
        try:
            self.account.refresh("2" if cap.get("product") == "cash" else "3")
        except Exception as e:
            self.log.warning("[自動売買] 余力の取得に失敗したためエントリーを見送り: %s", e)
            return

        r, info = self.account.plan_quantity(symbol, price, cap)
        if not r.ok:
            self.log.info("[自動売買] %s エントリー見送り: %s", symbol, r.reason)
            return

        self.log.info("[自動売買] %s %s シグナル → %d株（%s円・上限:%s）",
                      strategy, symbol, r.quantity, f"{r.amount:,.0f}", r.limited_by)

        order = ob.entry_market_buy(symbol, r.quantity,
                                    account_type=cap.get("account_type", 4))
        res = self.executor.place(order, now, is_entry=True, strategy=strategy)

        oid = res.get("OrderId") or (self._dry_id() if res.get("dry_run") else None)
        if oid is None:
            return
        self.pending_entries[oid] = {"symbol": symbol, "strategy": strategy,
                                     "qty": r.quantity, "signal_price": price,
                                     "group": info["price_range_group"],
                                     "time": now}
        if res.get("dry_run"):
            # 成行なのでシグナル価格で約定したとみなして状態機械を起動する
            self._open_position(oid, price, now)

    def _dry_id(self):
        self._dry_seq += 1
        return f"DRY{self._dry_seq:04d}"

    def _open_position(self, entry_order_id, fill_price, now):
        info = self.pending_entries.pop(entry_order_id, None)
        if info is None:
            return
        cap = self.config.get("capital", {})
        pm = PositionManager(
            symbol=info["symbol"], entry_price=float(fill_price), qty=info["qty"],
            config=dict(self.config,
                        _strategy_params=self.strategy_params.get(info["strategy"], {})),
            price_range_group=info["group"], account_type=cap.get("account_type", 4),
            strategy=info["strategy"], log=self.log)
        self.positions[info["symbol"]] = pm
        self.log.info("[自動売買] 建玉成立 %s", pm.describe())

    # ── 板の更新（PUSHごと） ──

    def on_tick(self, symbol: str, price, bid, ask, now: datetime):
        pm = self.positions.get(str(symbol))
        if pm is None or pm.state == CLOSED:
            return
        snap = Snapshot(time=now, price=price, bid=bid, ask=ask)
        try:
            actions = pm.on_tick(snap)
        except Exception:
            self.log.exception("[自動売買] %s 状態機械でエラー", symbol)
            return
        for a in actions:
            self._do(pm, a, now)
        # dry_run では相場が指値に届いたら約定したとみなす
        if self.config.get("dry_run", True):
            self._simulate_fill(pm, snap)

    def _do(self, pm: PositionManager, action: dict, now: datetime):
        if action["type"] == "cancel":
            self.executor.cancel(action["order_id"], action.get("reason", ""))
            pm.on_order_canceled(action["order_id"])
            return
        res = self.executor.place(action["order"], now, is_entry=False,
                                  strategy=pm.strategy)
        oid = res.get("OrderId") or (self._dry_id() if res.get("dry_run") else None)
        if oid:
            pm.on_order_placed(oid)
            self.log.info("[自動売買] %s %s → OrderId=%s",
                          pm.symbol, action.get("intent", ""), oid)

    def _simulate_fill(self, pm: PositionManager, snap: Snapshot):
        """dry_run用の擬似約定。売り指値に買い気配（なければ現在値）が届いたら約定とみなす。"""
        if pm.order_id is None or pm.last_order_price is None:
            return
        ref = snap.bid if snap.bid is not None else snap.price
        if ref is None:
            return
        if float(ref) >= float(pm.last_order_price):
            px = float(pm.last_order_price)
            pm.on_filled(pm.qty, px, snap.time)
            self.log.info("[自動売買/DRY-RUN 約定] %s %d株 @%s → 損益 %+.2f%%",
                          pm.symbol, pm.qty, px, pm.pnl_pct())
            self.executor.open_orders.pop(pm.order_id, None)
            pm.on_order_canceled()

    # ── 定期処理（注文状態の確認）──

    def poll(self, now: datetime, interval_seconds: float = 3.0):
        """実発注時のみ: 注文の約定・終了を確認して状態を進める。"""
        if self.config.get("dry_run", True):
            return
        if self._last_poll and (now - self._last_poll).total_seconds() < interval_seconds:
            return
        self._last_poll = now
        cap = self.config.get("capital", {})
        product = "2" if cap.get("product") == "cash" else "3"

        for st in self.executor.refresh_orders(product):
            oid, filled = st["order_id"], st["filled"]
            if st["state"] != 5 or filled <= 0:
                continue
            if oid in self.pending_entries:
                # 新規建てが約定 → 実際の建値を建玉照会から取る
                info = self.pending_entries[oid]
                px = self._lookup_entry_price(info["symbol"], product) or info["signal_price"]
                self._open_position(oid, px, now)
            else:
                for pm in self.positions.values():
                    if pm.order_id == oid:
                        pm.on_filled(filled, float(pm.last_order_price or 0), now)
                        self.log.warning("[自動売買/約定] %s %s株 → 損益 %+.2f%%",
                                         pm.symbol, filled, pm.pnl_pct() or 0)
                        break

    def _lookup_entry_price(self, symbol, product):
        try:
            for p in self.account.client.get_positions(product) or []:
                if str(p.get("Symbol")) == str(symbol) and float(p.get("LeavesQty") or 0) > 0:
                    return float(p.get("Price"))
        except Exception as e:
            self.log.warning("[自動売買] 建値の取得に失敗 %s: %s", symbol, e)
        return None

    # ── 集計 ──

    def summary(self):
        live = [p for p in self.positions.values() if p.state != CLOSED]
        done = [p for p in self.positions.values() if p.state == CLOSED]
        s = f"[自動売買] 建玉 {len(live)}件 / 決済済 {len(done)}件"
        if done:
            pnls = [p.pnl_pct() for p in done if p.pnl_pct() is not None]
            if pnls:
                s += f" / 損益合計 {sum(pnls):+.2f}%（平均 {sum(pnls)/len(pnls):+.2f}%）"
        return s
