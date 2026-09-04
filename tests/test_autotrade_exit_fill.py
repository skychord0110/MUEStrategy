# -*- coding: utf-8 -*-
"""決済の損益を「注文価格」ではなく「実約定価格」で出すことを固定するテスト。

背景（2026-09-05に判明）:
  引け手仕舞いは「買い気配-3%の深い指値売り」を出し、実際は大引けの単一価格で
  約定する。ところが決済損益は last_order_price（＝深い指値）で記録していたため、
  実際より悪い損益（悲観側）がログに残っていた。エントリー建値は建玉照会から
  実約定を取っていたのに、決済だけ注文価格を使う非対称があった。

ここで固定する仕様:
  1. 約定明細（GET /orders の Details, RecType=8）から数量加重の平均約定単価を出す
  2. 決済確定は「注文状態」ではなく「建玉照会」を真実として突き合わせる
     - 建玉が残る     → 未決済とみなし、決済扱い（損益記録）をしない
     - 建玉ゼロ       → 実約定価格で損益を確定
     - 照会不能(None) → 決済確定を保留（記録しない）
     - 建玉ゼロだが約定価格が取れない → 注文価格で暫定記録（最終手段）

外部ライブラリもネットワークも使わない。

実行:
    python tests/test_autotrade_exit_fill.py
"""
import logging
import os
import sys
from datetime import datetime
from decimal import Decimal

BASE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.normpath(os.path.join(BASE, ".."))
# autotrade/src のみを通す（runner/src を通さないことで notifier 依存の約定音を無効化）
sys.path.insert(0, os.path.join(REPO, "strategies", "autotrade", "src"))

import executor as ex_mod            # noqa: E402
from trader import AutoTrader        # noqa: E402
from position_manager import (       # noqa: E402
    PositionManager, CLOSED, CLOSING_OUT)

LOG = logging.getLogger("test")
LOG.addHandler(logging.NullHandler())


def now():
    return datetime(2026, 9, 5, 15, 30, 0).astimezone()


# ── 1. 約定明細から平均約定単価 ─────────────────────────────────────
def test_avg_exec_price_weighted():
    """RecType=8(約定)だけを数量加重平均。約定以外の明細は無視する。"""
    order = {"Details": [
        {"RecType": 1, "Price": 999, "Qty": 200},   # 受付 → 無視
        {"RecType": 8, "Price": 580.0, "Qty": 100},  # 約定
        {"RecType": 8, "Price": 584.0, "Qty": 100},  # 約定
    ]}
    # (580*100 + 584*100)/200 = 582
    assert abs(ex_mod.avg_exec_price(order) - 582.0) < 1e-9


def test_avg_exec_price_none_when_no_exec():
    """約定明細が無ければ None（＝価格を確定できない）。"""
    assert ex_mod.avg_exec_price({"Details": [{"RecType": 1, "Price": 1, "Qty": 1}]}) is None
    assert ex_mod.avg_exec_price({}) is None


# ── テスト用のフェイク ──────────────────────────────────────────────
class FakeClient:
    def __init__(self, positions):
        self._positions = positions
        self.raise_on_positions = False

    def get_positions(self, product=None):
        if self.raise_on_positions:
            raise RuntimeError("照会失敗")
        return self._positions


class FakeAccount:
    def __init__(self, client):
        self.client = client


def make_trader(positions):
    tr = AutoTrader(config={}, executor=None,
                    account_view=FakeAccount(FakeClient(positions)), log=LOG)
    return tr


def make_pm():
    pm = PositionManager(symbol="4422", entry_price=587.0, qty=200,
                         config={}, log=LOG)
    pm.state = CLOSING_OUT
    pm.order_id = "OID1"
    pm.last_order_price = Decimal("577")   # 買い気配-3%の深い指値（悲観側）
    return pm


# ── 2. 建玉ゼロ → 実約定価格で損益確定 ──────────────────────────────
def test_settle_uses_actual_fill_price_when_flat():
    """建玉が消えていれば決済済み。損益は実約定価格(583)で出す（577ではない）。"""
    tr = make_trader(positions=[])                 # 建玉なし
    pm = make_pm()
    tr.positions = {"4422": pm}
    tr._settle_exit(pm, "OID1", filled=200, avg_price=583.0, product="1", now=now())
    assert pm.state == CLOSED
    # 実約定583で -0.68%。注文価格577(-1.70%)ではないこと
    assert abs(pm.pnl_pct() - (583.0 - 587.0) / 587.0 * 100) < 1e-9
    assert pm.pnl_pct() > -1.0            # 悲観側の-1.70%になっていない


# ── 3. 建玉が残る → 未決済扱い（損益を記録しない） ────────────────────
def test_settle_keeps_open_when_position_remains():
    """建玉が残っていれば決済扱いにしない。注文IDだけ外して手仕舞い継続。"""
    tr = make_trader(positions=[{"Symbol": "4422", "LeavesQty": 200}])
    pm = make_pm()
    tr.positions = {"4422": pm}
    tr._settle_exit(pm, "OID1", filled=0, avg_price=None, product="1", now=now())
    assert pm.state != CLOSED             # 決済していない
    assert pm.fills == []                 # phantomな損益を積まない
    assert pm.order_id is None            # 死んだ注文は外す（次サイクルで手仕舞い継続）


# ── 4. 照会不能 → 確定を保留（記録しない・状態を変えない） ────────────
def test_settle_defers_when_position_lookup_fails():
    """建玉照会に失敗したら決済確定を保留。憶測で埋めない。"""
    tr = make_trader(positions=[])
    tr.account.client.raise_on_positions = True
    pm = make_pm()
    tr.positions = {"4422": pm}
    tr._settle_exit(pm, "OID1", filled=200, avg_price=583.0, product="1", now=now())
    assert pm.state != CLOSED
    assert pm.fills == []
    assert pm.order_id == "OID1"          # 状態は据え置き（次ポールで再確認）


# ── 5. 建玉ゼロだが約定価格不明 → 注文価格で暫定記録（最終手段） ──────
def test_settle_falls_back_to_order_price_only_when_flat():
    """建玉ゼロで約定価格が取れないときだけ、注文価格で暫定的に確定する。"""
    tr = make_trader(positions=[])
    pm = make_pm()
    tr.positions = {"4422": pm}
    tr._settle_exit(pm, "OID1", filled=200, avg_price=None, product="1", now=now())
    assert pm.state == CLOSED
    assert abs(pm.pnl_pct() - (577.0 - 587.0) / 587.0 * 100) < 1e-9   # 577で暫定


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
