"""統合ランナー: 全ストラテジーを1プロセス・1WebSocket接続でまとめて実行する。

kabuステーションへの接続・認証・銘柄登録を1回だけ行い、受信した各PUSHメッセージを
有効化された全ストラテジーの検知エンジン（各strategies/*/src/detector.py）に配る。
検知・通知のみで発注は行わない。

起動前提:
  - kabuステーション（デスクトップアプリ）を起動し、ログインしておく
  - 環境変数 KABU_API_PASSWORD にkabuステーションのAPIパスワードを設定しておく
    （毎回入力しないよう setx で永続化推奨。詳細は ../README.md「認証情報の設定」）
  - config.yaml で有効にするストラテジーと閾値を設定する
  - 監視銘柄は ../symbols.yaml（全ストラテジー共通）で管理する

詳細は ../README.md を参照。
"""
import argparse
import importlib.util
import json
import logging
import os
import signal
import sys
import threading
import time
from datetime import datetime, time as dtime

import websocket
import yaml

from kabu_client import KabuClient
import account_snapshot
import notifier

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STRATEGIES_ROOT = os.path.normpath(os.path.join(BASE_DIR, "..", ".."))

# コントロールパネルはこのファイルを置いて停止を要求する（詳細はメインループ末尾）。
# 中身は停止したいプロセスのPID。他プロセスあて／読めない内容なら無視する。
STOP_FILE = os.path.normpath(os.path.join(BASE_DIR, "..", "state", "stop.request"))

def _stop_requested() -> bool:
    """自分あての停止要求が置かれているか。"""
    try:
        with open(STOP_FILE, "r", encoding="utf-8") as f:
            return f.read().strip() == str(os.getpid())
    except OSError:
        return False


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


def start_edinet_monitor(cfg: dict, log):
    """EDINET大量保有報告書モニタをバックグラウンドで定期実行する。

    本体（strategies/edinet_holder_monitor）は日次バッチだが、ランナーは常時稼働の
    WebSocketループのため、別スレッドで「起動時に1回 → 以降 interval_hours ごと」に回す。
    PUSH処理をブロックしないこと、EDINET側の失敗でランナーを落とさないことを優先する。
    通知はランナーのnotifier経由で出るので、ログは runner/logs/ に一元化される。
    """
    import threading

    mon_dir = os.path.join(STRATEGIES_ROOT, "edinet_holder_monitor")
    mon_src = os.path.join(mon_dir, "src")
    mon_config = cfg.get("config_path") or os.path.join(mon_dir, "config.yaml")
    if not os.path.exists(mon_config):
        log.warning("EDINETモニタの設定が見つからないためスキップします: %s", mon_config)
        return
    if not os.environ.get("EDINET_API_KEY"):
        log.warning("環境変数 EDINET_API_KEY が未設定のためEDINETモニタは起動しません"
                    "（設定方法: setx EDINET_API_KEY \"取得したAPIキー\"）")
        return

    # モニタ側の内部import（absorption / edinet_client）を解決するためsys.pathに追加する
    if mon_src not in sys.path:
        sys.path.insert(0, mon_src)
    spec = importlib.util.spec_from_file_location(
        "edinet_holder_monitor__main", os.path.join(mon_src, "main.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    interval = float(cfg.get("interval_hours", 24)) * 3600
    days = cfg.get("days")

    def loop():
        first = True
        while True:
            try:
                alerts = mod.run_once(mon_config, log=log,
                                      notify_fn=notifier.notify_message,
                                      days=days if first else None)
                log.info("EDINETモニタ: チェック完了（新規通知 %d件）", len(alerts))
            except Exception:
                log.exception("EDINETモニタの実行に失敗しました（ランナーは継続します）")
            first = False
            time.sleep(interval)

    threading.Thread(target=loop, name="edinet-monitor", daemon=True).start()
    log.info("EDINETモニタをバックグラウンドで起動しました（%.1f時間ごと）",
             interval / 3600)


def build_autotrader(config: dict, client, log):
    """自動売買（strategies/autotrade）を用意する。無効・未設定なら None を返す。

    実弾が動きうる部分なので、設定が無い／読めない場合は黙って無効にする。
    """
    at_dir = os.path.join(STRATEGIES_ROOT, "autotrade")
    at_src = os.path.join(at_dir, "src")
    at_config = os.path.join(at_dir, "config.yaml")
    if not os.path.exists(at_config):
        return None
    if at_src not in sys.path:
        sys.path.insert(0, at_src)
    try:
        with open(at_config, "r", encoding="utf-8") as f:
            at_cfg = yaml.safe_load(f) or {}
        import account as at_account
        import executor as at_executor
        import trader as at_trader
    except Exception:
        log.exception("自動売買の読み込みに失敗しました（自動売買は無効のまま継続します）")
        return None

    enabled = at_cfg.get("enabled")
    on = [k for k, v in (at_cfg.get("strategies") or {}).items() if v]
    if not enabled or not on:
        log.info("自動売買: 無効（enabled=%s / 実売買対象=%s）。仮想売買のみ継続します",
                 enabled, on or "なし")
        return None

    mode = "DRY-RUN（送信しません）" if at_cfg.get("dry_run", True) else "★実発注★"
    log.warning("自動売買: 有効  モード=%s  対象戦略=%s", mode, on)
    if not at_cfg.get("dry_run", True):
        log.warning("★★ dry_run=false です。実際に注文が発注されます ★★")

    view = at_account.AccountView(client, log)
    ex = at_executor.Executor(at_cfg, client=client, log=log)
    # 各戦略の損切り/利確幅は runner 側の設定を引き継ぐ
    sp = {}
    # panic_rebound_wide は仮想売買のみだが、将来 autotrade の strategies に
    # 追加されたときに損切り・利確幅が引き継がれるようここにも入れておく
    # （autotrade側の strategies に無い間は _active() が False を返すので発注されない）
    for name in ("afternoon_reversal", "afternoon_reversal_ranked",
                 "panic_rebound", "confluence", "panic_rebound_wide"):
        s = (config.get("strategies") or {}).get(name) or {}
        sp[name] = {"stop_loss_pct": s.get("stop_loss_pct", 2.0),
                    "take_profit_pct": s.get("take_profit_pct", 2.0)}
    return at_trader.AutoTrader(at_cfg, ex, view, log=log, strategy_params=sp), view


def start_periodic_buy_zscore(cfg: dict, log, client=None):
    """定期買い集め検知（z値方式）をバックグラウンドで実行する。

    kabuステーションAPIの歩み値をポーリングし、「約定のちょうど10秒後の買い」が
    他のラグと比べて統計的に突出している銘柄を通知する。
    PUSHとは別系統のポーリングになるため別スレッドで回す。
    失敗してもランナー本体は動作を継続する。
    """
    import threading

    tool_dir = os.path.join(STRATEGIES_ROOT, "periodic_buy_zscore")
    tool_src = os.path.join(tool_dir, "src")
    tool_config = cfg.get("config_path") or os.path.join(tool_dir, "config.yaml")
    if not os.path.exists(tool_config):
        log.warning("定期買い集め検知(z値)の設定が見つからないためスキップします: %s", tool_config)
        return
    if tool_src not in sys.path:
        sys.path.insert(0, tool_src)
    spec = importlib.util.spec_from_file_location(
        "periodic_buy_zscore__main", os.path.join(tool_src, "main.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    def loop():
        try:
            mod.run_loop(tool_config, log=log, notify_fn=notifier.notify_message,
                         kabu_client=client)
        except Exception:
            log.exception("定期買い集め検知(z値)の実行に失敗しました（ランナーは継続します）")

    threading.Thread(target=loop, name="periodic-buy-zscore", daemon=True).start()
    log.info("定期買い集め検知(z値方式)をバックグラウンドで起動しました")


def start_periodic_buy_rss(cfg: dict, log, client=None):
    """定期買い集め検知（歩み値ベース）をバックグラウンドで実行する。

    取得元はツール側 config の source で選ぶ:
      "kabu" … kabuステーションAPI /timeandsales（既定・Excel不要）
      "rss"  … 楽天マーケットスピードII RSS（Excel経由・旧方式）
    PUSHとは別系統のポーリングになるため別スレッドで回す（PUSH処理はブロックしない）。
    失敗してもランナー本体は動作を継続する。通知はランナーのnotifier経由で一元化される。
    """
    import threading

    tool_dir = os.path.join(STRATEGIES_ROOT, "periodic_buy_rss")
    tool_src = os.path.join(tool_dir, "src")
    tool_config = cfg.get("config_path") or os.path.join(tool_dir, "config.yaml")
    if not os.path.exists(tool_config):
        log.warning("RSS検知の設定が見つからないためスキップします: %s", tool_config)
        return

    # ツール側の内部import（tick_detector / ms2_rss）を解決するためsys.pathに追加する
    if tool_src not in sys.path:
        sys.path.insert(0, tool_src)
    spec = importlib.util.spec_from_file_location(
        "periodic_buy_rss__main", os.path.join(tool_src, "main.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    def loop():
        try:
            mod.run_loop(tool_config, log=log, notify_fn=notifier.notify_message,
                         connect_retry_seconds=cfg.get("connect_retry_seconds", 60),
                         max_connect_retries=cfg.get("max_connect_retries", 10),
                         kabu_client=client)
        except Exception:
            log.exception("定期買い集め検知の実行に失敗しました（ランナーは継続します）")

    threading.Thread(target=loop, name="periodic-buy", daemon=True).start()
    log.info("定期買い集め検知をバックグラウンドで起動しました")


class RunnerEngine:
    """PUSHメッセージを有効な全detectorに配るディスパッチャ。"""

    def __init__(self, config: dict):
        self.detectors = {}
        self.autotrader = None      # 自動売買。main()から差し込む（未設定なら仮想売買のみ）
        self.autotrade_pump = None  # 自動売買の処理スレッド。未設定ならこのスレッドで同期処理
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
        rk = strategies_cfg.get("afternoon_reversal_ranked", {})
        cf = strategies_cfg.get("confluence", {})
        pr = strategies_cfg.get("panic_rebound", {})
        pw = strategies_cfg.get("panic_rebound_wide", {})
        if any(c.get("enabled") for c in (ar, rk, cf, pr, pw)):
            ai_mod = load_detector_module("AIStrategys")
            if ar.get("enabled"):
                self.ai_strategies["afternoon_reversal"] = ai_mod.AfternoonReversalStrategy(
                    entry_start=parse_time(ar.get("entry_start", "13:00")),
                    entry_end=parse_time(ar.get("entry_end", "15:00")),
                    stop_loss_pct=ar.get("stop_loss_pct", 2.0),
                    take_profit_pct=ar.get("take_profit_pct", 2.0),
                    min_entry_price=ar.get("min_entry_price", 500.0),
                )
            # 順位優先版: 検知・決済は afternoon_reversal と同じで、
            # 銘柄リストの順位によってエントリーを取捨選択する点だけが違う。
            # 順位は起動時に main() から set_ranks() で渡す。
            if rk.get("enabled"):
                self.ai_strategies["afternoon_reversal_ranked"] = \
                    ai_mod.RankedAfternoonReversalStrategy(
                        entry_start=parse_time(rk.get("entry_start", "13:00")),
                        entry_end=parse_time(rk.get("entry_end", "15:00")),
                        stop_loss_pct=rk.get("stop_loss_pct", 2.0),
                        take_profit_pct=rk.get("take_profit_pct", 2.0),
                        min_entry_price=rk.get("min_entry_price", 500.0),
                        top_rank=rk.get("top_rank", 25),
                        late_entry_after=parse_time(rk.get("late_entry_after", "14:00")),
                    )
            if cf.get("enabled"):
                self.ai_strategies["confluence"] = ai_mod.ConfluenceStrategy(
                    window_seconds=cf.get("window_seconds", 1800),
                    entry_start=parse_time(cf.get("entry_start", "13:00")),
                    entry_end=parse_time(cf.get("entry_end", "15:00")),
                    stop_loss_pct=cf.get("stop_loss_pct", 1.0),
                    take_profit_pct=cf.get("take_profit_pct", 1.0),
                )
            if pr.get("enabled"):
                self.ai_strategies["panic_rebound"] = ai_mod.PanicReboundStrategy(
                    entry_start=parse_time(pr.get("entry_start", "09:00")),
                    entry_end=parse_time(pr.get("entry_end", "15:00")),
                    stop_loss_pct=pr.get("stop_loss_pct", 1.0),
                    take_profit_pct=pr.get("take_profit_pct", 2.0),
                    min_entry_price=pr.get("min_entry_price", 0.0),
                    stages=tuple(pr.get("stages", ["DUMP"])),
                )
            # 幅広版: 検知は panic_rebound と同じで、損切り・利確の幅だけが違う。
            # PaperBookはインスタンスごとに独立しているので建玉は混ざらない。
            if pw.get("enabled"):
                self.ai_strategies["panic_rebound_wide"] = ai_mod.PanicReboundStrategy(
                    entry_start=parse_time(pw.get("entry_start", "09:00")),
                    entry_end=parse_time(pw.get("entry_end", "15:00")),
                    stop_loss_pct=pw.get("stop_loss_pct", 1.5),
                    take_profit_pct=pw.get("take_profit_pct", 3.0),
                    min_entry_price=pw.get("min_entry_price", 0.0),
                    stages=tuple(pw.get("stages", ["DUMP"])),
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

        # 自動売買: 板とシグナルを専用スレッドに渡すだけにする。
        # ここでAPIを呼ぶと、発注の応答を待つあいだPUSHの受信が丸ごと止まる
        # （詳細と実測: ../../autotrade/src/pump.py）。
        # 例外が出てもPUSH処理と仮想売買は止めない。
        if self.autotrader is not None:
            try:
                sell1_price = sell_levels[0][0] if sell_levels else None
                entries = [(name, alert) for name, alert in results
                           if name in self.ai_strategies and alert.get("type") == "ENTRY"]
                if self.autotrade_pump is not None:
                    self.autotrade_pump.submit_tick(symbol, current_price, buy1_price,
                                                    sell1_price, now)
                    for name, alert in entries:
                        self.autotrade_pump.submit_signal(name, alert, now)
                else:
                    # pumpを使わない場合（単体テストなど）は従来どおり同期で処理する
                    self.autotrader.on_tick(symbol, current_price, buy1_price,
                                            sell1_price, now)
                    for name, alert in entries:
                        self.autotrader.on_signal(name, alert, now)
                    self.autotrader.poll(now)
            except Exception:
                logging.getLogger("runner").exception(
                    "自動売買の処理でエラー（ランナーは継続します）")

        return results


def main():
    parser = argparse.ArgumentParser(description="統合ランナー（全ストラテジーを1接続で実行）")
    parser.add_argument("--config", default="../config.yaml")
    args = parser.parse_args()

    config = load_config(args.config)
    notifier.setup_logging()
    notifier.configure(config.get("notification", {}))
    log = logging.getLogger("runner")

    # 前回の停止要求が残っていると起動直後に止まってしまうので消しておく。
    # ただし「自分のPIDあての要求」は消さない。コントロールパネルが起動直後に停止を
    # 要求した場合、ここで無条件に消すとその要求が握りつぶされてしまうため
    # （実測: 起動2秒後の停止要求が効かず強制終了になった）。
    if not _stop_requested():
        try:
            os.remove(STOP_FILE)
        except OSError:
            pass

    api_password = os.environ.get("KABU_API_PASSWORD")
    if not api_password:
        log.error("環境変数 KABU_API_PASSWORD が設定されていません。"
                  "次のコマンドで永続化できます（一度だけ実行し、ターミナルを開き直す）: "
                  'setx KABU_API_PASSWORD "本番用APIパスワード"')
        sys.exit(1)

    engine = RunnerEngine(config)
    if not engine.detectors:
        log.error("有効なストラテジーがありません。config.yamlのstrategiesでenabled: trueを設定してください")
        sys.exit(1)
    log.info("有効ストラテジー: %s",
             ", ".join(list(engine.detectors.keys()) + list(engine.ai_strategies.keys())))

    # 認証は最初に済ませる。以降のバックグラウンド処理はこのクライアント（＝同一トークン）を
    # 共有する。トークンは「別のトークンが新たに発行された時」に無効になるため
    # （公式リファレンス /token）、1プロセス内で発行するトークンは1つに保つ。
    client = KabuClient(environment=config["environment"], api_password=api_password)
    client.authenticate()
    log.info("認証に成功しました（環境: %s, ポート: %s）", client.environment, client.port)

    # EDINET大量保有報告書モニタ（別系統・バックグラウンド実行）
    edinet_cfg = config.get("edinet_holder_monitor", {})
    if edinet_cfg.get("enabled"):
        start_edinet_monitor(edinet_cfg, log)

    # 定期買い集め検知（z値方式・現行）
    z_cfg = config.get("periodic_buy_zscore", {})
    if z_cfg.get("enabled"):
        start_periodic_buy_zscore(z_cfg, log, client)

    # 定期買い集め検知（旧・生カウント方式）。既定は無効
    rss_cfg = config.get("periodic_buy_rss", {})
    if rss_cfg.get("enabled"):
        log.warning("旧方式(periodic_buy_rss)が有効です。"
                    "z値方式と併用すると通知が重複します")
        start_periodic_buy_rss(rss_cfg, log, client)

    # 口座状態のスナップショット（コントロールパネル表示用）
    account_snapshot.start(client, config.get("account_snapshot", {}), log)

    symbols = load_symbols(config, args.config)
    log.info("監視銘柄: %d銘柄（%s から読み込み）", len(symbols),
             "config直接指定" if config.get("symbols") else config.get("symbols_file"))

    # 銘柄リストの並び順＝extracted_stocks のR/Rスコア順。これを順位として渡す。
    ranks = {str(s["symbol"]): i for i, s in enumerate(symbols, start=1)}
    for name, strat in engine.ai_strategies.items():
        if hasattr(strat, "set_ranks"):
            strat.set_ranks(ranks)
            log.info("%s: 銘柄リストの順位を設定しました（上位%d位は即エントリー / "
                     "それ以下は%s以降）", name, strat.top_rank,
                     strat.late_entry_after.strftime("%H:%M"))
    client.unregister_all()
    reg_result = client.register_symbols(symbols)
    log.info("銘柄登録結果: %s", reg_result)

    # 自動売買（strategies/autotrade）。無効なら None のまま＝仮想売買のみ
    built = build_autotrader(config, client, log)
    if built:
        engine.autotrader, at_view = built
        try:
            at_view.refresh("2")
        except Exception:
            log.exception("自動売買の初期化に失敗したため無効にします")
            engine.autotrader = None
        if engine.autotrader is not None:
            # 銘柄マスタの取得はバックグラウンドで進める。/symbol は1件約790msかかり、
            # 50銘柄で40秒近い。ここで待つと起動が遅れ、その間は停止要求にも
            # 反応できない。当日ぶんはキャッシュされるので2回目以降は一瞬で終わる。
            # 取得前にシグナルが来た銘柄は plan_quantity が個別に取りに行く。
            def load_master():
                try:
                    at_view.load_master([(s["symbol"], s["exchange"]) for s in symbols])
                except Exception:
                    log.exception("銘柄マスタの取得に失敗しました"
                                  "（発注時に個別取得へ切り替わります）")
            threading.Thread(target=load_master, daemon=True,
                             name="symbol-master").start()
        if engine.autotrader is not None:
            # 発注はこの専用スレッドで行う。PUSH受信スレッドをブロックさせないため
            import pump as at_pump
            engine.autotrade_pump = at_pump.AutoTradePump(engine.autotrader, log)
            engine.autotrade_pump.start()

    debug_remaining = [config.get("debug_raw_messages", 0)]

    def on_message(ws, message):
        data = json.loads(message)
        if debug_remaining[0] > 0:
            log.info("[DEBUG RAW] %s", data)
            debug_remaining[0] -= 1
        for strategy, alert in engine.handle(data):
            notifier.notify(strategy, alert)

    # ── 接続断からの自己復旧 ──
    # kabuステーション（KabuS.exe）はこちらの都合と関係なく落ちることがある。
    # 実例 2026-08-20 14:30:17: PCが終日「仮想メモリ不足」状態（イベントID 2004 が
    # その日80回）で、dwm.exe・EXCEL.EXE なども相次いで落ちるなかKabuS.exeが消えた。
    # WebSocketが WinError 10054（強制切断）→ 直後から18080は WinError 10061
    # （接続拒否＝誰も待ち受けていない）になった。旧実装は5秒おきに繋ぎ直そうとして
    # 6時間で4,003回失敗し、ログだけが1.2MB（通常80KB）に膨れ、誰も気づかなかった。
    #
    # そこで:
    #   1. 繋ぎ直す前にHTTP側で生死を見る。落ちている間はWebSocketを叩かない
    #   2. kabuステーションが再起動していると**トークンも銘柄登録も消えている**ので、
    #      必ず再認証・銘柄再登録をしてからWebSocketを張る。これを飛ばすと
    #      「WSは繋がっているのに板が1件も来ない」無言の停止になる（最も危険）
    #   3. 待ち時間は指数的に伸ばし（5秒→最大60秒）、同じ失敗のログは間引く
    #   4. 断が続いたらポップアップで知らせる（気づけるようにするのが主目的）
    RECONNECT_MIN_WAIT = 5
    RECONNECT_MAX_WAIT = 60
    OUTAGE_LOG_INTERVAL = 300                      # 断が続く間のログ間隔（秒）
    outage_notify_after = float(config.get("outage_notify_seconds", 60))

    # 断の状態。since が None でなければ「いま繋がっていない」
    outage = {"since": None, "attempts": 0, "last_log": 0.0, "notified": False}

    def resync_session():
        """kabuステーションと張り直す（再認証＋銘柄の再登録）。

        トークンはkabuステーションを再起動すると無効になり、銘柄登録も消える。
        どちらもHTTPが通らなければ例外になるので、生死確認も兼ねている。
        """
        client.authenticate()
        client.unregister_all()
        client.register_symbols(symbols)

    def note_outage(err):
        """接続に失敗した。最初の1回とその後の節目だけ記録する。"""
        now = time.monotonic()
        outage["attempts"] += 1
        if outage["since"] is None:
            outage["since"] = now
            outage["last_log"] = now
            log.error("kabuステーションに接続できません（%s: %s）。"
                      "アプリが落ちていないか確認してください。"
                      "復旧するまで繋ぎ直しを続けます（検知・自動売買は停止中）",
                      type(err).__name__, str(err)[:150] or "詳細なし")
            return
        down = now - outage["since"]
        if not outage["notified"] and down >= outage_notify_after:
            outage["notified"] = True
            notifier.notify_alert(
                "[ランナー] kabuステーションが応答しません",
                f"{int(down)}秒間つながりません（{outage['attempts']}回失敗）。"
                "kabuステーションを起動し直してください。"
                "検知・自動売買（損切り・利確・引け手仕舞い）は止まっています")
        if now - outage["last_log"] >= OUTAGE_LOG_INTERVAL:
            outage["last_log"] = now
            log.error("kabuステーションに接続できないまま%d分経過（%d回失敗）",
                      int(down // 60), outage["attempts"])

    def note_recovered():
        """接続できた。断からの復帰なら知らせる。"""
        if outage["since"] is None:
            return
        down = time.monotonic() - outage["since"]
        log.warning("kabuステーションへの接続が復旧しました"
                    "（%d分%d秒の断・%d回失敗）。認証と銘柄登録をやり直しました",
                    int(down // 60), int(down % 60), outage["attempts"])
        if outage["notified"]:
            notifier.notify_alert(
                "[ランナー] 接続が復旧しました",
                f"{int(down // 60)}分{int(down % 60)}秒ぶりに検知を再開しました。"
                "断のあいだの板は受け取れていません")
        outage.update({"since": None, "attempts": 0, "last_log": 0.0, "notified": False})

    ws_error_last_log = [0.0]

    def on_error(ws, error):
        # 断が続いている間は note_outage 側でまとめて記録する（1回/9秒で溢れるため）
        now = time.monotonic()
        if outage["since"] is None and now - ws_error_last_log[0] >= 60:
            ws_error_last_log[0] = now
            log.error("WebSocketエラー: %s: %s", type(error).__name__, error)
        else:
            log.debug("WebSocketエラー（抑制）: %s", error)

    def on_close(ws, code, msg):
        if outage["since"] is None:
            log.warning("WebSocket切断 (code=%s, msg=%s)", code, msg)
        else:
            log.debug("WebSocket切断（抑制・断の継続中）")

    def on_open(ws):
        log.info("WebSocket接続確立。PUSH配信の受信を開始します。")

    # ── 終了処理 ──
    # WebSocketの受信は別スレッドで回し、メインスレッドは停止信号を待つだけにする。
    # run_forever() の中で待っていると Ctrl+C / Ctrl+Break を受け取っても
    # 反応できず（コントロールパネルからの停止が強制終了になる）、
    # ログが途中で切れてしまうため。
    stop = threading.Event()

    def receive_loop():
        wait = RECONNECT_MIN_WAIT
        first = True
        while not stop.is_set():
            if first:
                first = False           # 起動時の認証・銘柄登録は main() で済んでいる
            else:
                try:
                    resync_session()
                except Exception as e:
                    note_outage(e)
                    stop.wait(wait)
                    wait = min(wait * 2, RECONNECT_MAX_WAIT)
                    continue
                note_recovered()
                # 待ち時間はここでは戻さない。接続が30秒もたなければ空回りなので、
                # run_forever() のあとで実際に繋がっていた時間を見て決める。
            ws = websocket.WebSocketApp(
                client.ws_url,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close,
                on_open=on_open,
            )
            ws_holder[0] = ws
            connected_at = time.monotonic()
            ws.run_forever()
            if stop.is_set():
                break
            # HTTPは通るのにWebSocketだけ張れない状態（kabuステーションの起動途中など）
            # では resync_session() が成功してしまい、5秒間隔の空回りになる。
            # すぐ落ちた接続は失敗とみなして待ち時間を伸ばす。
            if time.monotonic() - connected_at < 30:
                wait = min(wait * 2, RECONNECT_MAX_WAIT)
            else:
                wait = RECONNECT_MIN_WAIT
            if outage["since"] is None:
                log.warning("WebSocketが切断されました。%d秒後に繋ぎ直します", wait)
            stop.wait(wait)

    def request_stop(signum, _frame):
        log.warning("停止信号を受け取りました（signal=%s）。終了します", signum)
        stop.set()
        try:
            if ws_holder[0] is not None:
                ws_holder[0].close()
        except Exception:
            pass

    ws_holder = [None]
    signal.signal(signal.SIGINT, request_stop)
    if hasattr(signal, "SIGBREAK"):      # Windows の Ctrl+Break
        signal.signal(signal.SIGBREAK, request_stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, request_stop)

    threading.Thread(target=receive_loop, daemon=True, name="ws-receive").start()
    try:
        while not stop.is_set():
            # 停止要求ファイル（コントロールパネルからの停止手段）。
            # Windowsでは、窓なしで起動した子プロセスは親と別のコンソールを持つため
            # Ctrl+Break（コンソール制御イベント）が届かない。そこでファイル経由にする。
            if _stop_requested():
                try:
                    os.remove(STOP_FILE)
                except OSError:
                    pass
                request_stop("stopfile", None)
                break
            stop.wait(0.5)               # 短く待ち直して停止要求を取りこぼさない
    except KeyboardInterrupt:
        stop.set()

    # 処理待ちの発注を投げ捨てないよう、自動売買スレッドを先に止める
    if engine.autotrade_pump is not None:
        ticks, sigs = engine.autotrade_pump.pending()
        if sigs:
            log.warning("自動売買の未処理シグナルが%d件あります。処理してから終了します", sigs)
        engine.autotrade_pump.stop()

    # 古い口座情報をコントロールパネルが表示し続けないよう消しておく。
    # 銘柄登録は解除しない（他のツールが同じ登録を使うため）。
    account_snapshot.clear()
    log.info("ランナーを終了しました")


if __name__ == "__main__":
    main()
