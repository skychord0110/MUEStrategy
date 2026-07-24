"""AIストラテジー: 既存検知器のアラートを入力にした仮想売買（ペーパートレード）戦略。

2026-07-13〜17のrunnerログ分析（analysis/ 参照）に基づく2ストラテジー:

1. AfternoonReversalStrategy（午後の下値大口買い検知・引け戻り）
   13:00以降にUNDER急増が点灯した銘柄を、その日最初の1回だけ仮想買いエントリーし、
   損切り/利確/大引けで仮想決済する。
   検証結果: 23トレード・勝率69.6%・期待値+0.63%/回（SL-2%/TP+2%・コスト1ティック控除後）

2. ConfluenceStrategy（複合シグナル）
   午後に30分以内で「UNDER急増」と「小口売り連続」の両方が同一銘柄に点灯したら
   仮想買いエントリーする。
   検証結果: 勝率80%・期待値+0.65%/回（SL-1%/TP+1%・ただしn=5と極小）

どちらも**発注は一切行わない**。仮想エントリー/決済をログに残し、
フォワード検証（実際の勝率・期待値の確認）に使う。

runnerとの接続:
  - on_signal(): 基礎ストラテジーの検知アラートを受け取り、エントリー判定する
  - on_price(): PUSHの現在値更新を受け取り、保有中の仮想建玉の決済判定をする
  どちらも発火したアラート（type: ENTRY / EXIT）のリストを返す。
"""
from dataclasses import dataclass
from datetime import time as dtime

# 東証の大引け時刻（クロージング・オークション）
CLOSE_TIME = dtime(15, 30)


@dataclass
class VirtualPosition:
    symbol: str
    entry_price: float
    entry_time: object   # datetime
    entry_date: object   # date
    last_price: float    # 当日中に観測した最後の現在値（大引け補完決済用）


class PaperBook:
    """仮想建玉の管理と決済判定（同一銘柄は1日1回までエントリー可）。

    決済ルール（優先順）:
      1. 損切り: 現在値がエントリー価格の -stop_loss_pct% 以下
      2. 利確:   現在値がエントリー価格の +take_profit_pct% 以上
      3. 大引け: 15:30以降の現在値更新（クロージング・オークションの約定）
      4. 補完:   当日中に大引けのPUSHが来なかった場合、翌営業日以降の最初の
                 メッセージ時に「当日最後に観測した現在値」で決済扱いにする
    """

    def __init__(self, stop_loss_pct: float, take_profit_pct: float):
        self.stop_loss_pct = stop_loss_pct
        self.take_profit_pct = take_profit_pct
        self.positions = {}       # symbol -> VirtualPosition
        self.entered_today = set()  # (symbol, date)

    def can_enter(self, symbol: str, msg_time) -> bool:
        return (symbol not in self.positions
                and (symbol, msg_time.date()) not in self.entered_today)

    def enter(self, symbol: str, price: float, msg_time) -> dict:
        self.positions[symbol] = VirtualPosition(
            symbol=symbol, entry_price=price, entry_time=msg_time,
            entry_date=msg_time.date(), last_price=price,
        )
        self.entered_today.add((symbol, msg_time.date()))
        return {
            "type": "ENTRY",
            "symbol": symbol,
            "price": price,
            "time": msg_time,
            "stop_loss_pct": self.stop_loss_pct,
            "take_profit_pct": self.take_profit_pct,
        }

    def check_exit(self, symbol: str, price, msg_time):
        """現在値更新1件に対する決済判定。決済したらEXITアラートを返す。"""
        pos = self.positions.get(symbol)
        if pos is None or msg_time is None:
            return None

        reason = None
        exit_price = None
        if msg_time.date() > pos.entry_date:
            # 当日中に大引けPUSHが来なかった銘柄: 当日最後の観測値で補完決済
            reason = "大引け(補完)"
            exit_price = pos.last_price
        elif price is not None:
            pos.last_price = price
            if price <= pos.entry_price * (1 - self.stop_loss_pct / 100):
                reason = "損切り"
            elif price >= pos.entry_price * (1 + self.take_profit_pct / 100):
                reason = "利確"
            elif msg_time.time() >= CLOSE_TIME:
                reason = "大引け"
            exit_price = price

        if reason is None:
            return None
        del self.positions[symbol]
        return {
            "type": "EXIT",
            "symbol": symbol,
            "reason": reason,
            "entry_price": pos.entry_price,
            "exit_price": exit_price,
            "return_pct": (exit_price - pos.entry_price) / pos.entry_price * 100,
            "time": msg_time,
        }


def _in_window(msg_time, start: dtime, end: dtime) -> bool:
    if msg_time is None:
        return False
    return start <= msg_time.time() <= end


class AfternoonReversalStrategy:
    """午後の下値大口買い検知・引け戻り戦略（仮想売買）。

    エントリー: entry_start〜entry_end のUNDER急増アラートで、現在値が
                min_entry_price円以上の銘柄（同一銘柄は1日1回）
    決済: 損切り-stop_loss_pct% / 利確+take_profit_pct% / 残りは大引け

    min_entry_price（価格下限フィルタ）について:
      2026-07-13〜24の分析で、500円未満の低位株は勝率50%とノイズが大きく、
      500円以上に限定すると勝率が約71%→81%へ改善した（両週ともロバスト）。
      既定500円。0にすればフィルタなし（旧挙動）。
    """

    def __init__(self, entry_start: dtime = dtime(13, 0), entry_end: dtime = dtime(15, 0),
                 stop_loss_pct: float = 2.0, take_profit_pct: float = 2.0,
                 min_entry_price: float = 500.0):
        self.entry_start = entry_start
        self.entry_end = entry_end
        self.min_entry_price = min_entry_price
        self.book = PaperBook(stop_loss_pct, take_profit_pct)

    def on_price(self, symbol: str, price, msg_time) -> list:
        alert = self.book.check_exit(symbol, price, msg_time)
        return [alert] if alert else []

    def on_signal(self, source: str, alert: dict, msg_time) -> list:
        if source != "under_surge_detector":
            return []
        if not _in_window(msg_time, self.entry_start, self.entry_end):
            return []
        symbol = alert["symbol"]
        price = alert.get("price")
        if price is None or price < self.min_entry_price:
            return []
        if not self.book.can_enter(symbol, msg_time):
            return []
        entry = self.book.enter(symbol, price, msg_time)
        entry["trigger"] = "UNDER急増"
        return [entry]


class ConfluenceStrategy:
    """複合シグナル戦略（仮想売買）。

    エントリー: entry_start〜entry_end に、window_seconds以内で「UNDER急増」と
    「小口売り連続」の**両方**が同一銘柄に点灯（同一銘柄は1日1回）。
    分析（2026-07-13〜17）で有効だったのはこの2種の組み合わせのため、
    投げ売り検知は判定に使わない。
    決済: 損切り-stop_loss_pct% / 利確+take_profit_pct% / 残りは大引け
    """

    REQUIRED_SOURCES = ("under_surge_detector", "small_lot_sell_detector")

    def __init__(self, window_seconds: float = 1800,
                 entry_start: dtime = dtime(13, 0), entry_end: dtime = dtime(15, 0),
                 stop_loss_pct: float = 1.0, take_profit_pct: float = 1.0):
        self.window_seconds = window_seconds
        self.entry_start = entry_start
        self.entry_end = entry_end
        self.book = PaperBook(stop_loss_pct, take_profit_pct)
        self.recent = {}  # (symbol, date) -> {source: 最終点灯時刻}

    def on_price(self, symbol: str, price, msg_time) -> list:
        alert = self.book.check_exit(symbol, price, msg_time)
        return [alert] if alert else []

    def on_signal(self, source: str, alert: dict, msg_time) -> list:
        if source not in self.REQUIRED_SOURCES or msg_time is None:
            return []
        symbol = alert["symbol"]
        rec = self.recent.setdefault((symbol, msg_time.date()), {})
        rec[source] = msg_time

        # window_seconds以内にUNDER急増と小口売り連続の両方が点灯していること
        # （点灯記録は時間帯を問わず蓄積し、エントリー判定のみ午後の時間窓で行う）
        active = [s for s in self.REQUIRED_SOURCES
                  if s in rec and (msg_time - rec[s]).total_seconds() <= self.window_seconds]
        if len(active) < len(self.REQUIRED_SOURCES):
            return []
        if not _in_window(msg_time, self.entry_start, self.entry_end):
            return []
        # 小口売り連続のアラートは現在値の代わりに買い気配(buy_price)を持つ
        price = alert.get("price") or alert.get("buy_price")
        if price is None or not self.book.can_enter(symbol, msg_time):
            return []
        entry = self.book.enter(symbol, price, msg_time)
        entry["trigger"] = "+".join(sorted(active))
        return [entry]
