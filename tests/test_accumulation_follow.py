# -*- coding: utf-8 -*-
"""AI買い集め追随（accumulation_follow）の挙動を固定するテスト。

この戦略は分析で決めた条件がそのまま仕様になっている。
数字を後から安易に変えられないよう、根拠のある条件だけを検証する。
  ・入力は periodic_buy_zscore の STRONG のみ（WATCHは期待値マイナス）
  ・午前のみ（午後は検証で機能しなかった）
  ・利確を置かない（置くと期待値がほぼ半減した）
  ・損切り -2% と大引けでは決済する

外部ライブラリもネットワークも使わない。

実行:
    python tests/test_accumulation_follow.py
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


def at(h, m, s=0):
    return datetime(2026, 8, 21, h, m, s).astimezone()


def strat(**kw):
    kw.setdefault("stop_loss_pct", 2.0)
    return D.AccumulationFollowStrategy(**kw)


def sig(symbol="4013", tier="STRONG", zscore=11.0, price=1000.0):
    return {"symbol": symbol, "tier": tier, "zscore": zscore,
            "pairs": 40, "price": price}


def entries(out):
    return [a for a in out if a["type"] == "ENTRY"]


# ── 入力の選別 ────────────────────────────────────────────────────
def test_strong_in_morning_enters():
    s = strat()
    out = s.on_signal("periodic_buy_zscore", sig(), at(9, 30))
    assert len(entries(out)) == 1
    e = entries(out)[0]
    assert e["price"] == 1000.0 and e["trigger"] == "定期買い集め"


def test_watch_is_ignored():
    """WATCHは検証で期待値-0.38%だったため入力にしない。"""
    s = strat()
    assert s.on_signal("periodic_buy_zscore", sig(tier="WATCH"), at(9, 30)) == []


def test_other_sources_ignored():
    s = strat()
    assert s.on_signal("under_surge_detector", sig(), at(9, 30)) == []
    assert s.on_signal("panic_sell_detector", sig(), at(9, 30)) == []


def test_afternoon_ignored():
    """午後は n=4 期待値-0.19% で機能しなかったため既定では取らない。"""
    s = strat()
    assert s.on_signal("periodic_buy_zscore", sig(), at(13, 30)) == []


def test_price_floor():
    s = strat(min_entry_price=500.0)
    assert s.on_signal("periodic_buy_zscore", sig(price=480.0), at(9, 30)) == []


def test_price_missing_is_skipped():
    """歩み値ベースの検知は価格を持たない。補えなければ見送る。"""
    s = strat()
    assert s.on_signal("periodic_buy_zscore", sig(price=None), at(9, 30)) == []


def test_one_entry_per_symbol_per_day():
    s = strat()
    assert len(entries(s.on_signal("periodic_buy_zscore", sig(), at(9, 30)))) == 1
    assert s.on_signal("periodic_buy_zscore", sig(), at(9, 45)) == []


def test_min_zscore_filter():
    s = strat(min_zscore=12.0)
    assert s.on_signal("periodic_buy_zscore", sig(zscore=11.0), at(9, 30)) == []
    assert len(entries(s.on_signal("periodic_buy_zscore",
                                   sig(zscore=12.5), at(9, 30)))) == 1


# ── 決済 ──────────────────────────────────────────────────────────
def test_no_take_profit():
    """+2%どころか+10%でも利確しない。伸ばしきるのがこの戦略の要点。"""
    s = strat()
    s.on_signal("periodic_buy_zscore", sig(price=1000.0), at(9, 30))
    assert s.on_price("4013", 1100.0, at(10, 0)) == []
    assert s.on_price("4013", 1200.0, at(11, 0)) == []


def test_stop_loss_fires():
    s = strat()
    s.on_signal("periodic_buy_zscore", sig(price=1000.0), at(9, 30))
    out = s.on_price("4013", 980.0, at(10, 0))
    assert len(out) == 1 and out[0]["reason"] == "損切り"
    assert abs(out[0]["return_pct"] - (-2.0)) < 1e-9


def test_close_exit_takes_the_gain():
    s = strat()
    s.on_signal("periodic_buy_zscore", sig(price=1000.0), at(9, 30))
    assert s.on_price("4013", 1080.0, at(14, 0)) == []      # まだ持つ
    out = s.on_price("4013", 1080.0, at(15, 30))
    assert len(out) == 1 and out[0]["reason"] == "大引け"
    assert abs(out[0]["return_pct"] - 8.0) < 1e-9


def test_take_profit_still_works_if_configured():
    """利確を明示すれば従来どおり効く（既定が無効なだけ）。"""
    s = strat(take_profit_pct=2.0)
    s.on_signal("periodic_buy_zscore", sig(price=1000.0), at(9, 30))
    out = s.on_price("4013", 1020.0, at(10, 0))
    assert len(out) == 1 and out[0]["reason"] == "利確"


# ── 通知の文言 ────────────────────────────────────────────────────
def test_notification_says_no_take_profit():
    s = strat()
    e = entries(s.on_signal("periodic_buy_zscore", sig(), at(9, 30)))[0]
    title, body = notifier.build_message("accumulation_follow", e)
    assert "AI買い集め追随/エントリー" in title
    assert "利確なし" in body and "損切り-2.0%" in body
    assert "定期買い集め を検知" in body


def test_notification_exit():
    s = strat()
    s.on_signal("periodic_buy_zscore", sig(price=1000.0), at(9, 30))
    x = s.on_price("4013", 980.0, at(10, 0))[0]
    title, body = notifier.build_message("accumulation_follow", x)
    assert "決済:損切り" in title and "-2.00%" in body


# ── ランナーの受け渡し ────────────────────────────────────────────
def test_engine_bridges_external_alert():
    """別スレッドの検知が、PUSH処理の中で価格を補われて配られること。"""
    import main as runner
    eng = runner.RunnerEngine({"strategies": {
        "accumulation_follow": {"enabled": True, "entry_start": "09:00",
                                "entry_end": "11:00", "stop_loss_pct": 2.0,
                                "min_entry_price": 500.0}}})
    assert "accumulation_follow" in eng.ai_strategies

    now = at(9, 30)
    # 先に板のPUSHが来て現在値が分かる
    eng.handle({"Symbol": "4013", "CurrentPrice": 1000.0}, now)
    # 別スレッドの検知（価格を持たない）
    eng.submit_external("periodic_buy_zscore",
                        {"symbol": "4013", "tier": "STRONG", "zscore": 11.0})
    # 別銘柄のPUSHでも引き取って配られる
    out = eng.handle({"Symbol": "9999", "CurrentPrice": 300.0}, at(9, 31))
    ent = [a for n, a in out
           if n == "accumulation_follow" and a["type"] == "ENTRY"]
    assert len(ent) == 1, out
    assert ent[0]["price"] == 1000.0 and ent[0]["symbol"] == "4013"


def test_engine_skips_when_price_unknown():
    """一度もPUSHが来ていない銘柄は価格が補えないのでエントリーしない。"""
    import main as runner
    eng = runner.RunnerEngine({"strategies": {
        "accumulation_follow": {"enabled": True, "min_entry_price": 500.0}}})
    eng.submit_external("periodic_buy_zscore",
                        {"symbol": "7777", "tier": "STRONG", "zscore": 11.0})
    out = eng.handle({"Symbol": "4013", "CurrentPrice": 1000.0}, at(9, 30))
    assert not [a for n, a in out if n == "accumulation_follow"]


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
