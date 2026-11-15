# -*- coding: utf-8 -*-
"""定期買い集め検知（z値方式）の歩み値処理のテスト。

2026-11-16 に、歩み値を辞書・datetime に展開する実装から、当日分だけを数値配列
（TodayTrades）に詰める実装へ置き換えた（メモリ不足で MemoryError が出ていたため）。
ここでは次を固定する。
  ・判定結果（ラグごとの件数・z値・主なロット・上昇/同値/下落）が旧実装と完全に一致する
  ・応答から取り込む約定の条件が旧実装と同じ（当日分のみ・Time と Price が必須）
  ・キーの順番や空白の違いで取り込みが変わらない

旧実装は下の _reference_analyze() にそのまま残し、比較の基準にしている。

外部ライブラリもネットワークも使わない（kabuステーションには接続しない）。

実行:
    python tests/test_zscore_detector.py
"""
import json
import logging
import os
import random
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta

BASE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.normpath(os.path.join(BASE, ".."))
SRC = os.path.join(REPO, "strategies", "periodic_buy_zscore", "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import zscore_detector as Z          # noqa: E402
import main as M                     # noqa: E402  periodic_buy_zscore/src/main.py

TODAY = "2026-11-13"
YESTERDAY = "2026-11-12"
LOG = logging.getLogger("test_zscore")


# ── 旧実装（2026-11-16 以前）。比較の基準 ──────────────────────────
def _reference_fetch(d, today_iso):
    out = []
    for r in d.get("TradingPrice") or []:
        t = r.get("Time")
        p = r.get("Price")
        if not t or p is None or str(t)[:10] != today_iso:
            continue
        try:
            out.append((datetime.fromisoformat(str(t)), r.get("Volume"), p))
        except ValueError:
            continue
    out.sort(key=lambda x: x[0])
    return out


def _reference_analyze(symbol, trades, delay_seconds=10, lag_min=6, lag_max=14,
                       min_trades=50, min_pairs=20):
    a = Z.Analysis(symbol=str(symbol), trades=len(trades))
    if len(trades) < min_trades:
        return a
    idx = defaultdict(list)
    for t, v, p in trades:
        idx[t].append((v, p))
    lags = list(range(int(lag_min), int(lag_max) + 1))
    if int(delay_seconds) not in lags:
        lags.append(int(delay_seconds))
        lags.sort()
    counts = {}
    for L in lags:
        dl = timedelta(seconds=L)
        counts[L] = sum(len(idx.get(t + dl, ())) for t, v, p in trades)
    a.lag_counts = counts
    target = int(delay_seconds)
    a.target_pairs = counts.get(target, 0)
    others = [counts[L] for L in counts if L != target]
    mu = statistics.mean(others)
    sd = statistics.pstdev(others)
    a.zscore = (a.target_pairs - mu) / sd if sd > 0 else 0.0
    if a.target_pairs < min_pairs:
        return a
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


# ── テスト用の応答 ────────────────────────────────────────────────
def make_records(n_per_day, seed, algo_every=0):
    """2営業日ぶんの約定。同じ秒に複数約定が入るよう、秒は乱数で選ぶ。

    algo_every>0 なら当日に「約定の10秒後に100株の買い上がり」を混ぜる（買い集めアルゴ）。
    """
    rnd = random.Random(seed)
    recs = []
    for day in (YESTERDAY, TODAY):
        base = datetime.fromisoformat(day + "T09:00:00+09:00")
        for _ in range(n_per_day):
            t = base + timedelta(seconds=rnd.randint(0, 18000))
            price = float(1000 + rnd.randint(-20, 20))
            recs.append({"Time": t.isoformat(), "Volume": float(rnd.choice([100, 200, 300, 1000])),
                         "Price": price})
            if day == TODAY and algo_every and rnd.randint(1, algo_every) == 1:
                recs.append({"Time": (t + timedelta(seconds=10)).isoformat(),
                             "Volume": 100.0, "Price": price + 1})
    rnd.shuffle(recs)                # APIの並び順に依存しないことも確かめる
    return recs


def body(recs, **dumps_kw):
    return json.dumps({"Symbol": "9999", "TradingPriceCount": len(recs),
                       "TradingPrice": recs}, **dumps_kw).encode("utf-8")


def compare(a, b):
    for k in ("trades", "lag_counts", "target_pairs", "zscore", "dominant_lot",
              "dominant_lot_ratio", "up", "flat", "down", "ok"):
        assert getattr(a, k) == getattr(b, k), f"{k}: 旧={getattr(a, k)!r} 新={getattr(b, k)!r}"


# ── 判定結果が旧実装と一致する ────────────────────────────────────
def test_same_result_as_reference_random():
    for seed in range(5):
        recs = make_records(3000, seed)
        raw = body(recs, separators=(",", ":"))
        old = _reference_analyze("9999", _reference_fetch(json.loads(raw), TODAY))
        new = Z.analyze("9999", M.parse_today(raw, TODAY))
        compare(old, new)


def test_same_result_as_reference_with_algo():
    """買い集めアルゴが混ざり z値が高く出る場合も一致する（ロット・値動きの集計を含む）。"""
    recs = make_records(3000, 42, algo_every=3)
    raw = body(recs)
    old = _reference_analyze("9999", _reference_fetch(json.loads(raw), TODAY))
    new = Z.analyze("9999", M.parse_today(raw, TODAY))
    compare(old, new)
    assert new.ok and new.zscore > 10, new.zscore
    assert new.dominant_lot == 100.0


def test_same_result_with_missing_volume():
    """数量が null の約定があっても、主なロットの数え方が旧実装と同じ。"""
    recs = make_records(800, 7)
    for r in recs[::5]:
        r["Volume"] = None
    raw = body(recs)
    old = _reference_analyze("9999", _reference_fetch(json.loads(raw), TODAY))
    new = Z.analyze("9999", M.parse_today(raw, TODAY))
    compare(old, new)


def test_analyze_accepts_datetime_list():
    """従来どおり [(datetime, volume, price)] を渡しても同じ結果になる。"""
    recs = make_records(1500, 3, algo_every=4)
    trades = _reference_fetch({"TradingPrice": recs}, TODAY)
    compare(_reference_analyze("9999", trades), Z.analyze("9999", trades))


def test_too_few_trades():
    a = Z.analyze("9999", M.parse_today(body(make_records(10, 1)), TODAY))
    assert not a.ok and a.trades == 10 and "少なすぎる" in a.reason


# ── 応答からの取り込み条件 ────────────────────────────────────────
def test_parse_keeps_only_today():
    recs = [
        {"Time": "2026-11-12T14:59:59+09:00", "Volume": 100.0, "Price": 500.0},   # 前日
        {"Time": "2026-11-13T09:00:01+09:00", "Volume": 200.0, "Price": 501.0},
        {"Time": "2026-11-13T15:30:00+09:00", "Volume": 300.0, "Price": 502.0},
        {"Time": "2026-11-13T10:00:00+09:00", "Volume": 100.0, "Price": None},    # 価格なし
        {"Time": None, "Volume": 100.0, "Price": 503.0},                          # 時刻なし
        {"Time": "2026-11-13Txx:yy:zz+09:00", "Volume": 100.0, "Price": 504.0},   # 読めない
    ]
    tt = M.parse_today(body(recs), TODAY)
    assert len(tt) == 2
    assert list(tt.sec) == [9 * 3600 + 1, 15 * 3600 + 30 * 60]
    assert list(tt.vol) == [200.0, 300.0]
    assert list(tt.px) == [501.0, 502.0]


def test_parse_ignores_key_order_and_spacing():
    """キーの順番・空白・整数表記が違っても取り込み結果は同じ。"""
    a = b'{"Symbol":"1","TradingPrice":[{"Time":"2026-11-13T09:00:05+09:00","Volume":100,"Price":10}]}'
    b = (b'{ "TradingPrice" : [ { "Price" : 10.0 , "Volume" : 100.0 ,\n'
         b'   "Time" : "2026-11-13T09:00:05+09:00" } ] , "Symbol" : "1" }')
    ta, tb = M.parse_today(a, TODAY), M.parse_today(b, TODAY)
    assert (list(ta.sec), list(ta.vol), list(ta.px)) == (list(tb.sec), list(tb.vol), list(tb.px))
    assert list(ta.sec) == [9 * 3600 + 5]


def test_parse_empty_response():
    assert len(M.parse_today(b'{"Symbol":"1","TradingPrice":[]}', TODAY)) == 0
    assert len(M.parse_today(b'{"Symbol":"1"}', TODAY)) == 0
    assert len(M.parse_today(b'{"Symbol":"1","TradingPrice":null}', TODAY)) == 0


# ── 取得から通知までのつながり ────────────────────────────────────
class FakeClient:
    def __init__(self, raw=None, error=None):
        self.raw = raw
        self.error = error
        self.calls = 0

    def get_time_and_sales_raw(self, symbol, exchange=1):
        self.calls += 1
        if self.error:
            raise self.error
        return self.raw


def test_fetch_today_returns_today_trades():
    raw = body(make_records(500, 9))
    day = datetime.fromisoformat(TODAY).date()
    tt = M.fetch_today(FakeClient(raw), "9999", 1, TODAY, LOG, day=day)
    assert isinstance(tt, Z.TodayTrades) and len(tt) == 500 and tt.day == day


def test_fetch_today_failure_returns_none():
    M._fetch_fail.update(streak=0, last_log=0.0)
    assert M.fetch_today(FakeClient(error=RuntimeError("HTTP 500")), "9999", 1, TODAY, LOG) is None
    assert M._fetch_fail["streak"] == 1
    # 応答が壊れていても落ちずに None
    assert M.fetch_today(FakeClient(raw=b"{broken"), "9999", 1, TODAY, LOG) is None
    M._fetch_fail.update(streak=0, last_log=0.0)


def test_detector_fires_strong_once_per_day():
    det = Z.ZScoreBuyDetector()
    raw = body(make_records(3000, 42, algo_every=3))
    day = datetime.fromisoformat(TODAY).date()
    tt = M.fetch_today(FakeClient(raw), "9999", 1, TODAY, LOG, day=day)
    alerts = det.update("9999", tt, day)
    assert [a["tier"] for a in alerts] == ["STRONG"]
    assert alerts[0]["dominant_lot"] == 100.0
    title, body_text = M.build_message(alerts[0])          # 通知文が組み立てられること
    assert "定期買い集め/STRONG" in title and "100株" in body_text
    assert det.update("9999", tt, day) == []                # 同じ日は二度出さない


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
        except Exception as e:
            failed += 1
            print(f"  NG  {t.__name__}: {type(e).__name__} {e}")
        else:
            print(f"  ok  {t.__name__}")
    print(f"\n{len(tests) - failed}/{len(tests)} 成功")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
