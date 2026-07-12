"""UNDER急増（下値への大口買い出現）検知ストラテジー（検知・通知のみ。発注は行わない）。

起動前提:
  - kabuステーション（デスクトップアプリ）を起動し、ログインしておく
  - 環境変数 KABU_API_PASSWORD にkabuステーションのAPIパスワードを設定しておく
  - config.yaml に監視銘柄・閾値を設定する

詳細は ../README.md を参照。
"""
import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, time as dtime

import websocket
import yaml

from kabu_client import KabuClient
from detector import UnderSurgeDetector
import notifier


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def main():
    parser = argparse.ArgumentParser(description="UNDER急増（下値への大口買い出現）検知ストラテジー")
    parser.add_argument("--config", default="../config.yaml")
    args = parser.parse_args()

    config = load_config(args.config)
    notifier.setup_logging()
    log = logging.getLogger("under_surge_detector")

    api_password = os.environ.get("KABU_API_PASSWORD")
    if not api_password:
        log.error("環境変数 KABU_API_PASSWORD が設定されていません")
        sys.exit(1)

    client = KabuClient(environment=config["environment"], api_password=api_password)
    client.authenticate()
    log.info("認証に成功しました（環境: %s, ポート: %s）", client.environment, client.port)

    client.unregister_all()
    reg_result = client.register_symbols(config["symbols"])
    log.info("銘柄登録結果: %s", reg_result)

    # 板寄せ直後の通知抑制時間帯（config形式: ["09:00-09:01", "12:30-12:31"]）
    quiet_windows = None
    if "quiet_windows" in config:
        quiet_windows = []
        for w in config["quiet_windows"]:
            start_s, end_s = w.split("-")
            h1, m1 = map(int, start_s.split(":"))
            h2, m2 = map(int, end_s.split(":"))
            quiet_windows.append((dtime(h1, m1), dtime(h2, m2)))

    detector = UnderSurgeDetector(
        under_increase_pct=config.get("under_increase_pct", 0.20),
        over_change_tolerance_pct=config.get("over_change_tolerance_pct", 0.05),
        low_zone_pct=config.get("low_zone_pct", 0.01),
        cooldown_seconds=config.get("cooldown_seconds", 60),
        quiet_windows=quiet_windows,
    )

    debug_remaining = [config.get("debug_raw_messages", 0)]

    def on_message(ws, message):
        data = json.loads(message)

        if debug_remaining[0] > 0:
            log.info("[DEBUG RAW] %s", data)
            debug_remaining[0] -= 1

        symbol = data.get("Symbol")
        if symbol is None:
            return

        alerts = detector.update(
            symbol=symbol,
            msg_time=datetime.now().astimezone(),
            current_price=data.get("CurrentPrice"),
            low_price=data.get("LowPrice"),
            under_buy_qty=data.get("UnderBuyQty"),
            over_sell_qty=data.get("OverSellQty"),
        )
        for alert in alerts:
            notifier.notify(alert)

    def on_error(ws, error):
        log.error("WebSocketエラー: %s", error)

    def on_close(ws, code, msg):
        log.warning("WebSocket切断 (code=%s, msg=%s)", code, msg)

    def on_open(ws):
        log.info("WebSocket接続確立。PUSH配信の受信を開始します。")

    while True:
        ws = websocket.WebSocketApp(
            client.ws_url,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close,
            on_open=on_open,
        )
        ws.run_forever()
        log.warning("WebSocketが切断されました。5秒後に再接続します。")
        time.sleep(5)


if __name__ == "__main__":
    main()
