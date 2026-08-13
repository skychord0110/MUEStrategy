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
from position_manager import PositionManager, Snapshot, CLOSED, _hhmm


class EntryOrderTypeError(Exception):
    """entry.order_type の指定が不正。意図しない成行を出さないため発注を止める。"""


class AutoTrader:
    def __init__(self, config: dict, executor, account_view, log=None,
                 strategy_params: dict = None):
        self.config = config or {}
        self.executor = executor
        self.account = account_view          # AccountView（余力・建玉・銘柄マスタ）
        self.log = log or logging.getLogger("autotrade")
        self.strategy_params = strategy_params or {}
        self.positions = {}                  # symbol -> PositionManager
        # order_id -> {"symbol","strategy","qty","signal_price","limit_price","group","time"}
        # limit_price が None なら成行。指値は約定するまでここに残り続ける。
        self.pending_entries = {}
        # symbol -> {"price","bid","ask","time"}: 直近の板。
        # on_signal は板を受け取らないため、直前の on_tick で控えたものを使う
        # （main.py は同じPUSHについて on_tick → on_signal の順で呼ぶ）。
        self.boards = {}
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

    def _entry_limit_price(self, signal_price, group):
        """エントリーの指値価格を決める。成行のときは None を返す。

        order_type:
          "market"       … 成行
          "limit_signal" … シグナル価格（検知時の現在値）に指値

        指値は「この価格より上では買わない」という上限として働くので、
        呼値に乗らない端数は必ず切り下げる（不利側に丸めない）。
        """
        cfg = self.config.get("entry") or {}
        kind = str(cfg.get("order_type", "market")).strip()
        if kind == "market":
            return None
        if kind != "limit_signal":
            # 綴り違いを黙って成行にすると意図しない価格で約定するため、発注しない
            raise EntryOrderTypeError(
                f'entry.order_type の指定が不正です: "{kind}"'
                '（"market" または "limit_signal"）')
        if signal_price is None:
            raise EntryOrderTypeError("シグナル価格が取得できないため指値を決められません")
        px = T.round_to_tick(signal_price, group, mode="down")
        offset = int(cfg.get("limit_offset_ticks", 0) or 0)
        if offset:
            px = T.shift_ticks(px, offset, group)
        return px

    def _resting_entry(self):
        """未約定のまま残っているエントリー注文（先頭の1件）。無ければ None。"""
        for oid, info in self.pending_entries.items():
            return oid, info
        return None

    def _can_hit_ask(self, symbol: str, limit_price) -> bool:
        """その指値が、いまの売り板にぶつけられる（＝すぐ約定する）か。"""
        if limit_price is None:
            return True                       # 成行は常に約定するとみなす
        board = self.boards.get(str(symbol)) or {}
        ask = board.get("ask")
        if ask is None or not ask:
            return False
        return float(limit_price) >= float(ask)

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
        group = (self.account.master.get(symbol) or {}).get(
            "price_range_group", T.DEFAULT_GROUP)
        try:
            limit_price = self._entry_limit_price(price, group)
        except (EntryOrderTypeError, T.UnknownPriceRangeGroup) as e:
            self.log.error("[自動売買] %s エントリー中止: %s", symbol, e)
            return

        # 未約定の指値を残したまま別銘柄のシグナルが出たときの扱い。
        # 資金は1銘柄ぶんしかないため、両方に出すことはできない。
        #   ・新しい銘柄がすぐ約定する（売り板にぶつけられる）なら、そちらに乗り換える
        #   ・すぐ約定しないなら、いま出している指値をそのまま残して見送る
        resting = self._resting_entry()
        if resting is not None:
            oid, info = resting
            if info.get("limit_price") is None:
                self.log.info("[自動売買] %s は成行注文が処理中のため見送り", symbol)
                return
            if not self._can_hit_ask(symbol, limit_price):
                board = self.boards.get(symbol) or {}
                self.log.info(
                    "[自動売買] %s は即約定できない（指値%s / 売り気配%s）ため見送り。"
                    "%s の指値%sはそのまま残します",
                    symbol, limit_price, board.get("ask") or "—",
                    info["symbol"], info.get("limit_price"))
                return
            self.log.warning(
                "[自動売買] %s の指値%s（未約定）を取り消し、すぐ約定できる %s に乗り換えます",
                info["symbol"], info.get("limit_price"), symbol)
            self.executor.cancel(oid, "指値の乗り換え")
            self.pending_entries.pop(oid, None)

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

        board = self.boards.get(symbol) or {}
        if limit_price is None:
            order = ob.entry_market_buy(symbol, r.quantity,
                                        account_type=cap.get("account_type", 4))
            how = "成行"
        else:
            order = ob.entry_limit_buy(symbol, r.quantity, float(limit_price),
                                       account_type=cap.get("account_type", 4))
            how = (f"指値{limit_price}円"
                   f"（売り気配{board.get('ask') or '—'} / "
                   f"{'即約定の見込み' if self._can_hit_ask(symbol, limit_price) else '約定待ち'}）")
        self.log.info("[自動売買] %s %s シグナル → %d株 %s（%s円・上限:%s）",
                      strategy, symbol, r.quantity, how,
                      f"{r.amount:,.0f}", r.limited_by)

        res = self.executor.place(order, now, is_entry=True, strategy=strategy)

        oid = res.get("OrderId") or (self._dry_id() if res.get("dry_run") else None)
        if oid is None:
            return
        self.pending_entries[oid] = {"symbol": symbol, "strategy": strategy,
                                     "qty": r.quantity, "signal_price": price,
                                     "limit_price": float(limit_price) if limit_price is not None else None,
                                     "group": info["price_range_group"],
                                     "time": now}
        if res.get("dry_run") and limit_price is None:
            # 成行なのでシグナル価格で約定したとみなして状態機械を起動する。
            # 指値のときは on_tick で「売り気配が指値以下になったら約定」とみなす。
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
        sym = str(symbol)
        # 板は建玉の有無に関わらず控える（on_signal が売り気配を見るため）
        self.boards[sym] = {"price": price, "bid": bid, "ask": ask, "time": now}
        snap = Snapshot(time=now, price=price, bid=bid, ask=ask)

        if self.config.get("dry_run", True) and self._simulate_entry_fill(sym, snap):
            # 建玉ができた直後の同じメッセージで決済判定をしない（実発注時と揃える）
            return

        pm = self.positions.get(sym)
        if pm is None or pm.state == CLOSED:
            return
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

    def _simulate_entry_fill(self, symbol: str, snap: Snapshot) -> bool:
        """dry_run用: 買い指値に売り気配が届いたら約定したとみなす。

        売り気配が指値以下ということは、その値段で売ってくれる相手がいるということ。
        実際にはその売り気配で約定するので、約定価格は売り気配を使う。
        """
        if snap.ask is None or not snap.ask:
            return False
        for oid, info in list(self.pending_entries.items()):
            if info["symbol"] != symbol or info.get("limit_price") is None:
                continue
            if float(snap.ask) <= float(info["limit_price"]):
                self.log.info("[自動売買/DRY-RUN 約定] %s %d株 @%s（指値%s）",
                              symbol, info["qty"], snap.ask, info["limit_price"])
                self.executor.open_orders.pop(oid, None)
                self._open_position(oid, float(snap.ask), snap.time)
                return True
        return False

    def _cancel_stale_entries(self, now: datetime):
        """引け際までに約定しなかったエントリー指値を取り消す。

        指値を出しっぱなしにすると、15:25〜15:30のクロージング・オークションで
        約定して**決済されないまま翌日に持ち越す**恐れがある
        （日中で完結させる戦略のため、建玉を持ち越す想定になっていない）。
        entry.cancel_unfilled_at を null にすればこの取消は行わない。
        """
        at = (self.config.get("entry") or {}).get("cancel_unfilled_at")
        if not at:
            return
        try:
            cutoff = _hhmm(at)
        except (ValueError, TypeError):
            self.log.error("[自動売買] entry.cancel_unfilled_at の書式が不正: %r", at)
            return
        if now.time() < cutoff:
            return
        for oid, info in list(self.pending_entries.items()):
            if info.get("limit_price") is None:
                continue
            self.log.warning("[自動売買] %s の指値%s円が%sまでに約定しなかったため取り消します",
                             info["symbol"], info["limit_price"], at)
            self.executor.cancel(oid, "未約定エントリーの取消")
            self.pending_entries.pop(oid, None)

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
        """注文の約定・終了を確認して状態を進める。"""
        # 引け際の取消は dry_run でも動かす（ログで挙動を確認できるように）
        self._cancel_stale_entries(now)
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
