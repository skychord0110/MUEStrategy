"""定期買い集め検知（z値方式）— kabuステーションAPIの歩み値をポーリングして判定する。

「約定のちょうど10秒後に買い」が、他のラグ（6〜14秒）と比べて統計的に突出している
銘柄を通知する。詳細な根拠は ../README.md と src/zscore_detector.py を参照。

旧 periodic_buy_rss との違い:
  - 生のペア数ではなく **z値** で判定するため、流動性の違いに左右されない
  - 歩み値APIは毎回その日の全件を返すので **差分管理（重複除去）が不要**
  - Excel・マーケットスピードIIが不要

統合ランナーから自動起動される（kabuクライアントを受け取る必要があるため、
単体実行はしない設計）。
"""
import logging
import os
import time
from datetime import datetime, time as dtime

import yaml

from zscore_detector import ZScoreBuyDetector

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def load_config(path):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_symbols(config, config_path):
    def code(item):
        return (str(item.get("symbol")), int(item.get("exchange", 1))) \
            if isinstance(item, dict) else (str(item), 1)
    if config.get("symbols"):
        return [code(s) for s in config["symbols"]]
    base = os.path.dirname(os.path.abspath(config_path))
    path = os.path.normpath(os.path.join(base, config.get("symbols_file", "../symbols.yaml")))
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return [code(s) for s in (data or {}).get("symbols", [])]


def parse_hhmm(s):
    if not s:
        return None
    h, m = str(s).split(":")[:2]
    return dtime(int(h), int(m))


def build_message(a: dict):
    """通知の (タイトル, 本文) を組み立てる。"""
    title = f"[定期買い集め/{a['tier']}] {a['symbol']}"
    lot = f"{a['dominant_lot']:.0f}株" if a.get("dominant_lot") else "—"
    body = (
        f"{a['symbol']}: 約定の{a['delay']:.0f}秒後の買いが統計的に突出しています"
        f"（z値 {a['zscore']:.1f} / 該当{a['pairs']}件 / 当日約定{a['trades']}件）。"
        f"10秒後の約定は{lot}が{a['dominant_lot_ratio']*100:.0f}%を占め、"
        f"うち{a['buy_ratio']*100:.0f}%が買い上がりまたは同値。"
        f"一定ラグで反応する買い集めアルゴの可能性（大量保有報告に向けた仕込みの疑い）"
    )
    return title, body


def fetch_today(client, symbol, exchange, today_iso, log):
    """当日ぶんの歩み値を [(datetime, volume, price)] で返す。"""
    try:
        d = client.get_time_and_sales(symbol, exchange)
    except Exception as e:
        msg = str(e)
        if "429" in msg:
            log.debug("レート制限 %s", symbol)
        else:
            log.warning("歩み値の取得に失敗 %s: %s", symbol, msg[:70])
        return None
    out = []
    for r in d.get("TradingPrice") or []:
        t = r.get("Time")
        p = r.get("Price")
        if not t or p is None or str(t)[:10] != today_iso:
            continue
        try:
            out.append((datetime.fromisoformat(str(t)), r.get("Volume"), p))
        except ValueError:
            continue
    out.sort(key=lambda x: x[0])
    return out


def run_loop(config_path, log=None, notify_fn=None, stop_event=None, kabu_client=None):
    """ポーリングループ。統合ランナーから別スレッドで呼ばれる。"""
    log = log or logging.getLogger("periodic_buy_zscore")
    config = load_config(config_path)
    if kabu_client is None:
        log.error("kabuクライアントが渡されていないため起動できません")
        return

    symbols = load_symbols(config, config_path)
    d = config.get("detector", {})
    det = ZScoreBuyDetector(
        delay_seconds=d.get("delay_seconds", 10.0),
        lag_min=d.get("lag_min", 6), lag_max=d.get("lag_max", 14),
        z_threshold=d.get("z_threshold", 5.0),
        strong_z_threshold=d.get("strong_z_threshold", 10.0),
        min_trades=d.get("min_trades", 50), min_pairs=d.get("min_pairs", 20),
        min_buy_ratio=d.get("min_buy_ratio", 0.0))

    poll = float(config.get("poll_interval_seconds", 1.0))
    exchange = int(config.get("exchange", 1))
    ss = parse_hhmm(config.get("session_start", "09:00"))
    se = parse_hhmm(config.get("session_end", "15:30"))
    idle = float(config.get("idle_poll_seconds", 30))
    summary_iv = float(config.get("summary_interval_minutes", 30)) * 60

    log.info("定期買い集め検知(z値方式) 起動。監視%d銘柄 / ラグ%.0f秒 / "
             "しきい値 z>=%.1f(WATCH) z>=%.1f(STRONG) / 1周およそ%.0f秒",
             len(symbols), det.delay_seconds, det.z_threshold,
             det.strong_z_threshold, len(symbols) * poll)

    def stopped():
        return stop_event is not None and stop_event.is_set()

    def in_session(t):
        return not ((ss and t < ss) or (se and t > se))

    def wait(sec):
        if stop_event is not None:
            stop_event.wait(sec)
        else:
            time.sleep(sec)

    def log_summary():
        rows = det.ranking()
        if not rows:
            log.info("[集計] 判定できる銘柄がまだありません")
            return
        s = "  ".join(f"{sym}:z{z:+.1f}({a.target_pairs}件)" for z, sym, a in rows[:8])
        log.info("[集計] z値の高い順 %s", s)

    was_in = None
    last_summary = time.time()
    while not stopped():
        now = datetime.now().astimezone()
        if not in_session(now.time()):
            if was_in is not False:
                log.info("時間外のため検知を停止します（対象 %s〜%s）",
                         config.get("session_start"), config.get("session_end"))
                if was_in:
                    log_summary()
                was_in = False
            wait(idle)
            continue
        if was_in is not True:
            log.info("場中に入りました。検知を開始します")
            was_in = True

        if summary_iv > 0 and time.time() - last_summary >= summary_iv:
            log_summary()
            last_summary = time.time()

        today_iso = now.date().isoformat()
        for sym, exch in symbols:
            if stopped():
                break
            if not in_session(datetime.now().astimezone().time()):
                break
            t0 = time.time()
            trades = fetch_today(kabu_client, sym, exch or exchange, today_iso, log)
            if trades:
                try:
                    for a in det.update(sym, trades, now.date()):
                        title, body = build_message(a)
                        if notify_fn:
                            notify_fn(title, body)
                        else:
                            log.info("%s %s", title, body)
                except Exception:
                    log.exception("判定でエラー %s", sym)
            wait(max(0.0, poll - (time.time() - t0)))
