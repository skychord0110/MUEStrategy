# -*- coding: utf-8 -*-
"""スマホアプリ向けの書き出し（イベントJSONL・注文一覧）を固定するテスト。

アプリ側（MUEStrategyApp）はこの形を前提に取り込むので、キー名を変えたら
アプリのパーサも直すこと。

外部ライブラリもネットワークも使わない。

実行:
    python tests/test_mobile_events.py
"""
import json
import os
import sys
import tempfile
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.normpath(os.path.join(BASE, ".."))
p = os.path.join(REPO, "strategies", "runner", "src")
if p not in sys.path:
    sys.path.insert(0, p)

import account_snapshot as snap   # noqa: E402
import events                     # noqa: E402


def test_paper_events_are_appended_as_json_lines():
    with tempfile.TemporaryDirectory() as d:
        events.EVENTS_DIR = d
        t1 = datetime(2026, 10, 23, 9, 9, 20)
        t2 = datetime(2026, 10, 23, 9, 12, 8)
        events.record_paper("panic_rebound", {
            "type": "ENTRY", "symbol": "4579", "price": 590.0, "time": t1,
            "stop_loss_pct": 1.0, "take_profit_pct": 2.0, "trigger": "panic_sell_detector",
        }, now=t1)
        events.record_paper("panic_rebound", {
            "type": "EXIT", "symbol": "4579", "reason": "利確", "entry_price": 590.0,
            "exit_price": 606.0, "return_pct": 2.71, "time": t2,
        }, now=t2)
        with open(os.path.join(d, "events_2026-10-23.jsonl"), encoding="utf-8") as f:
            rows = [json.loads(line) for line in f]
    assert [r["type"] for r in rows] == ["ENTRY", "EXIT"]
    assert rows[0]["v"] == events.SCHEMA_VERSION
    assert rows[0]["kind"] == "paper"
    assert rows[0]["strategy"] == "panic_rebound"
    assert rows[0]["time"] == "2026-10-23T09:09:20"
    assert rows[1]["reason"] == "利確"
    assert rows[1]["exit_price"] == 606.0


def test_write_failure_does_not_raise():
    with tempfile.TemporaryDirectory() as d:
        blocker = os.path.join(d, "file")
        open(blocker, "w").close()
        events.EVENTS_DIR = os.path.join(blocker, "sub")   # ファイルの下にはフォルダを作れない
        events.record_paper("panic_rebound", {"type": "ENTRY", "symbol": "1"})


def test_order_rows():
    rows = snap.order_rows([
        {"ID": "A1", "Symbol": "5590", "SymbolName": "x", "Side": "2", "State": 5,
         "OrderQty": 200, "CumQty": 200, "Price": 703, "RecvTime": "2026-08-31T14:29:20"},
        {"ID": "A2", "Symbol": "5590", "Side": "1", "State": 1,
         "OrderQty": 200, "CumQty": 0, "Price": 0},
    ])
    assert rows[0] == {"id": "A1", "symbol": "5590", "name": "x", "side": "2", "state": 5,
                       "qty": 200.0, "filled": 200.0, "price": 703.0,
                       "recv_time": "2026-08-31T14:29:20"}
    assert rows[1]["price"] is None     # 成行
    assert snap.order_rows(None) == []


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
