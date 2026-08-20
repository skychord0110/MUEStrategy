"""kabuステーションAPI の歩み値（GET /timeandsales）から約定を取得するデータ源。

楽天マーケットスピードII RSS（Excel経由）の代替。MarketSpeedTickReader と
同じインターフェース（connect / read）を持つので、検知ロジックは無変更で使える。

RSS版に対する利点（2026-08-10に実機で確認）:
  - 取得件数の上限が実質ない（RssTickListは直近300件まで。4889は5,911件返った）
  - 時刻が **ISO8601・日付とタイムゾーン付き**（"2026-08-10T15:30:00+09:00"）。
    RSSは "15:24:25" と日付が無く、前営業日のティックを当日と誤認する問題があった
  - **銘柄登録が不要**（/board や /symbol と異なりウォッチリスト外でも取れる）
  - Excel・マーケットスピードII・COM連携が不要。ランナーと同一プロセスで動く

注意点:
  - **レート制限がある**（短時間に連続で叩くと HTTP 429）。
    ただし毎回「その日の全件」が返るため、間隔を空けても取りこぼしは起きず、
    検知が遅れるだけ。50銘柄を1秒間隔で回せば1周およそ50秒。
  - 時刻は秒単位（小数秒なし）。同一秒に複数の約定が入ることがある
  - 直近2営業日ぶんが返るので、当日ぶんに絞ってから使う
"""
import logging
import time
from datetime import datetime


class KabuTickSource:
    """GET /timeandsales から歩み値を取る。read() は古い順の [(time_iso, volume, price)]。

    MarketSpeedTickReader と同じ使い方ができるよう connect()/read() を用意している。
    """

    def __init__(self, client, symbols=None, exchange: int = 1,
                 today_only: bool = True, log=None):
        self.client = client
        self.symbols = symbols or []
        self.exchange = exchange
        self.today_only = today_only
        self.log = log or logging.getLogger("periodic_buy_rss")
        self._rate_limited = 0
        self._fail_streak = 0
        self._fail_last_log = 0.0

    def connect(self):
        """RSS版とのインターフェース互換のために存在する。kabuでは事前準備は不要。"""
        self.log.info("歩み値の取得元: kabuステーションAPI /timeandsales"
                      "（Excel・マーケットスピードIIは不要）")
        return True

    def read(self, symbol: str) -> list:
        """指定銘柄の歩み値を古い順の [(time_iso, volume, price)] で返す。

        レート制限（429）に当たった場合は空リストを返す（次の周回で取り直す）。
        """
        try:
            d = self.client.get_time_and_sales(symbol, self.exchange)
        except Exception as e:
            # 例外の型名を必ず添える。str(e) が空の例外があり、2026-08-20 14:30 の
            # ログは「歩み値の取得に失敗 5707: 」と原因不明になった。
            msg = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
            if "429" in msg:
                self._rate_limited += 1
                if self._rate_limited <= 3 or self._rate_limited % 50 == 0:
                    self.log.warning(
                        "歩み値の取得がレート制限に当たりました（%d回目）。"
                        "poll_interval_seconds を長くしてください: %s", self._rate_limited, symbol)
                return []
            # kabuステーションが落ちると全銘柄で延々と失敗する。1件ずつ出すと
            # ログが埋まる（2026-08-20の接続断では約4,000行）ので間引く。
            now = time.monotonic()
            self._fail_streak += 1
            if self._fail_streak == 1 or now - self._fail_last_log >= 300:
                self._fail_last_log = now
                self.log.warning("歩み値の取得に失敗 %s: %s（連続%d件目）",
                                 symbol, msg[:100], self._fail_streak)
            return []
        if self._fail_streak:
            self.log.info("歩み値の取得が復旧しました（%d件連続で失敗していました）",
                          self._fail_streak)
            self._fail_streak = 0

        rows = d.get("TradingPrice") or []
        today = datetime.now().astimezone().date().isoformat()
        out = []
        for r in rows:
            t = r.get("Time")
            price = r.get("Price")
            if not t or price is None:
                continue
            if self.today_only and str(t)[:10] != today:
                continue
            out.append((str(t), r.get("Volume"), price))
        out.sort(key=lambda x: x[0])   # 念のため古い順に整える
        return out
