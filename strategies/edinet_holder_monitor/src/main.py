"""大量保有報告書モニタ: 大口保有者の売却進捗を追い、売り切りタイミングを通知する。

処理の流れ（1日1回の実行を想定）:
  1. EDINET API v2 で、前回チェック日〜本日の提出書類からウォッチリスト銘柄の
     大量保有報告書・変更報告書を抽出
  2. 各書類から「保有株券等の数」「株券等保有割合」を取得し、保有者ごとの状態を更新
  3. 報告日以降の日足（出来高）を取得し、平常出来高を超えた分＝売り玉の消化と仮定して
     残存保有株の消化状況を推定（absorption.py）
  4. 比率低下 / 5%接近 / 5%割れ / 消化50%・80% / 売り切り推定 / 売り切り接近 を通知

  状態は state/holders.json に永続化し、同じ通知は二度出さない。
  検知・通知のみで発注は行わない。詳細は ../README.md を参照。

実行:
  $env:EDINET_API_KEY = "取得したAPIキー"
  cd strategies/edinet_holder_monitor/src
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

import absorption as absorb
from edinet_client import EdinetClient, EdinetCodeMap, extract_holding, dump_item_names

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.normpath(os.path.join(BASE_DIR, "..", "logs"))
STATE_DIR = os.path.normpath(os.path.join(BASE_DIR, "..", "state"))
STATE_PATH = os.path.join(STATE_DIR, "holders.json")
JST = timezone(timedelta(hours=9))

try:
    from plyer import notification as _plyer_notification
except ImportError:
    _plyer_notification = None


def setup_logging():
    os.makedirs(LOG_DIR, exist_ok=True)
    path = os.path.join(LOG_DIR, f"edinet_holder_{datetime.now(JST):%Y-%m-%d}.log")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.FileHandler(path, encoding="utf-8"), logging.StreamHandler()],
    )


def load_config(path):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_symbols(config, config_path):
    def code(item):
        return str(item.get("symbol")) if isinstance(item, dict) else str(item)
    if config.get("symbols"):
        return [code(s) for s in config["symbols"]]
    base = os.path.dirname(os.path.abspath(config_path))
    path = os.path.normpath(os.path.join(base, config["symbols_file"]))
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return [code(s) for s in (data or {}).get("symbols", [])]


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"holders": {}, "fired": [], "last_checked": None}


def save_state(state):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def fetch_daily_bars(symbol, start_date, timeout=20):
    """Yahoo Financeから日足を取得（analysis/ と同じ方式）。[(date, close, volume)] を古い順で返す。"""
    p1 = int(datetime.combine(start_date, datetime.min.time()).replace(tzinfo=JST).timestamp())
    p2 = int(datetime.now(JST).timestamp()) + 86400
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}.T"
           f"?period1={p1}&period2={p2}&interval=1d")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        j = json.load(resp)
    r = j["chart"]["result"][0]
    q = r["indicators"]["quote"][0]
    out = []
    for ts, c, v in zip(r["timestamp"], q["close"], q["volume"]):
        d = datetime.fromtimestamp(ts, JST).date()
        out.append((d.isoformat(), c, v))
    return out


def build_message(a):
    """通知の (タイトル, 本文) を組み立てる。"""
    sym, filer = a["symbol"], a["filer"]
    ratio = a.get("ratio")
    rtxt = f"{ratio:.2f}%" if isinstance(ratio, (int, float)) else "不明"
    kind = a["kind"]
    if kind == "RATIO_DOWN":
        title = f"[大量保有/比率低下] {sym}"
        body = (f"{sym}: {filer} の保有割合が {a['ratio_prev']:.2f}% → {rtxt} に低下"
                f"（残 {a.get('shares') or 0:,.0f}株・報告日 {a['report_date']}）。売却進行中")
    elif kind == "NEAR_5PCT":
        title = f"[大量保有/5%接近] {sym}"
        body = (f"{sym}: {filer} の保有割合が {rtxt}（残 {a.get('shares') or 0:,.0f}株）。"
                f"5%を割ると報告義務が消滅＝売り切りが近い可能性")
    elif kind == "BELOW_5PCT":
        title = f"[大量保有/5%割れ] {sym}"
        body = (f"{sym}: {filer} の保有割合が {rtxt} となり報告義務が消滅。"
                f"大口の売却が完了した可能性が高く、需給改善が見込まれる")
    elif kind == "ABSORB_PROGRESS":
        title = f"[大量保有/消化{a.get('tier', 0)*100:.0f}%到達] {sym}"
        body = (f"{sym}: {filer} の残存保有株の推定消化率 {a['progress']*100:.0f}%"
                f"（推定残 {a['remaining']:,.0f}株 / 報告時 {a.get('shares') or 0:,.0f}株）")
    elif kind == "ABSORBED_DONE":
        title = f"[大量保有/売り切り推定] {sym}"
        body = (f"{sym}: {filer} の保有株（報告時 {a.get('shares') or 0:,.0f}株）が"
                f"超過出来高で消化され尽くしたと推定（消化率{a['progress']*100:.0f}%）。"
                f"売り圧力の解消＝需給改善の可能性（※推定であり確定情報ではない）")
    elif kind == "ETA_SOON":
        title = f"[大量保有/売り切り接近] {sym}"
        body = (f"{sym}: {filer} の推定残 {a['remaining']:,.0f}株、直近ペース"
                f"{a['pace']:,.0f}株/日 → あと約{a['days_left']}営業日で売り切り見込み"
                f"（消化率{a['progress']*100:.0f}%）")
    else:
        title = f"[大量保有] {sym}"
        body = str(a)
    return title, body


def notify(log, alert):
    title, body = build_message(alert)
    log.info("%s %s", title, body)
    if _plyer_notification is not None:
        try:
            _plyer_notification.notify(title=title, message=body, timeout=10)
        except Exception:
            log.exception("ポップアップ通知の送信に失敗しました")


def run_once(config_path: str, log=None, notify_fn=None, days: int = None,
             debug_dump: bool = False) -> list:
    """EDINETチェック〜消化推定〜通知を1回実行する。

    統合ランナー（strategies/runner）からも呼べるようにCLIから切り出したもの。
    log: 使用するロガー（未指定なら本ツール専用のロガー）
    notify_fn: 通知関数 fn(title, body)。未指定なら本ツールのログ＋ポップアップ通知
    戻り値: 発火したアラートのリスト
    """
    log = log or logging.getLogger("edinet_holder")

    class _Args:
        pass
    args = _Args()
    args.config = config_path
    args.days = days
    args.debug_dump = debug_dump

    config = load_config(args.config)
    symbols = set(load_symbols(config, args.config))
    ed = config.get("edinet", {})
    ab = config.get("absorption", {})
    nt = config.get("notify", {})

    client = EdinetClient(api_key=os.environ.get("EDINET_API_KEY"))
    # 大量保有報告書の提出者は保有者側のため secCode は空。issuerEdinetCode を
    # 証券コードに変換するための対応表を用意する（週1回キャッシュ更新）。
    code_map = EdinetCodeMap(cache_path=os.path.join(STATE_DIR, "edinet_codes.json"))
    code_map.load()
    log.info("EDINETコード対応表: %d件", len(code_map.map))
    state = load_state()
    fired = set(state.get("fired", []))

    # チェック対象日の決定
    today = datetime.now(JST).date()
    lookback = args.days if args.days is not None else ed.get("lookback_days", 7)
    start = today - timedelta(days=lookback)
    if state.get("last_checked") and args.days is None:
        last = datetime.fromisoformat(state["last_checked"]).date()
        start = min(start, last + timedelta(days=1))
    log.info("EDINET書類チェック: %s 〜 %s（監視 %d銘柄）", start, today, len(symbols))

    doc_codes = tuple(str(c) for c in ed.get("doc_type_codes", ["350", "360"]))
    keywords = tuple(ed.get("keywords", ["大量保有", "変更報告書"]))

    # 1) EDINETから該当書類を収集
    found = []
    d = start
    while d <= today:
        try:
            results = client.list_documents(d.isoformat())
        except Exception as e:
            log.warning("%s の書類一覧取得に失敗: %s", d, e)
            d += timedelta(days=1)
            time.sleep(ed.get("request_interval", 0.5))
            continue
        picked = EdinetClient.filter_large_holding(results, symbols, code_map, doc_codes, keywords)
        if picked:
            log.info("%s: 大量保有関連 %d件", d, len(picked))
        found.extend([{**p, "submit_date": d.isoformat()} for p in picked])
        d += timedelta(days=1)
        time.sleep(ed.get("request_interval", 0.5))

    if args.debug_dump:
        if not found:
            log.info("対象期間に該当書類がありませんでした。--days を増やして再実行してください。")
            return []
        doc = found[0]
        log.info("[DEBUG] %s %s %s", doc["symbol"], doc.get("filerName"), doc.get("docDescription"))
        rows = client.fetch_document_csv(doc["docID"])
        for item in dump_item_names(rows):
            log.info("  %s | %s | %s", item["要素ID"], item["項目名"], item["値"])
        return []

    # 2) 書類を解析して保有者の状態を更新
    for doc in found:
        try:
            rows = client.fetch_document_csv(doc["docID"])
        except Exception as e:
            log.warning("書類取得に失敗 %s: %s", doc.get("docID"), e)
            continue
        info = extract_holding(rows)
        if info["shares"] is None and info["ratio"] is None:
            log.warning("保有株数・保有割合を抽出できませんでした（%s %s）。"
                        "--debug-dump で項目名を確認してください", doc["symbol"], doc.get("docID"))
            continue
        filer = info.get("filer") or doc.get("filerName") or "不明"
        # 報告義務発生日（書類内の値）を優先。無ければ提出日で代用する。
        report_date = info.get("report_date") or doc["submit_date"]
        key = f"{doc['symbol']}:{filer}"
        prev = state["holders"].get(key, {})
        # 同じ保有者の古い報告書で新しい状態を上書きしない
        if prev.get("report_date") and report_date < prev["report_date"]:
            log.info("スキップ（より新しい報告書が既にある）: %s %s %s",
                     doc["symbol"], filer, report_date)
            continue
        state["holders"][key] = {
            "symbol": doc["symbol"], "filer": filer,
            "shares": info["shares"], "ratio": info["ratio"],
            "ratio_prev": info["ratio_prev"] if info["ratio_prev"] is not None else prev.get("ratio"),
            "outstanding": info.get("outstanding"), "purpose": info.get("purpose"),
            "report_date": report_date, "doc_id": doc["docID"],
            "doc_description": doc.get("docDescription"),
            "is_final": (info["ratio"] is not None and info["ratio"] < 5.0),
        }
        log.info("更新: %s %s 保有割合=%s%% 保有株数=%s 報告義務発生日=%s (%s)",
                 doc["symbol"], filer,
                 f"{info['ratio']:.2f}" if info["ratio"] is not None else "—",
                 f"{info['shares']:,.0f}" if info["shares"] is not None else "—",
                 report_date, doc.get("docDescription"))
        time.sleep(ed.get("request_interval", 0.5))

    # 3) 保有者ごとに消化状況を推定して通知判定
    #    同一銘柄に売り手が複数いる場合、超過出来高を各自に丸ごと割り当てると
    #    二重計上になるため、既定では有効な保有者数で按分する。
    max_age = ab.get("max_report_age_days", 90)
    active = {}
    for key, holder in state["holders"].items():
        if not holder.get("shares"):
            continue
        try:
            rd = datetime.fromisoformat(holder["report_date"]).date()
        except (ValueError, TypeError):
            continue
        if (today - rd).days > max_age:
            log.info("スキップ（報告日が%d日より古い）: %s %s %s",
                     max_age, holder["symbol"], holder["filer"], holder["report_date"])
            continue
        active[key] = holder
    holders_per_symbol = {}
    for h in active.values():
        holders_per_symbol[h["symbol"]] = holders_per_symbol.get(h["symbol"], 0) + 1

    alerts = []
    for key, holder in active.items():
        report_date = datetime.fromisoformat(holder["report_date"]).date()
        base_start = report_date - timedelta(days=ab.get("baseline_lookback_days", 180))
        try:
            bars = fetch_daily_bars(holder["symbol"], base_start)
        except Exception as e:
            log.warning("日足取得に失敗 %s: %s", holder["symbol"], e)
            continue
        before = [v for (dt, c, v) in bars if dt < holder["report_date"]]
        after = [(dt, c, v) for (dt, c, v) in bars if dt > holder["report_date"]]
        baseline = absorb.median_baseline(before)
        share = ab.get("seller_share", 1.0)
        n_sellers = holders_per_symbol.get(holder["symbol"], 1)
        if ab.get("split_among_holders", True) and n_sellers > 1:
            share = share / n_sellers   # 同一銘柄の複数の売り手で按分（二重計上の抑制）
        res = absorb.compute_absorption(
            holder["shares"], after, baseline,
            seller_share=share,
            down_day_weight=ab.get("down_day_weight", 1.0),
            up_day_weight=ab.get("up_day_weight", 1.0),
            pace_window=ab.get("pace_window", 10),
        )
        eta = res["days_left"] if res["days_left"] is not None else "—"
        split = f" / 売り手{n_sellers}名で按分" if n_sellers > 1 else ""
        log.info("%s %s: 消化 %.0f%% (推定残 %s株 / 平常出来高 %s株 / ペース %s株/日 / 残り%s営業日%s)",
                 holder["symbol"], holder["filer"], res["progress"] * 100,
                 f"{res['remaining']:,.0f}", f"{baseline:,.0f}", f"{res['pace']:,.0f}", eta, split)
        alerts.extend(absorb.evaluate_alerts(
            holder, res, fired,
            progress_tiers=nt.get("progress_tiers", [0.5, 0.8, 1.0]),
            near_5pct_threshold=nt.get("near_5pct_threshold", 6.0),
            projection_notice_days=nt.get("projection_notice_days", 5),
        ))
        time.sleep(0.3)

    for a in alerts:
        if notify_fn is not None:
            title, body = build_message(a)
            notify_fn(title, body)
        else:
            notify(log, a)
        fired.add(a["key"])

    state["fired"] = sorted(fired)
    state["last_checked"] = today.isoformat()
    save_state(state)
    log.info("完了: 新規通知 %d件 / 追跡中の保有者 %d件", len(alerts), len(state["holders"]))
    return alerts


def main():
    ap = argparse.ArgumentParser(description="EDINET大量保有報告書モニタ")
    ap.add_argument("--config", default="../config.yaml")
    ap.add_argument("--days", type=int, default=None,
                    help="遡ってチェックする日数（既定はconfigのlookback_days／前回実行日から）")
    ap.add_argument("--debug-dump", action="store_true",
                    help="最初に見つかった書類の項目名一覧を出力して終了（項目名の確認用）")
    args = ap.parse_args()

    setup_logging()
    run_once(args.config, log=logging.getLogger("edinet_holder"),
             days=args.days, debug_dump=args.debug_dump)


if __name__ == "__main__":
    main()
