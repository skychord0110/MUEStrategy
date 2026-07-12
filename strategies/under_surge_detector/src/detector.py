"""UNDER急増（下値への大口買い出現）検知ロジック。

検知する現象:
  当日安値圏（下落局面）で、板のUNDER（買い気配10本より下に隠れている買い注文の合計
  = UnderBuyQty）が1回のPUSH更新間で一気に20%以上増加し、かつOVER（売り側の隠れ注文
  = OverSellQty）がほぼ変わっていない場合、「下値に大口の買い指値が置かれた（買い支え出現）」
  とみなして通知する。

  OVER不変の条件は「売り側は動いておらず、純粋に買いだけが積まれた」ことの確認。
  売り買い両方が同時に増えた場合（板全体の組み変わり・別要因）を除外する。

板の窓シフトについて:
  価格が下落すると表示10本の買い窓が下にずれ、UNDERにあった注文が表示側に吸われるため
  UnderBuyQtyは機械的には「減る」方向に動く。下落中の急増はこのドリフトに逆らう動きであり、
  新規の買い注文が実際に置かれたことを強く示唆する（誤検知しにくい方向の設計）。
  逆に価格上昇中は表示側からUNDERへ機械的に流入して増えるが、安値圏条件で除外される。

板寄せ直後の抑制:
  寄り付き・後場寄り直後は板が一気に組み変わりUNDERも大きく動くため、
  quiet_windows（既定: 9:00〜9:01、12:30〜12:31）の時間帯は通知を抑制する。

前提:
  kabuステーションAPIのPUSH配信スナップショットの差分に基づく。詳細は ../README.md を参照。
"""
from dataclasses import dataclass
from datetime import time as dtime


@dataclass
class SymbolState:
    prev_under: float = None
    prev_over: float = None
    last_alert_time: object = None  # datetime


# 板寄せ直後の通知抑制時間帯（開始時刻, 終了時刻）
DEFAULT_QUIET_WINDOWS = [
    (dtime(9, 0), dtime(9, 1)),     # 寄り付き直後
    (dtime(12, 30), dtime(12, 31)), # 後場寄り直後
]


class UnderSurgeDetector:
    def __init__(self, under_increase_pct: float = 0.20,
                 over_change_tolerance_pct: float = 0.05,
                 low_zone_pct: float = 0.01,
                 cooldown_seconds: float = 60,
                 quiet_windows: list = None):
        self.under_increase_pct = under_increase_pct
        self.over_change_tolerance_pct = over_change_tolerance_pct
        self.low_zone_pct = low_zone_pct
        self.cooldown_seconds = cooldown_seconds
        self.quiet_windows = DEFAULT_QUIET_WINDOWS if quiet_windows is None else quiet_windows
        self.states = {}

    def _in_quiet_window(self, msg_time) -> bool:
        if msg_time is None:
            return False
        t = msg_time.time()
        return any(start <= t <= end for start, end in self.quiet_windows)

    def _state(self, symbol: str) -> SymbolState:
        return self.states.setdefault(symbol, SymbolState())

    def update(self, symbol: str, msg_time, current_price, low_price,
               under_buy_qty, over_sell_qty):
        """1件のPUSHメッセージを処理し、発火したアラートのリストを返す。"""
        state = self._state(symbol)
        alerts = []

        prev_under = state.prev_under
        prev_over = state.prev_over

        # スナップショットは毎メッセージ更新（判定は直前値との比較で行う）
        if under_buy_qty is not None:
            state.prev_under = under_buy_qty
        if over_sell_qty is not None:
            state.prev_over = over_sell_qty

        if (prev_under is None or prev_over is None
                or under_buy_qty is None or over_sell_qty is None
                or prev_under <= 0 or prev_over <= 0
                or current_price is None or low_price is None or low_price <= 0):
            return alerts

        # 板寄せ直後（quiet_windows）の時間帯は板が一気に組み変わるため通知しない
        if self._in_quiet_window(msg_time):
            return alerts

        # 条件1: 安値圏（下落局面）にいること
        in_low_zone = current_price <= low_price * (1 + self.low_zone_pct)
        if not in_low_zone:
            return alerts

        # 条件2: UNDERが1回の更新で急増（直前のUNDERに対する割合で判定）
        under_delta = under_buy_qty - prev_under
        if under_delta < prev_under * self.under_increase_pct:
            return alerts

        # 条件3: OVERはほぼ変わっていない（売り側は動いていない）
        if abs(over_sell_qty - prev_over) > prev_over * self.over_change_tolerance_pct:
            return alerts

        # 連続通知の抑制（同一銘柄はcooldown_seconds間隔を空ける）
        if (state.last_alert_time is not None and msg_time is not None
                and (msg_time - state.last_alert_time).total_seconds() < self.cooldown_seconds):
            return alerts

        state.last_alert_time = msg_time
        alerts.append({
            "symbol": symbol,
            "prev_under": prev_under,
            "under": under_buy_qty,
            "under_delta": under_delta,
            "increase_pct": under_delta / prev_under * 100,
            "price": current_price,
            "time": msg_time,
        })
        return alerts
