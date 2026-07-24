"""マーケットスピードII RSS（歩み値）を使った定期買い集め検知ランナー。

起動中のExcel（RSSアドイン有効・マーケットスピードIIログイン済み）にアタッチし、
各銘柄の RssTickList スピル範囲を一定間隔でポーリング。新規約定を抽出し、
ティックルールで売買方向を推定して PeriodicBuyTickDetector に投入、
「トリガー約定の丁度N秒後の買い」が当日規定回数に達したら通知する。

検知・通知のみで発注は行わない。詳細・セットアップ手順は ../README.md を参照。

実行:
  cd strategies/periodic_buy_rss/src
  python main.py --config ../config.yaml
"""
import argparse
import logging
import os
import sys
import time
from datetime import datetime

import yaml

from tick_detector import PeriodicBuyTickDetector, TickDeduper, classify_tick
import ms2_rss

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.normpath(os.path.join(BASE_DIR, "..", "logs"))

STRATEGY_LABEL = "アルゴ買い集め(RSS)"

try:
    from plyer import notification as _plyer_notification
except ImportError:
    _plyer_notification = None


class DailyFileHandler(logging.FileHandler):
    """日付入りファイル名に書き、日付が変わったら自動で切り替える。"""

    def __init__(self, log_dir: str, prefix: str = "periodic_buy_rss"):
        self.log_dir = log_dir
        self.prefix = prefix
        self.current_date = datetime.now().strftime("%Y-%m-%d")
        os.makedirs(log_dir, exist_ok=True)
        super().__init__(self._path(), encoding="utf-8")

    def _path(self):
        return os.path.join(self.log_dir, f"{self.prefix}_{self.current_date}.log")

    def emit(self, record):
        date = datetime.now().strftime("%Y-%m-%d")
        if date != self.current_date:
            self.current_date = date
            self.close()
            self.baseFilename = os.path.abspath(self._path())
            self.stream = None
        super().emit(record)


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[DailyFileHandler(LOG_DIR), logging.StreamHandler()],
    )


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _symbol_code(item) -> str:
    """symbols.yaml の要素（{'symbol': '4165', 'exchange': 1} 形式 or 文字列）からコードを取り出す。"""
    if isinstance(item, dict):
        return str(item.get("symbol"))
    return str(item)


def load_symbols(config: dict, config_path: str) -> list:
    if config.get("symbols"):
        return [_symbol_code(s) for s in config["symbols"]]
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
    return [_symbol_code(s) for s in symbols]


def parse_trade_time(time_str: str, now: datetime) -> datetime:
    """歩み値の時刻文字列を当日日付つきの datetime にする。

    "HH:MM:SS" / "HH:MM:SS.ff" / Excelシリアル値の文字列など環境差に耐える。
    パースできなければ None を返す（呼び出し側でスキップ）。
    """
    s = str(time_str).strip()
    for fmt in ("%H:%M:%S", "%H:%M:%S.%f", "%Y/%m/%d %H:%M:%S", "%H:%M"):
        try:
            t = datetime.strptime(s, fmt)
        except ValueError:
            continue
        if fmt.startswith("%Y"):
            return t.replace(tzinfo=now.tzinfo)
        return now.replace(hour=t.hour, minute=t.minute, second=t.second,
                           microsecond=t.microsecond)
    return None


def build_message(alert: dict) -> tuple:
    title = f"[{STRATEGY_LABEL}/{alert['tier']}] {alert['symbol']}"
    trigger_label = {"sell": "売り約定", "buy": "買い約定", "any": "約定"}.get(
        alert["trigger_side"], "約定")
    body = (
        f"{alert['symbol']}: {trigger_label}の約{alert['avg_delay']:.1f}秒後に"
        f"買い上がる動きを本日{alert['occurrences']}回検知（歩み値ベース・現在値{alert['price']}円）。"
        f"一定ラグで反応する買い集めアルゴの可能性（大量保有報告に向けた仕込みサインの疑い）"
    )
    return title, body


def notify(log, alert: dict):
    title, body = build_message(alert)
    log.info("%s %s", title, body)
    if _plyer_notification is not None:
        try:
            _plyer_notification.notify(title=title, message=body, timeout=10)
        except Exception:
            log.exception("ポップアップ通知の送信に失敗しました")


class Feeder:
    """1銘柄ぶんの状態（重複除去・直前約定値/方向）を持ち、新規約定を検知器へ流す。"""

    def __init__(self, detector: PeriodicBuyTickDetector):
        self.detector = detector
        self.dedupers = {}       # symbol -> TickDeduper
        self.last_price = {}     # symbol -> float
        self.last_side = {}      # symbol -> str

    def process_batch(self, symbol: str, batch: list, now: datetime) -> list:
        """batch: 古い順の [(time_str, volume, price)]。発火アラートのリストを返す。"""
        deduper = self.dedupers.setdefault(symbol, TickDeduper())
        alerts = []
        for time_str, volume, price in deduper.new_trades(batch):
            try:
                price_f = float(price)
            except (TypeError, ValueError):
                continue
            vol = None
            try:
                vol = float(volume) if volume not in (None, "") else None
            except (TypeError, ValueError):
                vol = None
            side = classify_tick(price_f, self.last_price.get(symbol), self.last_side.get(symbol))
            self.last_price[symbol] = price_f
            if side != "unknown":
                self.last_side[symbol] = side
            t = parse_trade_time(time_str, now)
            if t is None:
                continue
            alerts.extend(self.detector.on_trade(symbol, t, price_f, vol, side))
        return alerts


def main():
    parser = argparse.ArgumentParser(description="MS2 RSS 歩み値による定期買い集め検知")
    parser.add_argument("--config", default="../config.yaml")
    args = parser.parse_args()

    config = load_config(args.config)
    setup_logging()
    log = logging.getLogger("periodic_buy_rss")

    symbols = load_symbols(config, args.config)
    d = config.get("detector", {})
    detector = PeriodicBuyTickDetector(
        delay_seconds=d.get("delay_seconds", 10.0),
        delay_tolerance_seconds=d.get("delay_tolerance_seconds", 1.0),
        trigger_side=d.get("trigger_side", "sell"),
        alert_tiers=d.get("alert_tiers"),
        min_lot=d.get("min_lot", 0),
        min_occurrence_gap_seconds=d.get("min_occurrence_gap_seconds", 2.0),
    )
    feeder = Feeder(detector)

    rss = config.get("rss", {})
    reader = ms2_rss.MarketSpeedTickReader(
        symbols=symbols,
        market_suffix=rss.get("market_suffix", "T"),
        tick_count=rss.get("tick_count", 300),
        sheet_name=rss.get("sheet_name", "TICKS"),
        anchor_row=rss.get("anchor_row", 1),
        cols_per_symbol=rss.get("cols_per_symbol", 4),
        newest_first=rss.get("newest_first", True),
        workbook_name=rss.get("workbook_name"),
        com_retries=rss.get("com_retries", 60),
        com_retry_delay=rss.get("com_retry_delay", 0.25),
    )
    poll_interval = rss.get("poll_interval_seconds", 1.0)

    log.info("定期買い集め検知(RSS) 起動。監視銘柄: %d件、ポーリング間隔: %.1f秒",
             len(symbols), poll_interval)
    reader.connect()
    log.info("Excel(マーケットスピードII RSS)への接続に成功しました。数式を書き込みました。")
    log.info("RssTickListの初回反映を待機します（数秒）...")
    time.sleep(rss.get("warmup_seconds", 5))

    debug_remaining = config.get("debug_raw_batches", 0)
    while True:
        cycle_start = time.time()
        now = datetime.now().astimezone()
        for sym in symbols:
            try:
                batch = reader.read(sym)
            except Exception:
                log.exception("銘柄 %s の読み取りに失敗しました", sym)
                continue
            if debug_remaining > 0 and batch:
                log.info("[DEBUG BATCH] %s 直近3件: %s", sym, batch[-3:])
                debug_remaining -= 1
            for alert in feeder.process_batch(sym, batch, now):
                notify(log, alert)
        elapsed = time.time() - cycle_start
        time.sleep(max(0.0, poll_interval - elapsed))


if __name__ == "__main__":
    main()
