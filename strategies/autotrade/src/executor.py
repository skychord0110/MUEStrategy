"""注文の送信口。発注・取消・約定確認を担う。

安全設計（多重の歯止め）— すべてを通過しないと実際の注文は出ない:
  1. config の enabled が false → 何もしない
  2. その戦略が strategies で false → 何もしない
  3. safety の上限（1日の発注数・同一銘柄の回数・連射防止）
  4. config の dry_run が true → ログに出すだけで送信しない

⚠️ dry_run を false にすると **実際に発注される**（ステップ3で実装済み）。
   実弾を動かす前に必ず dry_run のログで注文内容を確認すること。
"""
import logging
import time
from datetime import datetime, timedelta

import order_builder as ob


def _parse_time(s):
    """/orders の RecvTime（ISO8601）を datetime にする。読めなければ None。"""
    if not s:
        return None
    try:
        t = datetime.fromisoformat(str(s))
    except ValueError:
        return None
    return t if t.tzinfo else t.astimezone()

# 注文状態（GET /orders の State）
STATE_WAITING = 1      # 待機
STATE_PROCESSING = 2   # 処理中
STATE_PROCESSED = 3    # 処理済（発注済・訂正済）
STATE_CANCELING = 4    # 訂正取消送信中
STATE_DONE = 5         # 終了（発注エラー・取消済・全約定・失効・期限切れ）

ACTIVE_STATES = (STATE_WAITING, STATE_PROCESSING, STATE_PROCESSED, STATE_CANCELING)


class OrderBlocked(Exception):
    """安全弁により発注が止められた。"""


class Executor:
    def __init__(self, config: dict, client=None, log=None):
        self.config = config or {}
        self.client = client            # 実送信用。ステップ2では使わない
        self.log = log or logging.getLogger("autotrade")
        self.safety = self.config.get("safety", {})
        self._orders_today = 0
        self._entries_by_symbol = {}    # (symbol, date) -> 件数
        self._last_order_time = None
        self._day = None
        self.sent = []                  # 発注（dry_run含む）の記録
        self.open_orders = {}           # order_id -> {"order":.., "time":.., "strategy":..}

    # ── 安全弁 ──

    def _roll_day(self, now: datetime):
        d = now.date()
        if self._day != d:
            self._day = d
            self._orders_today = 0
            self._entries_by_symbol = {}
            self._last_order_time = None

    def _check_safety(self, order: dict, now: datetime, is_entry: bool):
        self._roll_day(now)
        max_orders = int(self.safety.get("max_orders_per_day", 20))
        if self._orders_today >= max_orders:
            raise OrderBlocked(f"1日の発注上限に達している（{self._orders_today}/{max_orders}件）")

        if is_entry:
            key = (order["Symbol"], now.date())
            n = self._entries_by_symbol.get(key, 0)
            max_e = int(self.safety.get("max_entries_per_symbol_per_day", 1))
            if n >= max_e:
                raise OrderBlocked(
                    f"{order['Symbol']} は本日のエントリー上限に達している（{n}/{max_e}件）")

        min_iv = float(self.safety.get("min_order_interval_seconds", 3))
        if self._last_order_time is not None:
            elapsed = (now - self._last_order_time).total_seconds()
            if elapsed < min_iv:
                raise OrderBlocked(f"発注間隔が短すぎる（{elapsed:.1f}秒 < {min_iv}秒）")

    # ── 送信口 ──

    def place(self, order: dict, now: datetime = None, is_entry: bool = False,
              strategy: str = ""):
        """注文を送る（dry_runならログのみ）。

        戻り値: 送信結果の dict。dry_run のときは {"dry_run": True, ...}
        """
        now = now or datetime.now().astimezone()
        desc = ob.describe(order)
        tag = f"[{strategy}] " if strategy else ""

        if not self.config.get("enabled"):
            self.log.info("%s[発注せず] enabled=false のためスキップ: %s", tag, desc)
            return {"skipped": "disabled"}

        strategies = self.config.get("strategies") or {}
        if strategy and not strategies.get(strategy):
            self.log.info("%s[発注せず] この戦略は実売買OFF: %s", tag, desc)
            return {"skipped": "strategy_disabled"}

        try:
            self._check_safety(order, now, is_entry)
        except OrderBlocked as e:
            self.log.warning("%s[発注ブロック] %s ← %s", tag, e, desc)
            return {"blocked": str(e)}

        payload = ob.to_payload(order)

        if self.config.get("dry_run", True):
            self.log.info("%s[DRY-RUN 発注] %s", tag, desc)
            self.log.info("%s          送信予定のボディ: %s", tag, payload)
            self._record(order, now, is_entry)
            rec = {"time": now, "order": order, "dry_run": True, "order_id": None}
            self.sent.append(rec)
            return {"dry_run": True, "payload": payload}

        # ── ここから実送信 ──
        self.log.warning("%s[実発注] %s", tag, desc)
        self.log.info("%s        ボディ: %s", tag, payload)
        sent_at = datetime.now().astimezone()
        try:
            result = self._send_real(payload)
        except Exception as e:
            # タイムアウト・500 などは「送れなかった」とは限らない。
            # kabuステーション側で受け付け済みかもしれないので必ず突き合わせる。
            self.log.exception("%s[実発注 失敗] %s ← %s", tag, e, desc)
            found = self.reconcile(order, sent_at, tag)
            if found is not None:
                self._record(order, now, is_entry)
                oid = str(found.get("ID"))
                self.sent.append({"time": now, "order": order, "result": found,
                                  "order_id": oid, "reconciled": True})
                self.open_orders[oid] = {"order": order, "time": now,
                                         "strategy": strategy}
                return {"Result": 0, "OrderId": oid, "reconciled": True}
            return {"error": str(e)}

        # Result=0 が成功。それ以外はエラーコード。
        code = result.get("Result")
        order_id = result.get("OrderId")
        if code == 0:
            self.log.warning("%s[実発注 受付] OrderId=%s  %s", tag, order_id, desc)
            self._record(order, now, is_entry)
            self.sent.append({"time": now, "order": order, "result": result,
                              "order_id": order_id})
            self.open_orders[order_id] = {"order": order, "time": now,
                                          "strategy": strategy}
        else:
            self.log.error("%s[実発注 エラー] Result=%s %s ← %s", tag, code, result, desc)
        return result

    def _record(self, order, now, is_entry):
        self._orders_today += 1
        self._last_order_time = now
        if is_entry:
            key = (order["Symbol"], now.date())
            self._entries_by_symbol[key] = self._entries_by_symbol.get(key, 0) + 1

    def _send_real(self, payload: dict):
        """実際に /sendorder を呼ぶ。⚠️ ここで注文が発注される。"""
        if self.client is None:
            raise RuntimeError("kabuクライアントが渡されていないため発注できません")
        return self.client.send_order(payload)

    # ── 発注失敗後の突き合わせ ──

    def reconcile(self, order: dict, sent_at: datetime, tag: str = ""):
        """送信が失敗扱いになった注文が、実は受け付けられていないか確認する。

        【なぜ必要か】
        HTTPのタイムアウトや500は「注文が出ていない」ことを意味しない。
        リクエストは届いていて、応答だけが返らなかった可能性がある。
        失敗と決めつけて何も記録しないと、実際には生きている注文を
        誰も管理しないまま放置してしまう（約定しても損切りも利確もかからない）。

        実測例（2026-08-14 13:30）: /sendorder が13.6秒かけて500を返した。
        このときは注文が出ていなかったが、出ていた場合に気づけない作りだった。

        見つかれば /orders の該当行を返す。見つからなければ None。
        """
        if self.client is None:
            return None
        symbol = str(order.get("Symbol"))
        side = str(order.get("Side"))
        qty = float(order.get("Qty") or 0)
        product = "2" if int(order.get("CashMargin", 1)) == 1 else "3"
        # 受付から /orders に載るまで少し間があるので数回試す
        for attempt, wait in enumerate((0.0, 1.0, 2.0), start=1):
            if wait:
                time.sleep(wait)
            try:
                rows = self.client.get_orders(product=product, symbol=symbol) or []
            except Exception as e:
                self.log.warning("%s[突き合わせ] 注文照会に失敗（%d回目）: %s", tag, attempt, e)
                continue
            for o in rows:
                if str(o.get("Symbol")) != symbol or str(o.get("Side")) != side:
                    continue
                if float(o.get("OrderQty") or 0) != qty:
                    continue
                recv = _parse_time(o.get("RecvTime"))
                if recv is None or recv < sent_at - timedelta(seconds=30):
                    continue          # 送信より前の注文＝別物
                if str(o.get("ID")) in self.open_orders:
                    continue          # 既に管理下にある
                self.log.error(
                    "%s[突き合わせ] ★送信は失敗扱いだったが、注文は受け付けられていた★ "
                    "OrderId=%s 状態=%s 約定%s/%s株 → 管理下に置きます",
                    tag, o.get("ID"), o.get("State"), o.get("CumQty"), o.get("OrderQty"))
                return o
            self.log.info("%s[突き合わせ] 該当する注文は見つからず（%d回目）", tag, attempt)
        self.log.warning("%s[突き合わせ] 注文は出ていないと判断しました（%s %s株）",
                         tag, symbol, qty)
        return None

    # ── 取消 ──

    def cancel(self, order_id: str, reason: str = ""):
        """注文を取り消す。dry_run ならログのみ。

        損切りの指し直し（気配を追いかける）や、15:26の強制手仕舞いへの
        切り替え時に、出しっぱなしの注文を消すために使う。
        """
        if not order_id:
            return {"skipped": "no_order_id"}
        tag = f"[{reason}] " if reason else ""
        if self.config.get("dry_run", True):
            self.log.info("%s[DRY-RUN 取消] OrderId=%s", tag, order_id)
            self.open_orders.pop(order_id, None)
            return {"dry_run": True, "order_id": order_id}
        self.log.warning("%s[実取消] OrderId=%s", tag, order_id)
        try:
            r = self.client.cancel_order(order_id)
        except Exception as e:
            self.log.exception("%s[取消 失敗] OrderId=%s ← %s", tag, order_id, e)
            return {"error": str(e)}
        if r.get("Result") == 0:
            self.open_orders.pop(order_id, None)
            self.log.warning("%s[取消 受付] OrderId=%s", tag, order_id)
        else:
            self.log.error("%s[取消 エラー] %s", tag, r)
        return r

    def cancel_all(self, reason: str = "一括取消"):
        """未約定の注文をすべて取り消す（緊急停止・引け際の切り替え用）。"""
        results = []
        for oid in list(self.open_orders):
            results.append(self.cancel(oid, reason))
        return results

    # ── 約定・注文状態の確認 ──

    def refresh_orders(self, product: str = None):
        """未約定として管理している注文の状態をAPIで確認し、終了したものを外す。

        戻り値: [{"order_id":.., "state":.., "filled":.., "qty":..}, ...]
        """
        if self.config.get("dry_run", True) or self.client is None:
            return []
        out = []
        for oid in list(self.open_orders):
            try:
                rows = self.client.get_orders(product=product, order_id=oid)
            except Exception as e:
                self.log.warning("注文照会に失敗 OrderId=%s: %s", oid, e)
                continue
            if not rows:
                continue
            o = rows[0]
            state = o.get("State")
            filled = float(o.get("CumQty") or 0)
            qty = float(o.get("OrderQty") or 0)
            out.append({"order_id": oid, "state": state, "filled": filled, "qty": qty})
            if state == STATE_DONE:
                self.log.info("[注文終了] OrderId=%s 約定%s/%s株", oid, filled, qty)
                self.open_orders.pop(oid, None)
        return out

    def has_open_orders(self) -> bool:
        return bool(self.open_orders)

    # ── 集計 ──

    def summary(self):
        n = len(self.sent)
        mode = "DRY-RUN" if self.config.get("dry_run", True) else "実発注"
        if not n:
            return f"本日の発注（{mode}）: なし"
        buys = sum(1 for s in self.sent if s["order"]["Side"] == ob.SIDE_BUY)
        return (f"本日の発注（{mode}）: {n}件（買{buys} / 売{n - buys}）"
                f" / 1日上限 {self.safety.get('max_orders_per_day', 20)}件"
                f" / 未約定 {len(self.open_orders)}件")
