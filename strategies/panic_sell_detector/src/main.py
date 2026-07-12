"""OVER売り降ろし（投げ売り）検知ストラテジー（検知・通知のみ。発注は行わない）。

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
from datetime import datetime

import websocket
import yaml

from kabu_client import KabuClient
from detector import PanicSellDetector
import notifier


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_symbols(config: dict, config_path: str) -> list:
    """監視銘柄リストを取得する。

    config内の symbols: を優先（単一銘柄でのテスト用）。
    なければ symbols_file: の指す共通ファイル（configからの相対パス）を読む。
    """
    if config.get("symbols"):
        return config["symbols"]
    symbols_file = config.get("symbols_file")
    if not symbols_file:
        raise ValueError("config に symbols または symbols_file を指定してください")
    base = os.path.dirname(os.path.abspath(config_path))
    path = os.path.normpath(os.path.join(base, symbols_file))
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    symbols = (data or {}).get("symbols")
    if not symbols:
        raise ValueError(f"銘柄リストファイルに symbols がありません: {path}")
    return symbols


def main():
    parser = argparse.ArgumentParser(description="OVER売り降ろし（投げ売り）検知ストラテジー")
    parser.add_argument("--config", default="../config.yaml")
    args = parser.parse_args()

    config = load_config(args.config)
    notifier.setup_logging()
    log = logging.getLogger("panic_sell_detector")

    api_password = os.environ.get("KABU_API_PASSWORD")
    if not api_password:
        log.error("環境変数 KABU_API_PASSWORD が設定されていません")
        sys.exit(1)

    client = KabuClient(environment=config["environment"], api_password=api_password)
    client.authenticate()
    log.info("認証に成功しました（環境: %s, ポート: %s）", client.environment, client.port)

    symbols = load_symbols(config, args.config)
    log.info("監視銘柄: %d銘柄（%s から読み込み）", len(symbols),
             "config直接指定" if config.get("symbols") else config.get("symbols_file"))
    client.unregister_all()
    reg_result = client.register_symbols(symbols)
    log.info("銘柄登録結果: %s", reg_result)

    detector = PanicSellDetector(
        over_drop_threshold=config["over_drop_threshold"],
        match_tolerance=config.get("match_tolerance", 0.2),
        match_window_seconds=config.get("match_window_seconds", 30),
        low_zone_pct=config.get("low_zone_pct", 0.007),
        requote_levels=config.get("requote_levels", 3),
        requote_consumed_fraction=config.get("requote_consumed_fraction", 0.5),
        price_tolerance_ticks=config.get("price_tolerance_ticks", 0),
        large_over_threshold=config.get("large_over_threshold", 100000),
        large_over_drop_pct=config.get("large_over_drop_pct", 0.10),
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

        # 注意: kabuステーションAPIは BidPrice=最良「売」気配 / AskPrice=最良「買」気配 と
        # 一般的な英語の慣例と逆の命名のため、誤解の余地がないSell1〜10/Buy1を使う。
        sell_levels = []
        for i in range(1, 11):
            level = data.get(f"Sell{i}") or {}
            sell_levels.append((level.get("Price"), level.get("Qty")))
        buy1 = data.get("Buy1") or {}

        # 時間窓（match_window_seconds）の計測には受信時刻を使う。
        # 板だけの更新ではCurrentPriceTime等が古いままのことがあるため。
        msg_time = datetime.now().astimezone()

        alerts = detector.update(
            symbol=symbol,
            msg_time=msg_time,
            current_price=data.get("CurrentPrice"),
            low_price=data.get("LowPrice"),
            over_sell_qty=data.get("OverSellQty"),
            sell_levels=sell_levels,
            buy1_price=buy1.get("Price"),
            sell1_price=sell_levels[0][0],
            trading_volume=data.get("TradingVolume"),
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
