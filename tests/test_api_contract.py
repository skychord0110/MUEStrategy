# -*- coding: utf-8 -*-
"""kabuステーションAPIの定義値の取り違えを見張るテスト。

2026-08-18 の実発注失敗（銘柄4199）で2つの不具合が出た。どちらも
「APIは正しく教えてくれていたのに、こちら側が取り違えていた／捨てていた」
という性質のもので、放っておくと必ず再発する。

  1. /orders・/positions の product を取り違えていた
     現物は "1" なのに "2"（＝信用）を渡していたため、発注が500で失敗した
     あとの突き合わせが信用の注文を探し、現物の注文を見つけられなかった
  2. エラー応答の本文を捨てていた
     500でも本文に ErrorResponse {"Code","Message"} が返る仕様なのに、
     raise_for_status() でHTTPステータスしか残らず原因が追えなかった

外部ライブラリもネットワークも使わない。

実行:
    python tests/test_api_contract.py
"""
import os
import sys
from datetime import datetime, timedelta

BASE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.normpath(os.path.join(BASE, ".."))
for p in (os.path.join(REPO, "strategies", "runner", "src"),
          os.path.join(REPO, "strategies", "autotrade", "src")):
    if p not in sys.path:
        sys.path.insert(0, p)

import kabu_client as kc          # noqa: E402
import order_builder as ob        # noqa: E402
from executor import Executor     # noqa: E402


# ── 1. product の定義値 ──────────────────────────────────────────────
def test_product_values():
    """公式仕様: 0=すべて 1=現物 2=信用 3=先物 4=OP"""
    assert (kc.PRODUCT_ALL, kc.PRODUCT_CASH, kc.PRODUCT_MARGIN,
            kc.PRODUCT_FUTURE, kc.PRODUCT_OPTION) == ("0", "1", "2", "3", "4")
    # order_builder 側にも同じ定義があるので食い違っていないこと
    assert ob.PRODUCT_CASH == kc.PRODUCT_CASH
    assert ob.PRODUCT_MARGIN == kc.PRODUCT_MARGIN


def test_product_mapping():
    """CashMargin（1=現物）と product（1=現物）を取り違えないこと。"""
    assert ob.product_for(1) == "1", "現物は product=1。2は信用"
    assert ob.product_for(2) == "2"
    assert ob.product_for(3) == "2"
    assert ob.product_for(None) == "1"
    assert ob.product_for_config("cash") == "1"
    assert ob.product_for_config("margin") == "2"
    assert ob.product_for_config(None) == "1"


# ── 2. 発注失敗後の突き合わせ ────────────────────────────────────────
class FakeClient:
    """現物の注文を1件だけ持つ /orders。product で正しく絞る。"""

    def __init__(self, recv_time):
        self.asked = []
        self.row = {"ID": "20260818A01N12345678", "Symbol": "4199",
                    "Side": "2", "OrderQty": 100.0, "CumQty": 0.0,
                    "State": 3, "RecvTime": recv_time, "CashMargin": 1}

    def get_orders(self, product=None, symbol=None, order_id=None, state=None):
        self.asked.append(product)
        if product not in (None, "0", kc.PRODUCT_CASH):
            return []          # 信用や先物を指定されたら現物の注文は返らない
        if symbol and symbol != self.row["Symbol"]:
            return []
        return [self.row]


def _executor(client):
    ex = Executor({"enabled": True, "dry_run": False}, client=client)
    ex.log.disabled = True
    return ex


def test_reconcile_finds_cash_order():
    """500で失敗扱いになっても、実際に出ていた現物注文を拾えること。

    これが今回の本丸。product を "2" にしていた頃はここで None が返り、
    生きている注文を誰も管理しないまま放置していた。
    """
    sent_at = datetime.now().astimezone()
    client = FakeClient((sent_at + timedelta(seconds=1)).isoformat())
    order = {"Symbol": "4199", "Side": "2", "Qty": 100, "CashMargin": 1}

    found = _executor(client).reconcile(order, sent_at)

    assert found is not None, "現物の注文を見つけられていない（productの取り違え）"
    assert found["ID"] == "20260818A01N12345678"
    assert client.asked[0] == kc.PRODUCT_CASH, f"product={client.asked[0]} で照会している"


def test_reconcile_ignores_older_order():
    """送信より前からあった注文を、自分が出したものと誤認しないこと。"""
    sent_at = datetime.now().astimezone()
    client = FakeClient((sent_at - timedelta(minutes=5)).isoformat())
    order = {"Symbol": "4199", "Side": "2", "Qty": 100, "CashMargin": 1}
    assert _executor(client).reconcile(order, sent_at) is None


# ── 3. エラー応答の本文 ──────────────────────────────────────────────
class FakeResponse:
    def __init__(self, status, payload=None, text=""):
        self.status_code = status
        self.ok = 200 <= status < 300
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def test_error_body_is_kept():
    """500でも本文の Code / Message を例外に持たせること。"""
    resp = FakeResponse(500, {"Code": 4001001, "Message": "内部エラー"})
    try:
        kc.KabuClient._check(resp, "/sendorder")
    except kc.KabuApiError as e:
        assert e.status == 500
        assert e.code == 4001001
        assert e.message == "内部エラー"
        assert "4001001" in str(e) and "内部エラー" in str(e)
    else:
        raise AssertionError("例外が投げられていない")


def test_error_without_json_body():
    """本文がJSONでなくても、中身を落とさず例外にすること。"""
    resp = FakeResponse(502, None, text="<html>Bad Gateway</html>")
    try:
        kc.KabuClient._check(resp, "/orders")
    except kc.KabuApiError as e:
        assert e.code is None and "Bad Gateway" in str(e)
    else:
        raise AssertionError("例外が投げられていない")


def test_ok_response_passes():
    kc.KabuClient._check(FakeResponse(200, {"Result": 0}), "/sendorder")


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
