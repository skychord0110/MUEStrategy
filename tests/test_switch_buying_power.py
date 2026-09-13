# -*- coding: utf-8 -*-
"""乗り換え時の「余力タイムラグ」対策を固定するテスト。

背景（利用者の実機経験）:
  未約定の指値A（別銘柄）を出したまま、すぐ約定できる銘柄Bのシグナルが出た。
  Aを取り消してBに乗り換えようとしたが、kabuの /wallet/cash は取消の解放が
  非同期で間に合わず、Aの資金が拘束されたままの古い余力を返した。その結果
  「1単元も買えない」と誤判定され、すぐ約定できたBを取りこぼした（機会損失）。

ここで固定する仕様:
  1. sizing.calc_quantity は extra_buying_power を買付余力に足し戻して数量を出す
  2. 取消で解放される見込みの額（数量×指値）を足し戻せば、余力反映を待たずに
     乗り換え先の数量を正しく計算できる
  3. 設定の使用上限（max_use_amount 等）は足し戻しの影響を受けず、そのまま効く
     （増えるのは余力制約だけ。実発注はkabu側でも実資金と照合される）

外部ライブラリもネットワークも使わない。

実行:
    python tests/test_switch_buying_power.py
"""
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.normpath(os.path.join(BASE, ".."))
sys.path.insert(0, os.path.join(REPO, "strategies", "autotrade", "src"))

import sizing  # noqa: E402

# 使用上限は余裕をもたせ、余力だけが効くようにした設定
CAP = {"max_use_amount": 1_000_000, "max_amount_per_symbol": 1_000_000,
       "max_positions": 1, "min_free_margin": 20_000, "lot_size": 100}


def test_lag_blocks_without_credit():
    """足し戻さないと、取消直後の古い余力（拘束されたまま）では買えないことを再現。

    余力7万円・株価818円（1単元=81,800円）→ 1単元も買えない。これが機会損失の姿。
    """
    r = sizing.calc_quantity(818.0, 70_000, CAP, trading_unit=100, open_positions=0)
    assert not r.ok
    assert "1単元も買えない" in r.reason


def test_credit_restores_switch_quantity():
    """取消で解放される見込みの額を足し戻せば、乗り換え先を1単元買える。

    Aの拘束 81,800円（100株×818円）を足し戻す → 実効余力 151,800円 → B(818円)を
    1単元(81,800円)ぶん計算できる。
    """
    freed = 100 * 818.0
    r = sizing.calc_quantity(818.0, 70_000, CAP, trading_unit=100,
                             open_positions=0, extra_buying_power=freed)
    assert r.ok, r.reason
    assert r.quantity == 100
    assert r.limited_by == "買付余力"       # 足し戻し後の余力が上限を決めている


def test_credit_does_not_bypass_config_cap():
    """足し戻しても設定の使用上限は超えない（増えるのは余力制約だけ）。"""
    cap = dict(CAP, max_use_amount=90_000, max_amount_per_symbol=90_000)
    r = sizing.calc_quantity(818.0, 70_000, cap, trading_unit=100,
                             open_positions=0, extra_buying_power=1_000_000)
    assert r.ok, r.reason
    assert r.quantity == 100                # 90,000円上限 → 1単元まで
    assert r.limited_by in ("設定の使用上限", "1銘柄あたり上限")


def test_credit_respects_min_free_after_addback():
    """足し戻し後の実効余力で min_free 判定する（解放見込みを含めて下限を満たすなら可）。"""
    # API余力1万円は下限2万円未満だが、取消解放10万円を足すと満たす
    r = sizing.calc_quantity(818.0, 10_000, CAP, trading_unit=100,
                             open_positions=0, extra_buying_power=100_000)
    assert r.ok, r.reason
    assert r.quantity == 100


def test_zero_credit_is_default_unchanged():
    """extra_buying_power 既定0のときは従来と同じ挙動（回帰防止）。"""
    r = sizing.calc_quantity(818.0, 200_000, CAP, trading_unit=100, open_positions=0)
    assert r.ok
    assert r.quantity == 200                # 200,000 // 81,800 = 2単元


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
