# -*- coding: utf-8 -*-
"""VWAP乖離反発（vwap_discount_reversal）とVWAP計算のテスト。

この戦略は検証で決めた条件がそのまま仕様になっている。数字を後から
安易に動かせないよう、根拠のある条件だけを固定する。
  ・土台は午後(13-15時)のUNDER急増（既存の afternoon_reversal と同じ）
  ・そこに「当日VWAPを1%以上下回る」を追加で要求する
  ・VWAPが取れないうちは**見送る**（無条件に入らない）

外部ライブラリもネットワークも使わない。

実行:
    python tests/test_vwap_discount.py
"""
import os
import sys
from datetime import datetime, time as dtime

BASE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.normpath(os.path.join(BASE, ".."))
for p in (os.path.join(REPO, "strategies", "AIStrategys", "src"),
          os.path.join(REPO, "strategies", "runner", "src")):
    if p not in sys.path:
        sys.path.insert(0, p)

import detector as D            # noqa: E402
import notifier                 # noqa: E402
from vwap import VwapTracker    # noqa: E402


def at(h, m, s=0, day=28):
    return datetime(2026, 8, day, h, m, s).astimezone()


# ── VWAPの積み上げ ──────────────────────────────────────────────
def test_vwap_weighted_by_volume():
    """出来高の多い価格に寄ること（単純平均ではない）。"""
    t = VwapTracker()
    t.update("4199", 100.0, 100, at(9, 0))      # 100円×100株
    t.update("4199", 200.0, 1100, at(9, 5))     # 200円×1000株
    # (100*100 + 200*1000) / 1100 = 190.9...
    assert abs(t.get("4199") - (100 * 100 + 200 * 1000) / 1100) < 1e-9


def test_vwap_uses_volume_delta_not_cumulative():
    """TradingVolumeは当日累計。差分を使わないと価格が二重に効いてしまう。"""
    t = VwapTracker()
    t.update("4199", 100.0, 1000, at(9, 0))
    t.update("4199", 100.0, 1000, at(9, 1))     # 出来高が増えていない＝約定なし
    assert abs(t.get("4199") - 100.0) < 1e-9


def test_vwap_resets_next_day():
    t = VwapTracker()
    t.update("4199", 100.0, 1000, at(9, 0, day=27))
    t.update("4199", 300.0, 1000, at(9, 0, day=28))
    assert abs(t.get("4199", at(9, 0, day=28)) - 300.0) < 1e-9


def test_vwap_handles_volume_going_backwards():
    """再接続などで累計が巻き戻っても壊れないこと。"""
    t = VwapTracker()
    t.update("4199", 100.0, 5000, at(9, 0))
    t.update("4199", 120.0, 100, at(9, 5))      # 巻き戻り
    assert t.get("4199") is not None


def test_vwap_none_until_computable():
    t = VwapTracker()
    assert t.get("4199") is None
    t.update("4199", None, 100, at(9, 0))
    t.update("4199", 100.0, None, at(9, 0))
    assert t.get("4199") is None


def test_discount_pct():
    t = VwapTracker()
    t.update("4199", 1000.0, 1000, at(9, 0))
    assert abs(t.discount_pct("4199", 990.0) - (-1.0)) < 1e-9
    assert abs(t.discount_pct("4199", 1010.0) - (+1.0)) < 1e-9
    assert t.discount_pct("9999", 100.0) is None


# ── 戦略 ────────────────────────────────────────────────────────
def strat(**kw):
    return D.VwapDiscountReversalStrategy(**kw)


def sig(price=1000.0, disc=-1.5, symbol="4199"):
    return {"symbol": symbol, "price": price, "vwap_discount_pct": disc}


def entries(out):
    return [a for a in out if a["type"] == "ENTRY"]


def test_enters_when_below_vwap():
    s = strat()
    out = s.on_signal("under_surge_detector", sig(disc=-1.5), at(13, 30))
    assert len(entries(out)) == 1
    e = entries(out)[0]
    assert e["trigger"] == "UNDER急増+VWAP乖離"
    assert e["vwap_discount_pct"] == -1.5


def test_skips_when_not_far_enough_below_vwap():
    """-1%ちょうどは通し、-0.9%は見送る（境界の向きを固定する）。"""
    s = strat(min_discount_pct=1.0)
    assert s.on_signal("under_surge_detector", sig(disc=-0.9), at(13, 30)) == []
    assert len(entries(s.on_signal("under_surge_detector",
                                   sig(disc=-1.0), at(13, 30)))) == 1


def test_skips_above_vwap():
    s = strat()
    assert s.on_signal("under_surge_detector", sig(disc=+0.5), at(13, 30)) == []


def test_skips_when_vwap_unavailable():
    """寄り直後などVWAPが無いときは**入らない**。無条件に入ると土台と同じになる。"""
    s = strat()
    a = sig()
    a.pop("vwap_discount_pct")
    assert s.on_signal("under_surge_detector", a, at(13, 30)) == []
    assert s.on_signal("under_surge_detector",
                       {**sig(), "vwap_discount_pct": None}, at(13, 30)) == []


def test_morning_ignored():
    """土台と同じく午後のみ。午前は既存分析で優位性が無い。"""
    s = strat()
    assert s.on_signal("under_surge_detector", sig(), at(11, 0)) == []


def test_other_sources_ignored():
    s = strat()
    assert s.on_signal("panic_sell_detector", sig(), at(13, 30)) == []


def test_price_floor():
    s = strat(min_entry_price=500.0)
    assert s.on_signal("under_surge_detector",
                       sig(price=480.0), at(13, 30)) == []


def test_one_entry_per_symbol_per_day():
    s = strat()
    assert len(entries(s.on_signal("under_surge_detector", sig(), at(13, 30)))) == 1
    assert s.on_signal("under_surge_detector", sig(), at(13, 45)) == []


def test_exits_follow_the_base_rules():
    s = strat(stop_loss_pct=2.0, take_profit_pct=2.0)
    s.on_signal("under_surge_detector", sig(price=1000.0), at(13, 30))
    assert s.on_price("4199", 1010.0, at(14, 0)) == []
    out = s.on_price("4199", 1020.0, at(14, 30))
    assert len(out) == 1 and out[0]["reason"] == "利確"


def test_stop_loss():
    s = strat()
    s.on_signal("under_surge_detector", sig(price=1000.0), at(13, 30))
    out = s.on_price("4199", 980.0, at(14, 0))
    assert len(out) == 1 and out[0]["reason"] == "損切り"


def test_notification():
    s = strat()
    e = entries(s.on_signal("under_surge_detector", sig(), at(13, 30)))[0]
    title, body = notifier.build_message("vwap_discount_reversal", e)
    assert "AI VWAP乖離反発/エントリー" in title
    assert "損切り-2.0%" in body and "利確+2.0%" in body


# ── ランナーの配線 ──────────────────────────────────────────────
def test_engine_attaches_vwap_to_alerts():
    """PUSHから積み上げたVWAPが、アラートに載って戦略へ渡ること。"""
    import main as runner
    eng = runner.RunnerEngine({"strategies": {
        "under_surge_detector": {"enabled": True},
        "vwap_discount_reversal": {"enabled": True, "min_discount_pct": 1.0},
    }})
    assert "vwap_discount_reversal" in eng.ai_strategies

    # 高い値段で出来高を作ってVWAPを押し上げる
    for k in range(5):
        eng.handle({"Symbol": "4199", "CurrentPrice": 1000.0,
                    "TradingVolume": 1000 * (k + 1)}, at(13, 0, k))
    v = eng.vwap.get("4199", at(13, 1))
    assert v is not None and abs(v - 1000.0) < 1e-6
    # 現在値が980円ならVWAPから-2%
    d = eng.vwap.discount_pct("4199", 980.0, at(13, 1))
    assert abs(d - (-2.0)) < 1e-6


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
