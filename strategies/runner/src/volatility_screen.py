# -*- coding: utf-8 -*-
"""直近でボラティリティが急変した銘柄の判定（日足・前日までのデータのみ）。

「ボラ急変銘柄 売り板消化反発」（volatile_panic_rebound）が起動時に対象銘柄を
決めるのに使う。バックテスト（analysis/research_panic_volatile.py）も同じ
features() / passes() を使うので、検証と実運用で判定がずれない。

判定（すべて**前日までの**日足。当日の値を使うと先読みになる）:
  前日の売買代金（終値×出来高）が min_prev_turnover_oku 億円以上 で、かつ
    (a) 直近3営業日のうち1日でも騰落率の絶対値が big_move_pct% 以上（急騰・急落）
    (b) 直近3営業日の平均値幅 ÷ 直近20営業日の平均値幅 が vol_expand_ratio 以上（ボラ拡大）
  の (a) または (b)。値幅 = (高値-安値) / 前日終値。

株価は Yahoo Finance の日足（公開値）。口座には触れない。
"""
import json
import time
import urllib.request
from datetime import datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))
OKU = 100_000_000

DEFAULTS = {
    "min_prev_turnover_oku": 1.0,   # 前日の売買代金（億円）
    "big_move_pct": 5.0,            # 直近3日の急騰・急落（騰落率の絶対値%）
    "vol_expand_ratio": 1.5,        # 3日平均値幅 / 20日平均値幅
}


def fetch_daily(sym, since, until, timeout=25):
    """Yahoo日足 [[date, open, high, low, close, volume], ...]（古い順）。"""
    p1 = int(datetime.strptime(since, "%Y-%m-%d").replace(tzinfo=JST).timestamp())
    p2 = int((datetime.strptime(until, "%Y-%m-%d").replace(tzinfo=JST)
              + timedelta(days=1)).timestamp())
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}.T"
           f"?period1={p1}&period2={p2}&interval=1d")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        j = json.load(resp)
    res = j["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    rows = []
    for k, ts in enumerate(res.get("timestamp") or []):
        o, h, lo, c, v = (q["open"][k], q["high"][k], q["low"][k],
                          q["close"][k], q["volume"][k])
        if None in (o, h, lo, c, v):
            continue
        rows.append([datetime.fromtimestamp(ts, JST).date().isoformat(), o, h, lo, c, v])
    return rows


def features(rows, day):
    """day（YYYY-MM-DD）の**前日まで**の日足から特徴量を作る。足りなければ None。"""
    idx = [i for i, r in enumerate(rows) if r[0] < day]
    if not idx:
        return None
    k = idx[-1]
    if k < 21:                       # 20日平均と前日終値の分が要る
        return None
    c = [r[4] for r in rows]
    h = [r[2] for r in rows]
    lo = [r[3] for r in rows]
    v = [r[5] for r in rows]

    def rng(j):
        return (h[j] - lo[j]) / c[j - 1] * 100

    def ret(j):
        return (c[j] / c[j - 1] - 1) * 100

    r3 = sum(rng(j) for j in range(k - 2, k + 1)) / 3
    r20 = sum(rng(j) for j in range(k - 19, k + 1)) / 20
    return {
        "prev_day": rows[k][0],
        "turnover": c[k] * v[k],                                       # 前日の売買代金（円）
        "prev_range": rng(k),                                          # 前日の値幅%
        "prev_ret": ret(k),                                            # 前日の騰落率%
        "maxabsret3": max(abs(ret(j)) for j in range(k - 2, k + 1)),   # 直近3日の最大|騰落率|%
        "vol_ratio": r3 / r20 if r20 else 0.0,                         # ボラ拡大率
        "ret5": (c[k] / c[k - 5] - 1) * 100,                           # 5日騰落率%
    }


def passes(f, cfg=None):
    """ボラ急変銘柄の条件を満たすか。f が None（日足不足）なら False。"""
    if f is None:
        return False
    c = dict(DEFAULTS, **(cfg or {}))
    if f["turnover"] < float(c["min_prev_turnover_oku"]) * OKU:
        return False
    big_move = f["maxabsret3"] >= float(c["big_move_pct"])
    expand = f["vol_ratio"] >= float(c["vol_expand_ratio"])
    return big_move or expand


def screen(symbols, day, cfg=None, log=None, sleep=0.4, fetch=fetch_daily):
    """監視銘柄を判定し、(合格銘柄のリスト, {銘柄: 特徴量}) を返す。

    day は判定する当日。特徴量は day の前日までで作る。
    取得に失敗した銘柄は不合格扱い（入らない＝安全側）。
    """
    since = (datetime.strptime(day, "%Y-%m-%d") - timedelta(days=60)).date().isoformat()
    passed, detail, errors = [], {}, []
    for sym in symbols:
        try:
            f = features(fetch(str(sym), since, day), day)
        except Exception as e:           # 1銘柄の失敗で全体を止めない
            errors.append((str(sym), str(e)[:60]))
            f = None
        detail[str(sym)] = f
        if passes(f, cfg):
            passed.append(str(sym))
        if sleep:
            time.sleep(sleep)
    if log is not None and errors:
        log.warning("ボラ急変銘柄の判定: 日足を取れなかった %d銘柄は対象外にしました %s",
                    len(errors), [s for s, _ in errors])
    return passed, detail
