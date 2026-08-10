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
from datetime import datetime, time as dtime

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


def parse_hhmm(s: str):
    """設定の "09:00" 形式を time に変換する。None/空ならNone。"""
    if not s:
        return None
    h, m = str(s).split(":")[:2]
    return dtime(int(h), int(m))


def parse_trade_time(time_str: str, now: datetime) -> datetime:
    """歩み値の時刻文字列を datetime にする。

    kabu /timeandsales は ISO8601（"2026-08-10T15:30:00+09:00"）で**日付付き**なので
    そのまま解釈する。RSS（Excel）は "HH:MM:SS" と日付が無いため当日日付を補う。
    パースできなければ None を返す（呼び出し側でスキップ）。
    """
    s = str(time_str).strip()
    # ISO8601（日付・タイムゾーン付き）を最優先で試す
    if "T" in s and "-" in s[:11]:
        try:
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None and now.tzinfo is not None:
                dt = dt.replace(tzinfo=now.tzinfo)
            return dt
        except ValueError:
            pass
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
    """1銘柄ぶんの状態（重複除去・直前約定値/方向）を持ち、新規約定を検知器へ流す。

    session_start / session_end を与えると、その時間帯の約定だけを検知対象にする。
    さらに「現在時刻より未来の約定」も捨てる。RssTickListは起動時点で前営業日の
    残りティック（例: 15:24）を返すため、これを弾かないと寄り付き前や寄り直後に
    前日ぶんを当日の検知としてカウントしてしまう。
    """

    def __init__(self, detector: PeriodicBuyTickDetector,
                 session_start=None, session_end=None, skip_future=True,
                 future_tolerance_seconds: float = 60.0):
        self.detector = detector
        self.session_start = session_start
        self.session_end = session_end
        self.skip_future = skip_future
        self.future_tolerance = future_tolerance_seconds
        self.dedupers = {}       # symbol -> TickDeduper
        self.last_price = {}     # symbol -> float
        self.last_side = {}      # symbol -> str

    def _accept(self, trade_time, now) -> bool:
        t = trade_time.time()
        if self.session_start is not None and t < self.session_start:
            return False
        if self.session_end is not None and t > self.session_end:
            return False
        if self.skip_future and now is not None:
            if (trade_time - now).total_seconds() > self.future_tolerance:
                return False   # 前営業日の残りティック（現在より未来の時刻）
        return True

    def process_batch(self, symbol: str, batch: list, now: datetime) -> list:
        """batch: 古い順の [(time_str, volume, price)]。発火アラートのリストを返す。"""
        deduper = self.dedupers.setdefault(symbol, TickDeduper())
        alerts = []
        for time_str, volume, price in deduper.new_trades(batch):
            try:
                price_f = float(price)
            except (TypeError, ValueError):
                continue
            t = parse_trade_time(time_str, now)
            if t is None:
                continue
            # 場中以外・前日の残りティックは検知に使わない。
            # 直前値(last_price)も更新しないので、翌日のティック判定が前日値に引きずられない。
            if not self._accept(t, now):
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
            alerts.extend(self.detector.on_trade(symbol, t, price_f, vol, side))
        return alerts


def main():
    parser = argparse.ArgumentParser(description="MS2 RSS 歩み値による定期買い集め検知")
    parser.add_argument("--config", default="../config.yaml")
    args = parser.parse_args()

    setup_logging()
    run_loop(args.config, log=logging.getLogger("periodic_buy_rss"))


def run_loop(config_path: str, log=None, notify_fn=None, stop_event=None,
             connect_retry_seconds: float = 60.0, max_connect_retries: int = 10,
             kabu_client=None):
    """歩み値のポーリングループを実行する（統合ランナーからも呼べるよう切り出し）。

    log: 使用するロガー（未指定なら本ツール専用）
    notify_fn: 通知関数 fn(title, body)。未指定なら本ツールのログ＋ポップアップ
    stop_event: threading.Event。set されたらループを抜ける
    connect_retry_seconds / max_connect_retries:
        Excel（MS2 RSS）に接続できないときの再試行間隔と回数。
        朝、ランナーを先に起動してから Excel/MS2 を立ち上げる運用でも拾えるようにする。
    """
    log = log or logging.getLogger("periodic_buy_rss")
    config = load_config(config_path)

    symbols = load_symbols(config, config_path)
    d = config.get("detector", {})
    detector = PeriodicBuyTickDetector(
        delay_seconds=d.get("delay_seconds", 10.0),
        delay_tolerance_seconds=d.get("delay_tolerance_seconds", 0.0),
        trigger_side=d.get("trigger_side", "any"),
        alert_tiers=d.get("alert_tiers"),
        min_lot=d.get("min_lot", 0),
        min_occurrence_gap_seconds=d.get("min_occurrence_gap_seconds", 2.0),
        buy_side_mode=d.get("buy_side_mode", "non_down"),
        lot_similarity_pct=d.get("lot_similarity_pct", 0.0),
    )
    rss = config.get("rss", {})
    session_start = parse_hhmm(rss.get("session_start", "09:00"))
    session_end = parse_hhmm(rss.get("session_end", "15:30"))
    feeder = Feeder(detector, session_start=session_start, session_end=session_end)
    source_kind = str(config.get("source", "kabu")).lower()
    if source_kind == "kabu":
        # kabuステーションAPIの歩み値を使う（Excel・MS2は不要）
        import kabu_tick_source
        kb = config.get("kabu", {})
        reader = kabu_tick_source.KabuTickSource(
            client=kabu_client, symbols=[s for s, _ in symbols] if symbols
            and isinstance(symbols[0], tuple) else symbols,
            exchange=kb.get("exchange", 1), log=log)
        poll_interval = kb.get("poll_interval_seconds", 1.0)
    else:
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

    log.info("定期買い集め検知 起動（取得元: %s）。監視銘柄: %d件、"
             "1銘柄あたりの間隔: %.1f秒（1周およそ%.0f秒）",
             "kabu歩み値API" if source_kind == "kabu" else "楽天MS2 RSS",
             len(symbols), poll_interval, len(symbols) * poll_interval)

    def stopped():
        return stop_event is not None and stop_event.is_set()

    if source_kind == "kabu" and kabu_client is None:
        log.error("取得元にkabuを指定していますが、kabuクライアントが渡されていません。"
                  "統合ランナーから起動するか、config の source を rss にしてください")
        return

    # 接続。kabuは事前準備不要なので1回で済む。RSSはExcel未起動に備えて再試行する
    for attempt in range(1, (1 if source_kind == "kabu" else max_connect_retries) + 1):
        if stopped():
            return
        try:
            reader.connect()
            break
        except Exception as e:
            if attempt >= max_connect_retries:
                log.error("Excel(マーケットスピードII RSS)に接続できませんでした（%d回試行）。"
                          "MS2とExcel（RSSアドイン有効）を起動してから再実行してください: %s",
                          attempt, e)
                return
            log.warning("Excelに接続できません（%d/%d回目）。%.0f秒後に再試行します: %s",
                        attempt, max_connect_retries, connect_retry_seconds, e)
            if stop_event is not None:
                stop_event.wait(connect_retry_seconds)
            else:
                time.sleep(connect_retry_seconds)
    if source_kind == "rss":
        log.info("Excel(マーケットスピードII RSS)への接続に成功しました。数式を書き込みました。")
        log.info("RssTickListの初回反映を待機します（数秒）...")
        time.sleep(rss.get("warmup_seconds", 5))

    def emit(alert):
        if notify_fn is not None:
            title, body = build_message(alert)
            notify_fn(title, body)
        else:
            notify(log, alert)

    # 手集計との突き合わせ用に、検知回数を定期的にまとめてログへ出す
    summary_interval = float(config.get("summary_interval_minutes", 30)) * 60
    last_summary = time.time()

    def log_summary():
        counts = sorted(((s.occurrences, sym) for sym, s in detector.states.items()
                         if s.occurrences > 0), reverse=True)
        if not counts:
            log.info("[集計] 「%.0f秒後の買い」の検知はまだありません", detector.delay)
            return
        top = "  ".join(f"{sym}:{n}回" for n, sym in counts[:10])
        log.info("[集計] 「%.0f秒後の買い」当日検知回数（上位10銘柄） %s", detector.delay, top)

    idle_poll = float(rss.get("idle_poll_seconds", 30))
    was_in_session = None

    def in_session(t):
        if session_start is not None and t < session_start:
            return False
        if session_end is not None and t > session_end:
            return False
        return True

    debug_remaining = config.get("debug_raw_batches", 0)
    while not stopped():
        cycle_start = time.time()
        now_t = datetime.now().astimezone()

        # 場中（既定 09:00〜15:30）以外はExcelを読みに行かない。
        # 前営業日の残りティックを当日ぶんとして数えてしまう事故も防げる。
        if not in_session(now_t.time()):
            if was_in_session is not False:
                log.info("時間外のため検知を停止します（対象 %s〜%s）。%.0f秒ごとに時刻を確認します",
                         session_start.strftime("%H:%M") if session_start else "—",
                         session_end.strftime("%H:%M") if session_end else "—", idle_poll)
                if was_in_session:
                    log_summary()
                was_in_session = False
            if stop_event is not None:
                stop_event.wait(idle_poll)
            else:
                time.sleep(idle_poll)
            continue
        if was_in_session is not True:
            log.info("場中に入りました。検知を開始します")
            was_in_session = True

        if summary_interval > 0 and cycle_start - last_summary >= summary_interval:
            log_summary()
            last_summary = cycle_start
        # 銘柄ごとに間隔を空けて読む。
        # kabu歩み値APIはレート制限があるが、毎回その日の全件が返るため
        # 間隔を空けても取りこぼしはなく、検知が遅れるだけ。
        for sym in symbols:
            if stopped():
                break
            now = datetime.now().astimezone()
            if not in_session(now.time()):
                break
            t0 = time.time()
            try:
                batch = reader.read(sym)
            except Exception:
                log.exception("銘柄 %s の読み取りに失敗しました", sym)
                batch = []
            if debug_remaining > 0 and batch:
                log.info("[DEBUG BATCH] %s %d件 直近3件: %s", sym, len(batch), batch[-3:])
                debug_remaining -= 1
            for alert in feeder.process_batch(sym, batch, now):
                emit(alert)
            wait = max(0.0, poll_interval - (time.time() - t0))
            if stop_event is not None:
                stop_event.wait(wait)
            else:
                time.sleep(wait)


if __name__ == "__main__":
    main()
