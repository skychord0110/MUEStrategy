"""統合ランナーのログ出力とデスクトップのポップアップ通知。

どのストラテジーの通知かをラベルで区別して1系統にまとめる。
ログは runner/logs/ ディレクトリに日付ごとのファイル（runner_YYYY-MM-DD.log）で保存する。
"""
import logging
import os
from datetime import datetime

logger = logging.getLogger("runner")

# ログ保存先: このファイルの場所を基準にした runner/logs/（カレントディレクトリに依存しない）
LOG_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "logs"))


class DailyFileHandler(logging.FileHandler):
    """日付入りファイル名（runner_YYYY-MM-DD.log）に書き、日付が変わったら自動で切り替える。"""

    def __init__(self, log_dir: str, prefix: str = "runner"):
        self.log_dir = log_dir
        self.prefix = prefix
        self.current_date = datetime.now().strftime("%Y-%m-%d")
        os.makedirs(log_dir, exist_ok=True)
        super().__init__(self._path(), encoding="utf-8")

    def _path(self) -> str:
        return os.path.join(self.log_dir, f"{self.prefix}_{self.current_date}.log")

    def emit(self, record):
        date = datetime.now().strftime("%Y-%m-%d")
        if date != self.current_date:
            # 日付が変わった: 現在のファイルを閉じ、新しい日付のファイルに切り替える
            self.current_date = date
            self.close()
            self.baseFilename = os.path.abspath(self._path())
            self.stream = None  # 次のemit時にFileHandlerが新ファイルを開く
        super().emit(record)

try:
    from plyer import notification as _plyer_notification
except ImportError:
    _plyer_notification = None

STRATEGY_LABELS = {
    "small_lot_sell_detector": "小口売り連続",
    "panic_sell_detector": "投げ売り",
    "under_surge_detector": "UNDER急増",
}

PANIC_STAGE_LABELS = {
    "ABSORBED": "投げ売り吸収",
    "DUMP": "買い気配へぶつけ",
}


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[DailyFileHandler(LOG_DIR), logging.StreamHandler()],
    )


def build_message(strategy: str, alert: dict):
    """(通知タイトル, 本文) を組み立てる。"""
    label = STRATEGY_LABELS.get(strategy, strategy)

    if strategy == "small_lot_sell_detector":
        title = f"[{label}/{alert['tier']}] {alert['symbol']}"
        body = (
            f"{alert['symbol']}: 買い気配{alert['buy_price']}円に小口売り{alert['streak']}回連続 "
            f"(直近の推定約定株数 {alert['last_volume_delta']}株)"
        )
    elif strategy == "panic_sell_detector":
        stage = PANIC_STAGE_LABELS.get(alert["stage"], alert["stage"])
        title = f"[{label}/{stage}] {alert['symbol']}"
        qty_removed = int(alert["qty_removed"])
        matched = int(alert["matched_qty"])
        if alert["stage"] == "ABSORBED":
            body = (
                f"{alert['symbol']}: OVERから消えた{qty_removed}株がほぼ同数、売り気配周辺に"
                f"指し直され、うち{matched}株が買われています（吸収進行中・現在値{alert['price']}円）"
            )
        else:  # DUMP
            body = (
                f"{alert['symbol']}: OVERから消えた{qty_removed}株とほぼ同数({matched}株)が"
                f"買い気配にぶつけられました（投げ売り・現在値{alert['price']}円）"
            )
    elif strategy == "under_surge_detector":
        title = f"[{label}] {alert['symbol']}"
        body = (
            f"{alert['symbol']}: UNDERが{int(alert['prev_under'])}株→{int(alert['under'])}株に急増 "
            f"(+{int(alert['under_delta'])}株, +{alert['increase_pct']:.1f}%)。OVERはほぼ不変。"
            f"下値に大口買いが入った可能性（現在値{alert['price']}円・安値圏）"
        )
    else:
        title = f"[{label}] {alert.get('symbol', '?')}"
        body = str(alert)

    return title, body


def notify(strategy: str, alert: dict):
    title, body = build_message(strategy, alert)
    logger.info("%s %s", title, body)

    if _plyer_notification is None:
        logger.warning("plyerが未インストールのためポップアップ通知はスキップします（pip install plyer）")
        return

    try:
        _plyer_notification.notify(title=title, message=body, timeout=10)
    except Exception:
        logger.exception("ポップアップ通知の送信に失敗しました")
