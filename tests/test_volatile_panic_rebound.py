# -*- coding: utf-8 -*-
"""ボラ急変銘柄 売り板消化反発（volatile_panic_rebound）の条件を固定するテスト。

背景（2026-09-25のバックテスト。analysis/research_panic_volatile.py）:
  直近でボラが急変し前日の売買代金が1億円を超える銘柄で、寄り後に始値を下回った
  局面の売り板消化（投げ売り検知 DUMP/ABSORBED）を仮想買い。利確+3%/損切り-1%。

ここで固定する仕様:
  判定（runner/src/volatility_screen.py）
    1. 特徴量は**前日までの**日足だけで作る（当日の足を混ぜない＝先読みなし）
    2. 前日の売買代金1億円未満は対象外
    3. 直近3日の急騰・急落（±5%）または ボラ拡大（3日/20日≥1.5）のどちらかで対象
    4. 日足が取れない銘柄は対象外（安全側）
  戦略（AIStrategys/src/detector.py）
    5. 入力は panic_sell_detector の DUMP/ABSORBED のみ
    6. 対象銘柄（set_universe）以外・未確定（空）なら入らない
    7. 始値を下回っているときだけ入る。始値が不明なら入らない
    8. 利確+3%/損切り-1%、同一銘柄1日1回
  ランナー（runner/src/main.py）
    9. 前日の始値（OpeningPriceTime が今日でない）を掴まない
   10. 検知アラートに当日の始値が添えられ、戦略まで届く

外部ライブラリもネットワークも使わない。

実行:
    python tests/test_volatile_panic_rebound.py
"""
import os
import sys
from datetime import datetime, date, timedelta, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.normpath(os.path.join(BASE, ".."))
sys.path.insert(0, os.path.join(REPO, "strategies", "runner", "src"))
sys.path.insert(0, os.path.join(REPO, "strategies", "AIStrategys", "src"))

import volatility_screen as vs  # noqa: E402
import detector as d            # noqa: E402

JST = timezone(timedelta(hours=9))
DAY = "2026-09-25"


def at(h, m, s=0):
    return datetime(2026, 9, 25, h, m, s, tzinfo=JST)


# ── 日足の合成データ ─────────────────────────────────────────────────
def flat_rows(n=30, end="2026-09-24", close=1000.0, vol=200_000):
    """値幅2%・騰落0%の平らな日足を n 本（end まで）。売買代金は close*vol。"""
    end_d = date.fromisoformat(end)
    rows = []
    for i in range(n):
        dd = (end_d - timedelta(days=n - 1 - i)).isoformat()
        rows.append([dd, close, close * 1.01, close * 0.99, close, vol])
    return rows


def test_features_use_only_days_before():
    """当日（DAY）の足があっても使わない。前日の足が最後になる。"""
    rows = flat_rows(end="2026-09-24")
    rows.append([DAY, 1000.0, 2000.0, 500.0, 1500.0, 9_999_999])   # 当日の異常値
    f = vs.features(rows, DAY)
    assert f["prev_day"] == "2026-09-24"
    assert abs(f["prev_ret"]) < 1e-9            # 当日の+50%を見ていない
    assert abs(f["prev_range"] - 2.0) < 1e-9


def test_turnover_below_threshold_excluded():
    """前日の売買代金が1億円未満なら、急騰していても対象外。"""
    rows = flat_rows(vol=50_000)                 # 1000円×5万株=5000万円
    rows[-1] = [rows[-1][0], 1000.0, 1100.0, 1000.0, 1080.0, 50_000]   # +8%
    f = vs.features(rows, DAY)
    assert f["turnover"] < vs.OKU
    assert not vs.passes(f)


def test_big_move_passes():
    """直近3日のうち1日でも±5%以上動いていれば対象（急落も含む）。"""
    rows = flat_rows()
    rows[-2] = [rows[-2][0], 1000.0, 1000.0, 930.0, 940.0, 200_000]    # -6%（急落）
    f = vs.features(rows, DAY)
    assert f["maxabsret3"] >= 5.0
    assert vs.passes(f)


def test_vol_expansion_passes():
    """騰落は小さくても、直近3日の値幅が20日平均の1.5倍以上に広がれば対象。"""
    rows = flat_rows()
    for j in (-3, -2, -1):                       # 値幅2%→6%、終値は据え置き
        rows[j] = [rows[j][0], 1000.0, 1030.0, 970.0, 1000.0, 200_000]
    f = vs.features(rows, DAY)
    assert f["maxabsret3"] < 5.0
    assert f["vol_ratio"] >= 1.5
    assert vs.passes(f)


def test_quiet_stock_excluded():
    """急騰落もボラ拡大もない銘柄は対象外。"""
    f = vs.features(flat_rows(), DAY)
    assert f["turnover"] >= vs.OKU
    assert not vs.passes(f)


def test_screen_excludes_fetch_failures():
    """日足が取れない銘柄は対象外になり、他の銘柄の判定は続く。"""
    good = flat_rows()
    good[-1] = [good[-1][0], 1000.0, 1100.0, 1000.0, 1080.0, 200_000]  # +8%

    def fake_fetch(sym, since, until):
        if sym == "9999":
            raise RuntimeError("network")
        return good if sym == "5707" else flat_rows()

    passed, detail = vs.screen(["5707", "9999", "3692"], DAY, fetch=fake_fetch, sleep=0)
    assert passed == ["5707"]
    assert detail["9999"] is None


# ── 戦略 ───────────────────────────────────────────────────────────
def make():
    s = d.VolatilePanicReboundStrategy()
    s.set_universe(["5707", "3692"])
    return s


def panic(sym, price, stage="DUMP", open_price=1000.0):
    return {"symbol": sym, "price": price, "stage": stage, "open_price": open_price}


def test_enters_on_dump_below_open():
    s = make()
    out = s.on_signal("panic_sell_detector", panic("5707", 980.0), at(10, 0))
    assert out and out[0]["type"] == "ENTRY"
    assert out[0]["trigger"] == "投げ売り"
    assert abs(out[0]["open_dev_pct"] - (-2.0)) < 1e-9


def test_absorbed_is_also_entry():
    s = make()
    out = s.on_signal("panic_sell_detector", panic("3692", 990.0, stage="ABSORBED"), at(10, 0))
    assert out and out[0]["trigger"] == "投げ売り吸収"


def test_not_below_open_is_skipped():
    """始値より上（寄り後に下落していない）なら入らない。"""
    s = make()
    assert s.on_signal("panic_sell_detector", panic("5707", 1010.0), at(10, 0)) == []


def test_unknown_open_is_skipped():
    """始値が不明なら判定できないので入らない。"""
    s = make()
    assert s.on_signal("panic_sell_detector", panic("5707", 980.0, open_price=None),
                       at(10, 0)) == []


def test_outside_universe_or_empty_is_skipped():
    s = make()
    assert s.on_signal("panic_sell_detector", panic("1234", 980.0), at(10, 0)) == []
    empty = d.VolatilePanicReboundStrategy()      # 対象が未確定
    assert empty.on_signal("panic_sell_detector", panic("5707", 980.0), at(10, 0)) == []


def test_only_panic_sell_source():
    s = make()
    assert s.on_signal("under_surge_detector", panic("5707", 980.0), at(10, 0)) == []


def test_take_profit_3_and_stop_loss_1():
    s = make()
    s.on_signal("panic_sell_detector", panic("5707", 1000.0, open_price=1010.0), at(10, 0))
    assert s.on_price("5707", 1020.0, at(10, 5)) == []          # +2%ではまだ（2026-09-26変更）
    out = s.on_price("5707", 1030.0, at(10, 10))                # +3%で利確
    assert out and out[0]["reason"] == "利確"

    s2 = make()
    s2.on_signal("panic_sell_detector", panic("3692", 1000.0, open_price=1010.0), at(10, 0))
    out = s2.on_price("3692", 990.0, at(10, 5))                 # -1%で損切り
    assert out and out[0]["reason"] == "損切り"


def test_one_entry_per_symbol_per_day():
    s = make()
    assert s.on_signal("panic_sell_detector", panic("5707", 980.0), at(10, 0))
    s.on_price("5707", 1000.0, at(10, 5))                       # 利確で建玉は閉じる
    assert s.on_signal("panic_sell_detector", panic("5707", 975.0), at(11, 0)) == []


# ── ランナーの配線 ─────────────────────────────────────────────────
def _engine():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "runner_main_for_test",
        os.path.join(REPO, "strategies", "runner", "src", "main.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m.RunnerEngine({"strategies": {"volatile_panic_rebound": {"enabled": True}}})


def test_runner_ignores_previous_day_open():
    """OpeningPriceTime が今日でない始値（寄り前の前日ぶん）は採らない。"""
    eng = _engine()
    eng.handle({"Symbol": "5707", "CurrentPrice": 1000.0, "OpeningPrice": 990.0,
                "OpeningPriceTime": "2026-09-24T09:00:00+09:00"}, now=at(8, 55))
    assert "5707" not in eng._open_price
    eng.handle({"Symbol": "5707", "CurrentPrice": 1000.0, "OpeningPrice": 1010.0,
                "OpeningPriceTime": "2026-09-25T09:00:00+09:00"}, now=at(9, 1))
    assert eng._open_price["5707"] == 1010.0


def test_runner_passes_open_price_to_strategy():
    """検知アラートに当日の始値が添えられ、戦略が始値比で判定してエントリーする。"""
    eng = _engine()
    eng.ai_strategies["volatile_panic_rebound"].set_universe(["5707"])
    eng.handle({"Symbol": "5707", "CurrentPrice": 1010.0, "OpeningPrice": 1010.0,
                "OpeningPriceTime": "2026-09-25T09:00:00+09:00"}, now=at(9, 1))
    eng.submit_external("panic_sell_detector",
                        {"symbol": "5707", "stage": "DUMP", "price": 990.0,
                         "qty_removed": 5000, "matched_qty": 5000})
    res = eng.handle({"Symbol": "5707", "CurrentPrice": 990.0}, now=at(10, 0))
    entries = [a for n, a in res if n == "volatile_panic_rebound" and a.get("type") == "ENTRY"]
    assert entries, res
    assert abs(entries[0]["open_dev_pct"] - (990.0 / 1010.0 - 1) * 100) < 1e-9


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            failed += 1
            print(f"  NG  {t.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"  NG  {t.__name__}: {type(e).__name__} {e}")
        else:
            print(f"  ok  {t.__name__}")
    print(f"\n{len(tests) - failed}/{len(tests)} 成功")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
