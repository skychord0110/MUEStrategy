# -*- coding: utf-8 -*-
"""流動UNDER急増戦略（LiquidUnderSurgeStrategy）の条件を固定するテスト。

背景（2026-09-13の検証）:
  資金増でのマーケットインパクトを避けるため、監視50銘柄を流動条件で絞った
  少数銘柄だけを対象に、終日のUNDER急増を仮想買い→大引け決済する戦略。
  独立検証（Yahoo5分足）で 流動7銘柄・全日・大引け決済 n=71 勝率64.8% 期待値+0.73%。

ここで固定する仕様:
  1. 対象は set_liquid_universe() で渡した流動銘柄のみ（未設定=空なら何も入らない）
  2. 入力トリガーは UNDER急増（under_surge_detector）のみ
  3. 終日エントリー可（既定 09:00〜15:00）。午後限定にしない
  4. take_profit_pct=None のとき利確を置かず、損切りに触れなければ大引けまで持つ
  5. 同一銘柄は1日1回まで

外部ライブラリもネットワークも使わない。

実行:
    python tests/test_liquid_under_surge.py
"""
import os
import sys
from datetime import datetime, time as dtime, timezone, timedelta

BASE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.normpath(os.path.join(BASE, ".."))
sys.path.insert(0, os.path.join(REPO, "strategies", "AIStrategys", "src"))

import detector as d  # noqa: E402

JST = timezone(timedelta(hours=9))


def at(h, m, s=0):
    return datetime(2026, 9, 14, h, m, s, tzinfo=JST)


def make(**kw):
    s = d.LiquidUnderSurgeStrategy(**kw)
    s.set_liquid_universe(["5707", "3692", "9556"])
    return s


def under(symbol, price):
    return {"symbol": symbol, "price": price}


def test_enters_only_liquid_symbols():
    """流動ユニバースの銘柄だけエントリーする。"""
    s = make()
    assert s.on_signal("under_surge_detector", under("5707", 1200.0), at(10, 0))
    # 対象外の銘柄は無視
    assert s.on_signal("under_surge_detector", under("9999", 1200.0), at(10, 0)) == []


def test_empty_universe_enters_nothing():
    """set_liquid_universe を呼ばない（空）と、何もエントリーしない（安全側）。"""
    s = d.LiquidUnderSurgeStrategy()
    assert s.on_signal("under_surge_detector", under("5707", 1200.0), at(10, 0)) == []


def test_all_day_entry():
    """午前でもエントリーできる（午後限定でない）。"""
    s = make()
    out = s.on_signal("under_surge_detector", under("3692", 4800.0), at(9, 30))
    assert out and out[0]["type"] == "ENTRY"
    assert out[0]["trigger"] == "UNDER急増(流動)"


def test_only_under_surge_trigger():
    """UNDER急増以外のソースには反応しない。"""
    s = make()
    assert s.on_signal("panic_sell_detector", under("5707", 1200.0), at(10, 0)) == []
    assert s.on_signal("periodic_buy_zscore", under("5707", 1200.0), at(10, 0)) == []


def test_min_price_filter():
    """500円未満は対象外（既定 min_entry_price=500）。"""
    s = make()
    assert s.on_signal("under_surge_detector", under("9556", 480.0), at(10, 0)) == []


def test_one_entry_per_symbol_per_day():
    """同一銘柄は1日1回まで。"""
    s = make()
    assert s.on_signal("under_surge_detector", under("5707", 1200.0), at(10, 0))
    assert s.on_signal("under_surge_detector", under("5707", 1210.0), at(11, 0)) == []


def test_hold_to_close_when_no_take_profit():
    """take_profit_pct=None なら利確では出ず、大引け(15:30以降)まで持つ。"""
    s = make()
    s.on_signal("under_surge_detector", under("5707", 1000.0), at(10, 0))
    # +5%に上げても利確しない（利確なし設定）
    assert s.on_price("5707", 1050.0, at(13, 0)) == []
    # 大引けで決済
    out = s.on_price("5707", 1050.0, at(15, 30))
    assert out and out[0]["reason"] == "大引け"
    assert abs(out[0]["return_pct"] - 5.0) < 1e-6


def test_stop_loss_still_applies():
    """保護的な損切りは効く（既定 stop_loss_pct=3.0）。"""
    s = make()
    s.on_signal("under_surge_detector", under("3692", 1000.0), at(10, 0))
    out = s.on_price("3692", 969.0, at(10, 5))     # -3.1%
    assert out and out[0]["reason"] == "損切り"


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
