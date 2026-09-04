"""有償ストックオプション（業績条件付）×事前下落 モニタ。

背景（analysis/output/2026-09-05/new_signal_paid_so_2026-09-05.md）:
  2018-2026年のTDnet開示89件をYahoo Finance日足で検証した結果、
    - 業績条件付き有償SO ＋ 発表前6ヶ月で20%以上下落 → 発表後6ヶ月 勝率69.2%・平均+26.2%（n=13）
    - 業績条件なしの有償SOは、同じ下落フィルタをかけても一貫してマイナス（n=33で中央値-11.8%）
  という方向性が出た。ただしn=13程度でまだ統計的有意ではなく、外れ値の分散も大きい。
  実弾はもちろん仮想売買にも進めず、**まず候補を見つけたら通知するだけ**の位置づけ。

処理の流れ（1日1回の実行を想定。日次でなくても良いが開示は不定期のため取りこぼし防止に日次推奨）:
  1. TDnet非公式ミラーAPIで、前回チェック日〜本日の新規開示から
     「有償ストックオプションの発行に関するお知らせ」（初回付与の公告）を抽出
  2. 「業績」を含むもの（業績条件付き）だけを候補にする（config で無効化可）
  3. 発表前6ヶ月の株価（Yahoo Finance日足）から下落率を計算
  4. しきい値（既定20%以上下落）を満たせば通知。満たさなくてもログには残す

  状態は state/seen.json に永続化し、同じ開示は二度評価しない。
  検知・通知のみで発注は一切行わない。詳細は ../README.md を参照。

実行:
  cd strategies/paid_so_monitor/src
  python main.py --config ../config.yaml
"""
import argparse
import json
import logging
import os
import time
import urllib.request
from datetime import datetime, timedelta, timezone

import yaml

from tdnet_client import collect_events

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.normpath(os.path.join(BASE_DIR, "..", "logs"))
STATE_DIR = os.path.normpath(os.path.join(BASE_DIR, "..", "state"))
STATE_PATH = os.path.join(STATE_DIR, "seen.json")
JST = timezone(timedelta(hours=9))

try:
    from plyer import notification as _plyer_notification
except ImportError:
    _plyer_notification = None


def setup_logging():
    os.makedirs(LOG_DIR, exist_ok=True)
    path = os.path.join(LOG_DIR, f"paid_so_monitor_{datetime.now(JST):%Y-%m-%d}.log")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.FileHandler(path, encoding="utf-8"), logging.StreamHandler()],
    )


def load_config(path):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"checked": [], "last_checked": None}


def save_state(state):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def fetch_daily_bars(code4, start_date, timeout=20):
    """Yahoo Financeから日足を取得（analysis/ と同じ方式）。[(date, open, close)] を古い順で返す。"""
    p1 = int(datetime.combine(start_date, datetime.min.time()).replace(tzinfo=JST).timestamp())
    p2 = int(datetime.now(JST).timestamp()) + 86400
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{code4}.T"
           f"?period1={p1}&period2={p2}&interval=1d")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        j = json.load(resp)
    r = j["chart"]["result"][0]
    q = r["indicators"]["quote"][0]
    out = []
    for ts, o, c in zip(r["timestamp"], q["open"], q["close"]):
        if o is None or c is None:
            continue
        d = datetime.fromtimestamp(ts, JST).date()
        out.append((d.isoformat(), o, c))
    return out


def price_on_or_before(bars, d):
    best = None
    for dd, o, c in bars:
        if dd <= d:
            best = c
        else:
            break
    return best


def build_message(ev, pre_decline_pct):
    title = f"[有償SO/業績条件付・事前下落{abs(pre_decline_pct):.0f}%] {ev['code4']} {ev['name']}"
    body = (f"{ev['code4']} {ev['name']}: {ev['date']} に「{ev['title']}」を開示。"
            f"発表前6ヶ月で{pre_decline_pct:+.1f}%（下落）。"
            f"過去の検証(n=13)では発表後6ヶ月の勝率69%・平均+26%だが、"
            f"サンプルが少なく統計的有意ではない。判断は必ず自分で行うこと。")
    return title, body


def notify(log, title, body):
    log.info("%s %s", title, body)
    if _plyer_notification is not None:
        try:
            _plyer_notification.notify(title=title, message=body, timeout=15)
        except Exception:
            log.exception("ポップアップ通知の送信に失敗しました")


def run_once(config_path: str, log=None, notify_fn=None, days: int = None) -> list:
    """TDnetチェック〜下落率判定〜通知を1回実行する。

    統合ランナー（strategies/runner）から呼びたくなった場合に備え、CLIから
    切り出してある（現時点では単独バッチとしてのみ使う想定。日足取得を伴うため
    PUSH受信スレッドとは別プロセスで動かすこと）。
    log: 使用するロガー（未指定なら本ツール専用のロガー）
    notify_fn: 通知関数 fn(title, body)。未指定なら本ツールのログ＋ポップアップ通知
    戻り値: 通知した候補のリスト
    """
    log = log or logging.getLogger("paid_so_monitor")
    config = load_config(config_path)
    td = config.get("tdnet", {})
    sc = config.get("screen", {})

    state = load_state()
    checked = set(state.get("checked", []))

    today = datetime.now(JST).date()
    lookback = days if days is not None else td.get("lookback_days", 30)
    start = today - timedelta(days=lookback)
    if state.get("last_checked") and days is None:
        last = datetime.fromisoformat(state["last_checked"]).date()
        start = min(start, last + timedelta(days=1))
    log.info("TDnetチェック: %s 〜 %s", start, today)

    events = collect_events(
        start, today,
        require_keywords=tuple(sc.get("require_keywords", ["有償", "ストックオプション"])),
        require_title_contains=sc.get("require_title_contains", "発行に関するお知らせ"),
        exclude_words=tuple(sc.get("exclude_words", [])),
        chunk_days=td.get("chunk_days", 7),
        request_interval=td.get("request_interval", 0.15),
        log=log,
    )
    log.info("初回発行公告（有償ストックオプション） %d件", len(events))

    perf_kw = sc.get("performance_keyword", "業績")
    require_perf = sc.get("require_performance_condition", True)
    decline_days = sc.get("decline_lookback_days", 182)
    min_decline = sc.get("min_decline_pct", 20.0)

    alerts = []
    for ev in events:
        key = f"{ev['date']}:{ev['code4']}"
        if key in checked:
            continue
        checked.add(key)

        has_perf = perf_kw in ev["title"]
        if require_perf and not has_perf:
            log.info("見送り（業績条件なし）: %s %s %s", ev["date"], ev["code4"], ev["name"])
            continue

        ev_date = datetime.strptime(ev["date"], "%Y-%m-%d").date()
        base_start = ev_date - timedelta(days=decline_days + 30)
        try:
            bars = fetch_daily_bars(ev["code4"], base_start)
        except Exception as e:
            log.warning("日足取得に失敗 %s %s: %s", ev["code4"], ev["name"], e)
            continue
        p_now = price_on_or_before(bars, ev["date"])
        p_before = price_on_or_before(
            bars, (ev_date - timedelta(days=decline_days)).isoformat())
        if p_now is None or p_before is None or p_before <= 0:
            log.warning("株価不足でスキップ: %s %s", ev["code4"], ev["name"])
            continue
        pre_decline_pct = (p_now / p_before - 1) * 100

        log.info("評価: %s %s %s 業績条件=%s 発表前%dヶ月=%+.1f%%",
                 ev["date"], ev["code4"], ev["name"], has_perf,
                 decline_days // 30, pre_decline_pct)

        if pre_decline_pct <= -min_decline:
            title, body = build_message(ev, pre_decline_pct)
            if notify_fn is not None:
                notify_fn(title, body)
            else:
                notify(log, title, body)
            alerts.append({**ev, "pre_decline_pct": pre_decline_pct})
        time.sleep(0.3)

    state["checked"] = sorted(checked)
    state["last_checked"] = today.isoformat()
    save_state(state)
    log.info("完了: 新規候補 %d件 / 評価済み開示 累計%d件", len(alerts), len(state["checked"]))
    return alerts


def main():
    ap = argparse.ArgumentParser(description="有償ストックオプション（業績条件付）×事前下落 モニタ")
    ap.add_argument("--config", default="../config.yaml")
    ap.add_argument("--days", type=int, default=None,
                    help="遡ってチェックする日数（既定はconfigのlookback_days／前回実行日から）")
    args = ap.parse_args()

    setup_logging()
    run_once(args.config, log=logging.getLogger("paid_so_monitor"), days=args.days)


if __name__ == "__main__":
    main()
