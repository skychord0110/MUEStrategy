"""口座情報（買付余力・建玉・銘柄情報）の取得と、発注可能数量の算出。

【ステップ1: 発注は一切行わない】
本モジュールは参照系API（GET）のみを呼び、結果をログに出す。
/sendorder などの発注APIは呼ばない。

使い方（単体確認）:
    $env:KABU_API_PASSWORD = "..."
    cd strategies/autotrade/src
    python account.py --config ../config.yaml
"""
import argparse
import logging
import os
import sys
from decimal import Decimal

import yaml

# 既存の kabu クライアントを再利用する（認証・参照系を一元管理するため）
_RUNNER_SRC = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "runner", "src"))
if _RUNNER_SRC not in sys.path:
    sys.path.insert(0, _RUNNER_SRC)
from kabu_client import KabuClient  # noqa: E402

import sizing  # noqa: E402
import tick_size  # noqa: E402

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.normpath(os.path.join(BASE_DIR, "..", "logs"))

# 買付余力として使うフィールド。
# StockAccountWallet（合計）ではなく三菱UFJ eスマート証券ぶんを使う。
BUYING_POWER_FIELD = "AuKCStockAccountWallet"


def setup_logging():
    os.makedirs(LOG_DIR, exist_ok=True)
    from datetime import datetime
    path = os.path.join(LOG_DIR, f"autotrade_{datetime.now():%Y-%m-%d}.log")
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.FileHandler(path, encoding="utf-8"), logging.StreamHandler()])


def load_config(path):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_symbols(config, config_path):
    """監視銘柄。autotradeのconfigに無ければ共通の symbols.yaml を読む。"""
    def code(item):
        return (str(item.get("symbol")), int(item.get("exchange", 1))) \
            if isinstance(item, dict) else (str(item), 1)
    if config.get("symbols"):
        return [code(s) for s in config["symbols"]]
    base = os.path.dirname(os.path.abspath(config_path))
    path = config.get("symbols_file") or os.path.normpath(
        os.path.join(base, "..", "symbols.yaml"))
    if not os.path.isabs(path):
        path = os.path.normpath(os.path.join(base, path))
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return [code(s) for s in (data or {}).get("symbols", [])]


class AccountView:
    """買付余力・建玉・銘柄マスタをまとめて保持する。

    銘柄マスタ（売買単位・呼値グループ）は日中変わらないため起動時に一度だけ取得する。
    """

    def __init__(self, client: KabuClient, log=None):
        self.client = client
        self.log = log or logging.getLogger("autotrade")
        self.buying_power = None
        self.positions = []
        self.master = {}     # symbol -> {"trading_unit":int, "price_range_group":str, "name":str}

    def load_master(self, symbols: list):
        """銘柄情報（売買単位・呼値グループ）を取得してキャッシュする。

        注意: kabuステーションに銘柄登録されていない銘柄は /symbol が 400 を返す。
        その場合は --register で登録してから再実行する。
        """
        ok = 0
        failed = []
        for sym, exch in symbols:
            try:
                d = self.client.get_symbol(sym, exch)
            except Exception as e:
                failed.append(sym)
                self.log.debug("銘柄情報の取得に失敗 %s: %s", sym, e)
                continue
            self.master[sym] = {
                "name": d.get("SymbolName") or "",
                "trading_unit": int(d.get("TradingUnit") or 100),
                "price_range_group": str(d.get("PriceRangeGroup") or tick_size.DEFAULT_GROUP),
            }
            ok += 1
        self.log.info("銘柄マスタを取得: %d/%d件", ok, len(symbols))
        if failed:
            self.log.warning(
                "取得できなかった %d銘柄: %s", len(failed), " ".join(failed))
            self.log.warning(
                "  → kabuステーションに銘柄登録されていない可能性が高い"
                "（未登録だと /symbol・/board は400を返す）。"
                "--register を付けて再実行するか、runnerを一度起動して登録してください")
        # 呼値グループの内訳を出す（10003は0.1円刻みなど、執行に影響するため）
        groups = {}
        for m in self.master.values():
            groups[m["price_range_group"]] = groups.get(m["price_range_group"], 0) + 1
        self.log.info("  呼値グループ内訳: %s", groups)
        return self.master

    def refresh(self, product: str = None):
        """買付余力と建玉を取り直す。"""
        w = self.client.get_wallet_cash()
        self.buying_power = w.get(BUYING_POWER_FIELD)
        if self.buying_power is None:
            self.log.warning("%s が取得できませんでした。レスポンス: %s", BUYING_POWER_FIELD, w)
        self.positions = self.client.get_positions(product) or []
        held = sizing.count_open_positions(self.positions)
        self.log.info("[資金] 買付可能額(%s) %s円 / 保有建玉 %d件",
                      BUYING_POWER_FIELD,
                      f"{float(self.buying_power):,.0f}" if self.buying_power is not None else "—",
                      held)
        # 余力0は「本当に資金が無い」以外に、時間外や口座区分の取り違えでも起こるため
        # レスポンス全体を出して切り分けられるようにする
        if not self.buying_power:
            self.log.warning("買付余力が0です。/wallet/cash のレスポンス全体: %s", w)
            self.log.warning("  切り分けの観点: (1)入金額・拘束額 (2)実行時刻（時間外は0を返す場合がある）"
                             " (3)口座区分（信用なら /wallet/margin を見る必要がある）")
        else:
            for k, v in w.items():
                if k != BUYING_POWER_FIELD and v is not None:
                    self.log.info("       （参考: %s %s円）", k, f"{float(v):,.0f}")
        for p in self.positions:
            if float(p.get("LeavesQty") or 0) > 0:
                self.log.info("       建玉: %s %s %s株 @%s",
                              p.get("Symbol"), p.get("SymbolName") or "",
                              p.get("LeavesQty"), p.get("Price"))
        return self.buying_power, self.positions

    def plan_quantity(self, symbol: str, price, cfg_capital: dict):
        """指定銘柄について、現在値から発注可能数量を計算する（発注はしない）。"""
        m = self.master.get(str(symbol), {})
        unit = m.get("trading_unit", cfg_capital.get("lot_size", 100))
        held = sizing.count_open_positions(self.positions)
        r = sizing.calc_quantity(price, self.buying_power, cfg_capital,
                                 trading_unit=unit, open_positions=held)
        grp = m.get("price_range_group", tick_size.DEFAULT_GROUP)
        try:
            tick = tick_size.tick_size(price, grp) if price else None
        except tick_size.UnknownPriceRangeGroup as e:
            tick = None
            self.log.warning("%s: %s", symbol, e)
        return r, {"name": m.get("name", ""), "trading_unit": unit,
                   "price_range_group": grp, "tick": tick}


def main():
    ap = argparse.ArgumentParser(
        description="口座情報の確認と発注可能数量の算出（発注はしない）")
    ap.add_argument("--config", default="../config.yaml")
    ap.add_argument("--symbols", default=None,
                    help="確認する銘柄をカンマ区切りで指定（既定は監視銘柄すべて）")
    ap.add_argument("--register", action="store_true",
                    help="実行前にkabuステーションへ銘柄登録し直す（全解除→登録）。"
                         "未登録銘柄で /symbol・/board が400になる場合に使う。"
                         "※統合ランナーの稼働中に使うとPUSH配信が切り替わるので同時実行しないこと")
    args = ap.parse_args()

    config = load_config(args.config)
    setup_logging()
    log = logging.getLogger("autotrade")

    log.info("=" * 78)
    log.info("自動売買 ステップ1: 残高照会＋数量計算（発注は行いません）")
    log.info("  enabled=%s / dry_run=%s / 実売買対象=%s",
             config.get("enabled"), config.get("dry_run"),
             [k for k, v in (config.get("strategies") or {}).items() if v] or "（なし）")
    log.info("=" * 78)

    pw = os.environ.get("KABU_API_PASSWORD")
    if not pw:
        log.error("環境変数 KABU_API_PASSWORD が設定されていません。"
                  '設定方法: setx KABU_API_PASSWORD "本番用APIパスワード"')
        sys.exit(1)

    client = KabuClient(environment=config.get("environment", "production"), api_password=pw)
    client.authenticate()
    log.info("認証に成功しました（環境: %s, ポート: %s）", client.environment, client.port)

    symbols = load_symbols(config, args.config)
    if args.symbols:
        want = {s.strip() for s in args.symbols.split(",")}
        symbols = [(s, e) for s, e in symbols if s in want]
    log.info("対象銘柄: %d件", len(symbols))

    if args.register:
        log.warning("--register 指定: kabuステーションの銘柄登録を入れ替えます"
                    "（統合ランナーが稼働中の場合はPUSH配信に影響します）")
        client.unregister_all()
        reg = client.register_symbols([{"symbol": s, "exchange": e} for s, e in symbols])
        log.info("銘柄登録しました: %d件", len(reg.get("RegistList") or []))

    view = AccountView(client, log)
    view.load_master(symbols)
    cap = config.get("capital", {})
    product = "2" if cap.get("product") == "cash" else "3"
    view.refresh(product)

    # 現在値は本来PUSHで受け取るが、単体確認では板APIから1回だけ取得する
    log.info("-" * 78)
    log.info("[数量] 現在値ベースの発注可能数量（確認用に板APIから現在値を取得）")
    shown = 0
    for sym, exch in symbols:
        if shown >= 10:
            log.info("  …（以降省略。全件見るには --symbols で指定）")
            break
        try:
            b = client.get_board(sym, exch)
        except Exception as e:
            log.warning("  %s 板情報の取得に失敗: %s", sym, e)
            continue
        price = b.get("CurrentPrice")
        r, info = view.plan_quantity(sym, price, cap)
        tick = f"{info['tick']}円" if info["tick"] is not None else "—"
        if r.ok:
            log.info("  %s %-14s 現在値%s円 呼値%s 単元%s株 → %d株（%s円）[上限: %s]",
                     sym, info["name"][:14],
                     f"{price:,.0f}" if price else "—", tick, info["trading_unit"],
                     r.quantity, f"{r.amount:,.0f}", r.limited_by)
        else:
            log.info("  %s %-14s 現在値%s円 → 見送り: %s",
                     sym, info["name"][:14], f"{price:,.0f}" if price else "—", r.reason)
        shown += 1
    log.info("-" * 78)
    log.info("完了（発注APIは呼んでいません）")


if __name__ == "__main__":
    main()
