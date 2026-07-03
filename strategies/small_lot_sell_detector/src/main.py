"""小口売り連続検知ストラテジー（検知・通知のみ。発注は行わない）。

起動前提:
  - kabuステーション（デスクトップアプリ）を起動し、config.yamlのenvironmentに
    対応する環境（検証/本番）にログインしておく
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
from datetime import datetime

import websocket
import yaml

from kabu_client import KabuClient
from detector import SmallLotSellDetector
import notifier


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def main():
    parser = argparse.ArgumentParser(description="小口売り連続検知ストラテジー")
    parser.add_argument("--config", default="../config.yaml")
    args = parser.parse_args()

    config = load_config(args.config)
    notifier.setup_logging()
    log = logging.getLogger("small_lot_detector")

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

    detector = SmallLotSellDetector(
        small_lot_threshold=config["small_lot_threshold"],
        price_tolerance_ticks=config.get("price_tolerance_ticks", 0),
        alert_tiers=config["alert_tiers"],
        min_hit_interval_seconds=config.get("min_hit_interval_seconds", 1.0),
    )

    debug_remaining = [config.get("debug_raw_messages", 0)]

    def on_message(ws, message):
        data = json.loads(message)

        if debug_remaining[0] > 0:
            log.info("[DEBUG RAW] %s", data)
            debug_remaining[0] -= 1

        symbol = data.get("Symbol")
        current_price = data.get("CurrentPrice")
        trading_volume = data.get("TradingVolume")
        # 最良買い気配はBuy1.Priceを使う。
        # 注意: kabuステーションAPIは BidPrice=最良「売」気配 / AskPrice=最良「買」気配 と
        # 一般的な英語の慣例と逆の命名のため、誤解の余地がないBuy1を採用している。
        buy1 = data.get("Buy1") or {}
        buy_price = buy1.get("Price")
        if buy_price is None:
            buy_price = data.get("AskPrice")  # AskPrice=最良買気配（公式リファレンス準拠）

        if symbol is None or trading_volume is None:
            return

        # 約定時刻。アルゴ分散売りのバースト判定に使う。
        # TradingVolumeTime（売買高時刻）を優先し、なければCurrentPriceTime、
        # どちらもなければ受信時刻で代用する。
        time_str = data.get("TradingVolumeTime") or data.get("CurrentPriceTime")
        if time_str:
            try:
                trade_time = datetime.fromisoformat(time_str)
            except ValueError:
                trade_time = datetime.now().astimezone()
        else:
            trade_time = datetime.now().astimezone()

        alerts = detector.update(symbol, current_price, trading_volume, buy_price, trade_time)
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
