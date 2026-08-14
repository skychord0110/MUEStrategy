"""自動売買の処理を、PUSH受信スレッドから切り離して専用スレッドで動かす。

【なぜ必要か】
発注は `requests.post` の同期呼び出しで、応答が返るまでそのスレッドが止まる。
これを WebSocket の on_message から直接呼ぶと、**発注のあいだ全銘柄のPUSHが
1件も処理されない**。検知も、建玉の損切り・利確判定も、その間すべて止まる。

実測（2026-08-14 13:30）:
    13:30:24.955  /sendorder 送信
    13:30:38.524  13.6秒かけて 500 が返る
    13:30:41.903  止まっていたPUSH処理がようやく再開（17秒遅れ）
    13:30:43.003  kabuステーションがWebSocketを強制切断（WinError 10054）
受信を止めたままにしたことが切断の一因と考えられる。

そこで PUSH受信スレッドは「置くだけ」にして、実際のAPI呼び出しは本スレッドで行う。

板の更新は**銘柄ごとに最新の1件だけ**を残す（溜め込んでも古い板で判断する意味がなく、
処理が遅れるほど無駄が増えるため）。一方 **エントリーシグナルは絶対に捨てない**。
"""
import logging
import threading
from datetime import datetime

MAX_PENDING_SIGNALS = 100


class AutoTradePump:
    def __init__(self, autotrader, log=None, poll_interval: float = 1.0):
        self.autotrader = autotrader
        self.log = log or logging.getLogger("autotrade")
        self.poll_interval = float(poll_interval)
        self._ticks = {}          # symbol -> (price, bid, ask, now) 最新のみ
        self._signals = []        # [(strategy, alert, now)] 取りこぼさない
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stopping = threading.Event()
        self._thread = None
        self.dropped_signals = 0

    # ── PUSH受信スレッドから呼ばれる（ここではAPIを叩かない）──

    def submit_tick(self, symbol, price, bid, ask, now):
        with self._lock:
            self._ticks[str(symbol)] = (price, bid, ask, now)
        self._wake.set()

    def submit_signal(self, strategy, alert, now):
        with self._lock:
            if len(self._signals) >= MAX_PENDING_SIGNALS:
                self.dropped_signals += 1
                self.log.error("[自動売買] 処理待ちのシグナルが%d件を超えたため捨てました"
                               "（累計%d件）。発注処理が滞っています",
                               MAX_PENDING_SIGNALS, self.dropped_signals)
                return
            self._signals.append((strategy, alert, now))
        self._wake.set()

    # ── 専用スレッド ──

    def start(self):
        if self._thread is not None:
            return self._thread
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="autotrade")
        self._thread.start()
        self.log.info("自動売買の処理スレッドを開始しました"
                      "（PUSH受信は発注で待たされません）")
        return self._thread

    def stop(self, timeout: float = 5.0):
        self._stopping.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def _loop(self):
        while not self._stopping.is_set():
            # 板が来なくても poll は回す（引け際の未約定取消・注文状態の確認のため）
            self._wake.wait(self.poll_interval)
            self._wake.clear()
            self.run_once()

    def run_once(self):
        """溜まったぶんを1回処理する（テストから直接呼べるよう分けてある）。"""
        with self._lock:
            ticks, self._ticks = self._ticks, {}
            signals, self._signals = self._signals, []

        for sym, (price, bid, ask, now) in ticks.items():
            try:
                self.autotrader.on_tick(sym, price, bid, ask, now)
            except Exception:
                self.log.exception("[自動売買] %s の板処理でエラー", sym)

        for strategy, alert, now in signals:
            try:
                self.autotrader.on_signal(strategy, alert, now)
            except Exception:
                self.log.exception("[自動売買] %s のシグナル処理でエラー", strategy)

        try:
            self.autotrader.poll(datetime.now().astimezone())
        except Exception:
            self.log.exception("[自動売買] 注文状態の確認でエラー")

    def pending(self):
        with self._lock:
            return len(self._ticks), len(self._signals)
