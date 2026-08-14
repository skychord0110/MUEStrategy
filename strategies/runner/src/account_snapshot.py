"""口座状態（買付余力・建玉・評価損益）を定期的にJSONへ書き出す。

コントロールパネル（ui/control_panel.pyw）がこのファイルを読んで表示する。

【なぜUIから直接APIを叩かないのか】
kabuステーションAPIのトークンは、公式リファレンス POST /token の説明どおり
「別のトークンが新たに発行された時」に無効になる。
つまりコントロールパネルが自分で /token を発行すると、稼働中のランナーが持っている
トークンがその瞬間に失効し、参照系だけでなく **自動売買の発注・取消まで失敗する**。

そのため参照系APIを呼ぶのはトークンを持つランナー1プロセスだけに限定し、
UI側はこのスナップショットファイルを読むだけにしている。
（ランナー停止中はスナップショットが更新されないので、UIは「—」を表示する）

出力先: strategies/runner/state/account.json （gitignore対象）
"""
import json
import os
import threading
import time
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.normpath(os.path.join(BASE_DIR, "..", "state"))
PATH = os.path.join(STATE_DIR, "account.json")

# 買付余力として使うフィールド。autotrade/src/account.py と揃えること
# （StockAccountWallet は合計。三菱UFJ eスマート証券ぶんだけを見る）
BUYING_POWER_FIELD = "AuKCStockAccountWallet"

DEFAULT_INTERVAL = 15.0


def _num(v):
    """APIの数値フィールドを float にする。null や空文字は None。"""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def build(client, product: str = None) -> dict:
    """買付余力と建玉を1回取得して、UIが表示しやすい形に整える。

    評価損益は /positions の追加情報（addinfo 既定 true）に含まれる
    ProfitLoss（評価損益額）・ProfitLossRate（評価損益率）をそのまま使う。
    """
    snap = {
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "buying_power": None,
        "positions": [],
        "total_pl": None,
        "total_cost": None,
        "total_pl_rate": None,
        "error": None,
    }

    wallet = client.get_wallet_cash()
    snap["buying_power"] = _num(wallet.get(BUYING_POWER_FIELD))

    positions = client.get_positions(product) or []
    total_pl = 0.0
    total_cost = 0.0
    have_pl = False
    for p in positions:
        qty = _num(p.get("LeavesQty")) or 0.0
        if qty <= 0:
            continue                      # 返済済みの建玉は表示しない
        price = _num(p.get("Price"))
        pl = _num(p.get("ProfitLoss"))
        snap["positions"].append({
            "symbol": str(p.get("Symbol") or ""),
            "name": p.get("SymbolName") or "",
            "qty": qty,
            "price": price,
            "current": _num(p.get("CurrentPrice")),
            "pl": pl,
            "pl_rate": _num(p.get("ProfitLossRate")),
            "side": str(p.get("Side") or ""),
        })
        if pl is not None:
            total_pl += pl
            have_pl = True
        if price is not None:
            total_cost += price * qty

    if have_pl:
        snap["total_pl"] = total_pl
        snap["total_cost"] = total_cost or None
        # 全体の損益率は「取得原価の合計」に対する比率。
        # 建玉ごとの ProfitLossRate を平均すると金額の大きさを無視してしまうため使わない。
        if total_cost:
            snap["total_pl_rate"] = total_pl / total_cost * 100.0
    return snap


def write(snap: dict) -> None:
    """途中まで書かれたファイルをUIが読まないよう、一時ファイル経由で置き換える。"""
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False)
    os.replace(tmp, PATH)


def clear() -> None:
    """ランナー終了時にスナップショットを消す（古い値をUIが表示し続けないように）。"""
    try:
        os.remove(PATH)
    except OSError:
        pass


def start(client, cfg: dict, log, product: str = "2") -> threading.Thread:
    """スナップショットの書き出しをバックグラウンドで開始する。

    参照系2本（/wallet/cash と /positions）を interval 秒ごとに呼ぶだけなので
    レート制限には十分な余裕がある。失敗してもランナー本体は止めない。
    """
    cfg = cfg or {}
    if cfg.get("enabled") is False:
        log.info("口座スナップショット: 無効")
        return None
    interval = float(cfg.get("interval_seconds", DEFAULT_INTERVAL))

    def loop():
        last_error = None
        skipped = 0
        while True:
            # 発注など他のリクエストが進行中なら、この回は飛ばす。
            # クライアントのロックで順番待ちはできるが、待ってから実行すると
            # 発注直後に余計なリクエストを重ねることになる。表示用の情報なので
            # 1回飛ばして次の周期に回すほうが安全（実測の500の再発防止）。
            if getattr(client, "busy", False):
                skipped += 1
                if skipped % 10 == 1:
                    log.info("口座スナップショット: 他のリクエスト中のため見送り（累計%d回）",
                             skipped)
                time.sleep(interval)
                continue
            try:
                write(build(client, product))
                last_error = None
            except Exception as e:
                msg = f"{type(e).__name__}: {e}"
                if msg != last_error:
                    # 同じ失敗が続く間はログを埋めない（1回だけ出す）
                    log.warning("口座スナップショットの更新に失敗: %s", msg)
                    last_error = msg
                try:
                    write({"updated_at": datetime.now().isoformat(timespec="seconds"),
                           "buying_power": None, "positions": [], "total_pl": None,
                           "total_cost": None, "total_pl_rate": None, "error": msg})
                except Exception:
                    pass
            time.sleep(interval)

    t = threading.Thread(target=loop, daemon=True, name="account-snapshot")
    t.start()
    log.info("口座スナップショット: %.0f秒ごとに %s へ書き出します", interval, PATH)
    return t
