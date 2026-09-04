# -*- coding: utf-8 -*-
"""PUSHの現在値と出来高から当日のVWAP（出来高加重平均価格）を積み上げる。

VWAPは「その日その銘柄を売買した人たちの平均取得コスト」。ここからどれだけ
下に離れているかは、板の厚みとは別の角度から需給を映す。

kabuステーションAPIの TradingVolume は**当日の累計**出来高なので、前回値との
差分がその間に成立した出来高になる。約定ごとに 価格×出来高 を足していけば、
5分足から計算するより細かいVWAPが得られる。

日付が変わったら自動でリセットする（前日の売買を混ぜない）。

【なぜ検知側ではなくここに置くか】
VWAPは特定の検知に紐づく値ではなく、板と同じ「市場の状態」。どの
ストラテジーからも参照できるよう、エンジンが1つ持って配る形にしている。
"""


class VwapTracker:
    """銘柄ごとの当日VWAP。PUSHを受けるスレッドからのみ更新する。"""

    __slots__ = ("_state",)

    def __init__(self):
        # symbol -> [日付, Σ(価格×出来高), Σ出来高, 前回の累計出来高]
        self._state = {}

    def update(self, symbol, price, cum_volume, now) -> None:
        """PUSH1件ぶんを取り込む。価格か出来高が欠けていれば何もしない。"""
        if symbol is None or price is None or cum_volume is None or now is None:
            return
        try:
            price = float(price)
            cum_volume = float(cum_volume)
        except (TypeError, ValueError):
            return
        if price <= 0 or cum_volume < 0:
            return

        key = str(symbol)
        day = now.date()
        s = self._state.get(key)
        if s is None or s[0] != day:
            # その日の最初の1件。寄り付きの板寄せぶんはすべて寄り値で
            # 成立したものとして扱う（実際の内訳は取得できないため）。
            self._state[key] = [day, price * cum_volume, cum_volume, cum_volume]
            return

        delta = cum_volume - s[3]
        if delta < 0:
            # 累計が減ることは通常ないが、日付をまたぐ取りこぼしや
            # 再接続で起こりうる。その場合は積み上げをやり直す。
            self._state[key] = [day, price * cum_volume, cum_volume, cum_volume]
            return
        if delta > 0:
            s[1] += price * delta
            s[2] += delta
        s[3] = cum_volume

    def get(self, symbol, now=None):
        """当日のVWAP。まだ計算できなければ None。"""
        s = self._state.get(str(symbol))
        if s is None or s[2] <= 0:
            return None
        if now is not None and s[0] != now.date():
            return None
        return s[1] / s[2]

    def discount_pct(self, symbol, price, now=None):
        """VWAPからの乖離(%)。負なら平均コストより安い。取れなければ None。"""
        v = self.get(symbol, now)
        if v is None or not v or price is None:
            return None
        try:
            return (float(price) / v - 1.0) * 100.0
        except (TypeError, ValueError, ZeroDivisionError):
            return None

    def reset(self) -> None:
        self._state.clear()
