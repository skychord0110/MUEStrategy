"""「約定のちょうどN秒後に買い」が統計的に突出しているかで買い集めを検知する。

API非依存の純ロジック。1日ぶんの歩み値を渡すと分析結果を返す。

なぜz値なのか（2026-08-10の実データ検証より）:
  生の「10秒差ペア数」を数えるだけだと、流動性が高いほど数が増えるだけの指標になる。

    銘柄     約定数   10秒ペア
    4889     5,240    19,913   ← 数は多いが、9秒(20,469)や11秒(20,280)と変わらない
    4013       192       121   ← 数は少ないが、他のラグ(0〜25)と比べて桁違い

  そこで「ラグ6〜14秒それぞれのペア数」を並べ、**10秒だけが突出しているか**を
  z値で測る。これなら流動性で正規化され、銘柄をまたいで同じ基準で比較できる。

    4013(勤次郎) z=+15.10  ★  他の5銘柄は -1.36 〜 +1.56
    → しきい値 z>=5 なら、実データ上は誤検知ゼロで勤次郎だけを拾えた

  勤次郎の中身も買い集めアルゴと整合していた:
    10秒後のロットは112/121件が100株（判で押したように同じ）
    価格は 上昇86 / 同値22 / 下落13 で7割が買い上がり
    9:30時点で既に z=35.5（寄り付き30分で判定できる）
"""
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import timedelta


@dataclass
class Analysis:
    """1銘柄・1日ぶんの分析結果。"""
    symbol: str
    trades: int = 0
    lag_counts: dict = field(default_factory=dict)   # ラグ秒 -> ペア数
    target_pairs: int = 0                            # 目標ラグ(既定10秒)のペア数
    zscore: float = 0.0
    dominant_lot: float = None                       # 10秒後の約定で最も多いロット
    dominant_lot_ratio: float = 0.0
    up: int = 0                                      # 10秒後が上昇（買い上がり）
    flat: int = 0
    down: int = 0
    ok: bool = False                                 # 判定に足るデータがあったか
    reason: str = ""

    @property
    def buy_ratio(self) -> float:
        n = self.up + self.flat + self.down
        return (self.up + self.flat) / n if n else 0.0


def analyze(symbol, trades, delay_seconds=10, lag_min=6, lag_max=14,
            min_trades=50, min_pairs=20) -> Analysis:
    """trades: [(datetime, volume, price), ...]（当日ぶん・時刻順）を分析する。

    歩み値APIは毎回その日の全件を返すため、差分管理は不要で毎回この関数を呼べばよい。
    """
    a = Analysis(symbol=str(symbol), trades=len(trades))
    if len(trades) < min_trades:
        a.reason = f"約定が少なすぎる（{len(trades)}件 < {min_trades}件）"
        return a

    # 時刻 -> その秒に起きた約定のリスト
    idx = defaultdict(list)
    for t, v, p in trades:
        idx[t].append((v, p))

    lags = [L for L in range(int(lag_min), int(lag_max) + 1)]
    if int(delay_seconds) not in lags:
        lags.append(int(delay_seconds))
        lags.sort()

    counts = {}
    for L in lags:
        dl = timedelta(seconds=L)
        c = 0
        for t, v, p in trades:
            c += len(idx.get(t + dl, ()))
        counts[L] = c
    a.lag_counts = counts

    target = int(delay_seconds)
    a.target_pairs = counts.get(target, 0)
    others = [counts[L] for L in counts if L != target]
    if len(others) < 2:
        a.reason = "比較するラグが足りない"
        return a
    mu = statistics.mean(others)
    sd = statistics.pstdev(others)
    a.zscore = (a.target_pairs - mu) / sd if sd > 0 else 0.0

    if a.target_pairs < min_pairs:
        a.reason = f"ペア数が少なすぎる（{a.target_pairs}件 < {min_pairs}件）"
        return a

    # 目標ラグのペアについて、10秒後側のロットと値動きを集計する
    dl = timedelta(seconds=target)
    lots = Counter()
    for t, v, p in trades:
        for v2, p2 in idx.get(t + dl, ()):
            lots[v2] += 1
            if p2 > p:
                a.up += 1
            elif p2 == p:
                a.flat += 1
            else:
                a.down += 1
    if lots:
        lot, n = lots.most_common(1)[0]
        a.dominant_lot = lot
        a.dominant_lot_ratio = n / sum(lots.values())

    a.ok = True
    return a


class ZScoreBuyDetector:
    """銘柄ごとに判定し、しきい値を超えたら通知する。同じ段階は1日1回だけ。"""

    def __init__(self, delay_seconds=10.0, lag_min=6, lag_max=14,
                 z_threshold=5.0, strong_z_threshold=10.0,
                 min_trades=50, min_pairs=20, min_buy_ratio=0.0):
        self.delay_seconds = delay_seconds
        self.lag_min = lag_min
        self.lag_max = lag_max
        self.z_threshold = z_threshold
        self.strong_z_threshold = strong_z_threshold
        self.min_trades = min_trades
        self.min_pairs = min_pairs
        self.min_buy_ratio = min_buy_ratio
        self.day = None
        self.fired = set()          # (symbol, tier)
        self.last = {}              # symbol -> Analysis（集計ログ用）

    def _roll_day(self, day):
        if self.day != day:
            self.day = day
            self.fired = set()
            self.last = {}

    def update(self, symbol, trades, day=None) -> list:
        """当日ぶんの歩み値を渡して判定する。発火したアラートのリストを返す。"""
        if day is None and trades:
            day = trades[-1][0].date()
        self._roll_day(day)

        a = analyze(symbol, trades, self.delay_seconds, self.lag_min, self.lag_max,
                    self.min_trades, self.min_pairs)
        self.last[str(symbol)] = a
        if not a.ok:
            return []
        if a.buy_ratio < self.min_buy_ratio:
            return []

        alerts = []
        sym = str(symbol)
        # 上位の段階から見る。STRONGを出したら、以後WATCHは出さない
        # （下位の段階もまとめて発火済みにする）
        tiers = (("STRONG", self.strong_z_threshold), ("WATCH", self.z_threshold))
        for i, (tier, th) in enumerate(tiers):
            if a.zscore >= th and (sym, tier) not in self.fired:
                for lower, _ in tiers[i:]:
                    self.fired.add((sym, lower))
                alerts.append({"symbol": sym, "tier": tier,
                               "zscore": a.zscore, "pairs": a.target_pairs,
                               "trades": a.trades, "delay": self.delay_seconds,
                               "dominant_lot": a.dominant_lot,
                               "dominant_lot_ratio": a.dominant_lot_ratio,
                               "buy_ratio": a.buy_ratio,
                               "lag_counts": dict(a.lag_counts)})
                break     # 上位の段階だけ通知する
        return alerts

    def ranking(self, top=10):
        """z値の高い順に並べる（定期集計ログ用）。"""
        rows = [(a.zscore, s, a) for s, a in self.last.items() if a.trades]
        rows.sort(reverse=True)
        return rows[:top]
