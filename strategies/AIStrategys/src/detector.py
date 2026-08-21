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

3. PanicReboundStrategy（投げ売り反発／セリングクライマックス）
   安値圏でOVERが急減し、ほぼ同数が買い気配にぶつけられた（投げが出切った）局面で
   仮想買いエントリーする。時間帯を問わない点が上2つと異なる。
   検証結果（2026-07-13〜31・銘柄1日1回・コスト控除後）:
     SL-1%/TP+2% で n=12・勝率66.7%・期待値+0.678%・PF2.72
   ただし **n=12と極小で、week1は勝率40%・期待値-0.43%とマイナス**。
   プラスはすべてweek3由来であり、有効性は未確定。フォワード検証が目的。

4. PanicReboundStrategy の「幅広版」（panic_rebound_wide）
   3と同じ検知・同じクラスを、**損切り・利確の幅だけ変えて**もう1つ動かす。
   2026-08-14の5週間分析（analysis/analysis_result_2026-08-14.md）より、
   同一銘柄1日1回に整理した n=20 での比較:

     SL-1.0%/TP+1.0%   勝率80.0%  期待値+0.594%  コスト後+0.444%
     SL-1.5%/TP+3.0%   勝率70.0%  期待値+1.242%  コスト後+1.092%  ← 幅広版
     SL-2.0%/TP+2.0%   勝率75.0%  期待値+0.826%  コスト後+0.676%

   最大益の中央値が+2.02%あるのに対し最大損の中央値は-0.87%。
   伸びる余地に対して利確が早く、損切りが浅すぎるという読み。
   勝率は落ちるが期待値は倍。どちらが実際に効くかを**並走させて比べる**ため、
   既存の panic_rebound は変更せず別ストラテジーとして追加した。

   クラスを分けていないのは、判定ロジックが完全に同一で、違いは設定値だけのため。
   PaperBook はインスタンスごとに独立しているので、2つの建玉が混ざることはない。

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

    take_profit_pct に None を渡すと利確を置かず、損切りに触れない限り
    大引けまで持ち切る。1日かけて進む買い集めのように、上値を先に切ると
    伸びしろを捨ててしまう性質の戦略で使う。
    """

    def __init__(self, stop_loss_pct: float, take_profit_pct=None):
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
            elif (self.take_profit_pct is not None
                  and price >= pos.entry_price * (1 + self.take_profit_pct / 100)):
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


class RankedAfternoonReversalStrategy(AfternoonReversalStrategy):
    """午後の下値大口買い・引け戻り（順位優先版）。

    AfternoonReversalStrategy との違いは**エントリーの取捨選択だけ**で、
    検知条件・決済ルールは同じ。

      銘柄リストの上位 top_rank 位以内 … 従来どおり即エントリー
      それより下の順位            … late_entry_after 以降でないとエントリーしない

    順位は symbols.yaml の並び順（＝extracted_stocks のR/Rスコア順）。
    set_ranks() でランナーから渡す。渡されていない銘柄は下位として扱う。

    【根拠】2026-08-14の5週間分析（analysis/analysis_result_2026-08-14.md）
    午後UNDER急増を順位で分けると、決済ルールを5つ変えても上位が下位を上回った:

      順位      SL2/TP2        大引け         60分後
      1〜25位   90.6%/+0.897%  84.4%/+0.574%  75.0%/+0.684%   (n=32)
      26〜50位  65.9%/+0.464%  65.9%/+0.481%  63.6%/+0.442%   (n=44)

    ただし「上位25位だけ」にすると取引機会が21日→16日に減り、
    累計リターンはむしろ落ちた（+13.29% → +11.42%）。
    そこで**下位は遅い時間帯なら拾う**ことで機会を残す。
    1日1建玉で回した実測（21営業日・コスト0.15%控除後）:

      上位50・その日最初（現行）      21回 勝率76.2% +0.633%/回 累計+13.29% 負け3回(最悪-2.00%)
      上位25のみ                    16回 勝率93.8% +0.714%/回 累計+11.42% 負け1回(最悪-1.18%)
      本戦略（上位25即／下位は14時〜） 18回 勝率94.4% +0.708%/回 累計+12.74% 負け1回(最悪-1.18%)

    需給の読み: R/Rスコア上位は「大幅下落後に空売り機関が買い戻しに入っている」銘柄で、
    売り手が枯れかけたところに下値の大口買いが重なると反発しやすい。
    実際、上位25位は75%が買い戻しあり、26位以下は18%だった。

    ⚠️ 上位25位の標本は n=32 と少なく、week2は2件でマイナス。
    """

    def __init__(self, entry_start: dtime = dtime(13, 0), entry_end: dtime = dtime(15, 0),
                 stop_loss_pct: float = 2.0, take_profit_pct: float = 2.0,
                 min_entry_price: float = 500.0, top_rank: int = 25,
                 late_entry_after: dtime = dtime(14, 0), ranks: dict = None):
        super().__init__(entry_start=entry_start, entry_end=entry_end,
                         stop_loss_pct=stop_loss_pct, take_profit_pct=take_profit_pct,
                         min_entry_price=min_entry_price)
        self.top_rank = int(top_rank)
        self.late_entry_after = late_entry_after
        self.ranks = dict(ranks or {})

    def set_ranks(self, ranks: dict):
        """銘柄コード -> 順位（1始まり）。ランナーが銘柄リストから作って渡す。"""
        self.ranks = {str(k): int(v) for k, v in (ranks or {}).items()}

    def rank_of(self, symbol):
        return self.ranks.get(str(symbol))

    def on_signal(self, source: str, alert: dict, msg_time) -> list:
        rank = self.rank_of(alert.get("symbol"))
        # 順位が分からない銘柄は下位扱い（安全側）
        if rank is None or rank > self.top_rank:
            if msg_time is None or msg_time.time() < self.late_entry_after:
                return []
        out = super().on_signal(source, alert, msg_time)
        for e in out:
            e["rank"] = rank
        return out


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


class PanicReboundStrategy:
    """投げ売り反発戦略（仮想売買）。

    エントリー: 投げ売り検知（panic_sell_detector）のアラートで仮想買い。
                同一銘柄は1日1回まで。既定では stage="DUMP"（買い気配へぶつけ＝投げが
                実際に消化された局面）のみを対象とし、ABSORBED は含めない。
    決済: 損切り-stop_loss_pct% / 利確+take_profit_pct% / 残りは大引け

    他の2戦略との違い:
      午後限定ではなく**終日**が対象。投げ売りは寄り付き直後のパニックでも起こり、
      検証でも前場・後場の双方でプラスだったため時間帯を絞っていない。
      価格フィルタも既定では無効（低位株でも機能したため）。
    """

    def __init__(self, entry_start: dtime = dtime(9, 0), entry_end: dtime = dtime(15, 0),
                 stop_loss_pct: float = 1.0, take_profit_pct: float = 2.0,
                 min_entry_price: float = 0.0, stages: tuple = ("DUMP",)):
        self.entry_start = entry_start
        self.entry_end = entry_end
        self.min_entry_price = min_entry_price
        self.stages = tuple(stages)
        self.book = PaperBook(stop_loss_pct, take_profit_pct)

    def on_price(self, symbol: str, price, msg_time) -> list:
        alert = self.book.check_exit(symbol, price, msg_time)
        return [alert] if alert else []

    def on_signal(self, source: str, alert: dict, msg_time) -> list:
        if source != "panic_sell_detector":
            return []
        if alert.get("stage") not in self.stages:
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
        entry["trigger"] = "投げ売り"
        return [entry]


class AccumulationFollowStrategy:
    """定期買い集め追随戦略（仮想売買）。

    エントリー: 定期買い集め検知（z値方式）が STRONG を点灯させた銘柄を
                仮想買い。既定では**午前のみ**（同一銘柄は1日1回）。
    決済: 損切り -stop_loss_pct% のみ。**利確は置かず**、触れなければ大引け。

    他の戦略と違う点が2つある。
      1. 利確を置かない
      2. 入力が板（PUSH）ではなく歩み値のポーリングから来る

    【根拠】2026-08-21の分析（analysis/output/2026-08-21/）
      07-21〜08-21のアラートをYahoo Financeの5分足で検証した結果:
        大引けまで持ち切り      n=20  勝率65.0%  期待値+0.56%
        損切り-2%のみ           n=20  勝率60.0%  期待値+0.40%
        利確+2%/損切り-2%       n=20  勝率60.0%  期待値+0.25%
      利確を置くと期待値が半分近くまで落ちる。買い集めは1日かけて進むため、
      +2%で切ると伸びしろを捨てることになる、という解釈と整合する。
      時間帯では午前(09-11時)が n=14 期待値+0.52% と強く、
      午後は n=4 期待値-0.19% で機能しなかった。

    【重要・実弾に使ってはいけない理由】
      上記の20件は**わずか8銘柄**から出ており、うち16件が3銘柄
      （4013・6580・4414）に集中している。検出日も8日しかない。
      実質3〜4の独立事例しかなく、統計として成立していない。
      別銘柄で20件以上たまるまでは仮想売買に留めること。
    """

    def __init__(self, entry_start: dtime = dtime(9, 0),
                 entry_end: dtime = dtime(11, 0),
                 stop_loss_pct: float = 2.0, take_profit_pct=None,
                 min_entry_price: float = 500.0, tiers: tuple = ("STRONG",),
                 min_zscore: float = 0.0):
        self.entry_start = entry_start
        self.entry_end = entry_end
        self.min_entry_price = min_entry_price
        self.tiers = tuple(tiers)
        self.min_zscore = min_zscore
        self.book = PaperBook(stop_loss_pct, take_profit_pct)

    def on_price(self, symbol: str, price, msg_time) -> list:
        alert = self.book.check_exit(symbol, price, msg_time)
        return [alert] if alert else []

    def on_signal(self, source: str, alert: dict, msg_time) -> list:
        if source != "periodic_buy_zscore":
            return []
        if alert.get("tier") not in self.tiers:
            return []
        if float(alert.get("zscore") or 0) < self.min_zscore:
            return []
        if not _in_window(msg_time, self.entry_start, self.entry_end):
            return []
        symbol = str(alert["symbol"])
        # この検知は歩み値の統計から出るため価格を持たない。
        # ランナー側が直近のPUSHで観測した現在値を入れてから渡してくる。
        price = alert.get("price")
        if price is None or price < self.min_entry_price:
            return []
        if not self.book.can_enter(symbol, msg_time):
            return []
        entry = self.book.enter(symbol, price, msg_time)
        entry["trigger"] = "定期買い集め"
        return [entry]
