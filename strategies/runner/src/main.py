"""統合ランナー: 全ストラテジーを1プロセス・1WebSocket接続でまとめて実行する。

kabuステーションへの接続・認証・銘柄登録を1回だけ行い、受信した各PUSHメッセージを
有効化された全ストラテジーの検知エンジン（各strategies/*/src/detector.py）に配る。
検知・通知のみで発注は行わない。

起動前提:
  - kabuステーション（デスクトップアプリ）を起動し、ログインしておく
  - 環境変数 KABU_API_PASSWORD にkabuステーションのAPIパスワードを設定しておく
  - config.yaml で有効にするストラテジーと閾値を設定する
  - 監視銘柄は ../symbols.yaml（全ストラテジー共通）で管理する

詳細は ../README.md を参照。
"""
import argparse
import importlib.util
import json
import logging
import os
import sys
import time
from datetime import datetime, time as dtime

import websocket
import yaml

from kabu_client import KabuClient
import notifier

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STRATEGIES_ROOT = os.path.normpath(os.path.join(BASE_DIR, "..", ".."))


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_symbols(config: dict, config_path: str) -> list:
    """監視銘柄リストを取得する。config内のsymbolsが優先、なければsymbols_fileの共通ファイルを読む。"""
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


def load_detector_module(strategy_name: str):
    """各ストラテジーのdetector.pyを、モジュール名の衝突なしに読み込む。"""
    path = os.path.join(STRATEGIES_ROOT, strategy_name, "src", "detector.py")
    spec = importlib.util.spec_from_file_location(f"{strategy_name}__detector", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def parse_quiet_windows(windows: list) -> list:
    result = []
    for w in windows:
        start_s, end_s = w.split("-")
        h1, m1 = map(int, start_s.split(":"))
        h2, m2 = map(int, end_s.split(":"))
        result.append((dtime(h1, m1), dtime(h2, m2)))
    return result


def parse_time(s: str) -> dtime:
    h, m = map(int, s.split(":"))
    return dtime(h, m)


class RunnerEngine:
    """PUSHメッセージを有効な全detectorに配るディスパッチャ。"""

    def __init__(self, config: dict):
        self.detectors = {}
        strategies_cfg = config.get("strategies", {})

        sl = strategies_cfg.get("small_lot_sell_detector", {})
        if sl.get("enabled"):
            mod = load_detector_module("small_lot_sell_detector")
            self.detectors["small_lot_sell_detector"] = mod.SmallLotSellDetector(
                small_lot_threshold=sl["small_lot_threshold"],
                price_tolerance_ticks=sl.get("price_tolerance_ticks", 0),
                alert_tiers=sl["alert_tiers"],
                min_hit_interval_seconds=sl.get("min_hit_interval_seconds", 1.0),
            )

        ps = strategies_cfg.get("panic_sell_detector", {})
        if ps.get("enabled"):
            mod = load_detector_module("panic_sell_detector")
            self.detectors["panic_sell_detector"] = mod.PanicSellDetector(
                over_drop_threshold=ps["over_drop_threshold"],
                match_tolerance=ps.get("match_tolerance", 0.2),
                match_window_seconds=ps.get("match_window_seconds", 30),
                low_zone_pct=ps.get("low_zone_pct", 0.007),
                requote_levels=ps.get("requote_levels", 3),
                requote_consumed_fraction=ps.get("requote_consumed_fraction", 0.5),
                price_tolerance_ticks=ps.get("price_tolerance_ticks", 0),
                large_over_threshold=ps.get("large_over_threshold", 100000),
                large_over_drop_pct=ps.get("large_over_drop_pct", 0.10),
            )

        us = strategies_cfg.get("under_surge_detector", {})
        if us.get("enabled"):
            mod = load_detector_module("under_surge_detector")
            quiet = parse_quiet_windows(us["quiet_windows"]) if "quiet_windows" in us else None
            self.detectors["under_surge_detector"] = mod.UnderSurgeDetector(
                under_increase_pct=us.get("under_increase_pct", 0.20),
                over_change_tolerance_pct=us.get("over_change_tolerance_pct", 0.05),
                low_zone_pct=us.get("low_zone_pct", 0.01),
                cooldown_seconds=us.get("cooldown_seconds", 60),
                quiet_windows=quiet,
            )

        # AIストラテジー（strategies/AIStrategys/）: 基礎ストラテジーの検知アラートを
        # 入力にした仮想売買。発注はしない。詳細は ../../AIStrategys/README.md
        self.ai_strategies = {}
        ar = strategies_cfg.get("afternoon_reversal", {})
        cf = strategies_cfg.get("confluence", {})
        if ar.get("enabled") or cf.get("enabled"):
            ai_mod = load_detector_module("AIStrategys")
            if ar.get("enabled"):
                self.ai_strategies["afternoon_reversal"] = ai_mod.AfternoonReversalStrategy(
                    entry_start=parse_time(ar.get("entry_start", "13:00")),
                    entry_end=parse_time(ar.get("entry_end", "15:00")),
                    stop_loss_pct=ar.get("stop_loss_pct", 2.0),
                    take_profit_pct=ar.get("take_profit_pct", 2.0),
                    min_entry_price=ar.get("min_entry_price", 500.0),
                )
            if cf.get("enabled"):
                self.ai_strategies["confluence"] = ai_mod.ConfluenceStrategy(
                    window_seconds=cf.get("window_seconds", 1800),
                    entry_start=parse_time(cf.get("entry_start", "13:00")),
                    entry_end=parse_time(cf.get("entry_end", "15:00")),
                    stop_loss_pct=cf.get("stop_loss_pct", 1.0),
                    take_profit_pct=cf.get("take_profit_pct", 1.0),
                )

    def handle(self, data: dict, now=None) -> list:
        """1件のPUSHメッセージを処理し、[(ストラテジー名, alert), ...] を返す。"""
        results = []
        symbol = data.get("Symbol")
        if symbol is None:
            return results
        if now is None:
            now = datetime.now().astimezone()

        current_price = data.get("CurrentPrice")
        trading_volume = data.get("TradingVolume")
        low_price = data.get("LowPrice")

        # 注意: kabuステーションAPIは BidPrice=最良「売」気配 / AskPrice=最良「買」気配 と
        # 一般的な英語の慣例と逆の命名のため、誤解の余地がないBuy1/Sell1〜10を使う。
        buy1 = data.get("Buy1") or {}
        buy1_price = buy1.get("Price")
        if buy1_price is None:
            buy1_price = data.get("AskPrice")  # AskPrice=最良買気配（公式リファレンス準拠）
        sell_levels = []
        for i in range(1, 11):
            level = data.get(f"Sell{i}") or {}
            sell_levels.append((level.get("Price"), level.get("Qty")))

        # 約定時刻の近似: 出来高更新時刻 → 現在値更新時刻 → 受信時刻 の順で採用。
        time_str = data.get("TradingVolumeTime") or data.get("CurrentPriceTime")
        trade_time = now
        if time_str:
            try:
                trade_time = datetime.fromisoformat(time_str)
            except ValueError:
                pass

        d = self.detectors.get("small_lot_sell_detector")
        if d is not None and trading_volume is not None:
            for alert in d.update(symbol, current_price, trading_volume, buy1_price, trade_time):
                results.append(("small_lot_sell_detector", alert))

        d = self.detectors.get("panic_sell_detector")
        if d is not None:
            for alert in d.update(
                symbol=symbol, msg_time=now, current_price=current_price, low_price=low_price,
                over_sell_qty=data.get("OverSellQty"), sell_levels=sell_levels,
                buy1_price=buy1_price, sell1_price=sell_levels[0][0], trading_volume=trading_volume,
            ):
                results.append(("panic_sell_detector", alert))

        d = self.detectors.get("under_surge_detector")
        if d is not None:
            for alert in d.update(
                symbol=symbol, msg_time=now, current_price=current_price, low_price=low_price,
                under_buy_qty=data.get("UnderBuyQty"), over_sell_qty=data.get("OverSellQty"),
            ):
                results.append(("under_surge_detector", alert))

        # AIストラテジー: 先に現在値更新で仮想建玉の決済判定を行い（エントリー直後の
        # 同一メッセージで即決済しないよう順序を固定）、その後に基礎ストラテジーの
        # 検知アラートをエントリー判定に配る
        if self.ai_strategies:
            base_results = list(results)
            for name, strat in self.ai_strategies.items():
                for alert in strat.on_price(symbol, current_price, now):
                    results.append((name, alert))
            for base_name, base_alert in base_results:
                for name, strat in self.ai_strategies.items():
                    for alert in strat.on_signal(base_name, base_alert, now):
                        results.append((name, alert))

        return results


def main():
    parser = argparse.ArgumentParser(description="統合ランナー（全ストラテジーを1接続で実行）")
    parser.add_argument("--config", default="../config.yaml")
    args = parser.parse_args()

    config = load_config(args.config)
    notifier.setup_logging()
    log = logging.getLogger("runner")

    api_password = os.environ.get("KABU_API_PASSWORD")
    if not api_password:
        log.error("環境変数 KABU_API_PASSWORD が設定されていません")
        sys.exit(1)

    engine = RunnerEngine(config)
    if not engine.detectors:
        log.error("有効なストラテジーがありません。config.yamlのstrategiesでenabled: trueを設定してください")
        sys.exit(1)
    log.info("有効ストラテジー: %s",
             ", ".join(list(engine.detectors.keys()) + list(engine.ai_strategies.keys())))

    client = KabuClient(environment=config["environment"], api_password=api_password)
    client.authenticate()
    log.info("認証に成功しました（環境: %s, ポート: %s）", client.environment, client.port)

    symbols = load_symbols(config, args.config)
    log.info("監視銘柄: %d銘柄（%s から読み込み）", len(symbols),
             "config直接指定" if config.get("symbols") else config.get("symbols_file"))
    client.unregister_all()
    reg_result = client.register_symbols(symbols)
    log.info("銘柄登録結果: %s", reg_result)

    debug_remaining = [config.get("debug_raw_messages", 0)]

    def on_message(ws, message):
        data = json.loads(message)
        if debug_remaining[0] > 0:
            log.info("[DEBUG RAW] %s", data)
            debug_remaining[0] -= 1
        for strategy, alert in engine.handle(data):
            notifier.notify(strategy, alert)

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
