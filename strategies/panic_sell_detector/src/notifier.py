"""ログ出力とデスクトップのポップアップ通知を行う。

ログは logs/ ディレクトリに日付ごとのファイル（panic_sell_YYYY-MM-DD.log）で保存する。
"""
import logging
import os
from datetime import datetime

logger = logging.getLogger("panic_sell_detector")

# ログ保存先: このファイルの場所を基準にした ../logs/（カレントディレクトリに依存しない）
LOG_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "logs"))
LOG_PREFIX = "panic_sell"

try:
    from plyer import notification as _plyer_notification
except ImportError:
    _plyer_notification = None

STAGE_LABELS = {
    "ABSORBED": "投げ売り吸収",
    "DUMP": "買い気配へぶつけ",
}


class DailyFileHandler(logging.FileHandler):
    """日付入りファイル名（<prefix>_YYYY-MM-DD.log）に書き、日付が変わったら自動で切り替える。"""

    def __init__(self, log_dir: str, prefix: str):
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
            self.current_date = date
            self.close()
            self.baseFilename = os.path.abspath(self._path())
            self.stream = None  # 次のemit時にFileHandlerが新ファイルを開く
        super().emit(record)


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[DailyFileHandler(LOG_DIR, LOG_PREFIX), logging.StreamHandler()],
    )


def notify(alert: dict):
    stage = alert["stage"]
    qty_removed = int(alert["qty_removed"])
    matched = int(alert["matched_qty"])

    if stage == "ABSORBED":
        message = (
            f"{alert['symbol']}: OVERから消えた{qty_removed}株がほぼ同数、売り気配周辺に"
            f"指し直され、うち{matched}株が買われています（吸収進行中・現在値{alert['price']}円）"
        )
    else:  # DUMP
        message = (
            f"{alert['symbol']}: OVERから消えた{qty_removed}株とほぼ同数({matched}株)が"
            f"買い気配にぶつけられました（投げ売り・現在値{alert['price']}円）"
        )

    logger.info("[%s] %s", STAGE_LABELS.get(stage, stage), message)

    if _plyer_notification is None:
        logger.warning("plyerが未インストールのためポップアップ通知はスキップします（pip install plyer）")
        return

    try:
        _plyer_notification.notify(
            title=f"投げ売り検知 [{STAGE_LABELS.get(stage, stage)}] {alert['symbol']}",
            message=message,
            timeout=10,
        )
    except Exception:
        logger.exception("ポップアップ通知の送信に失敗しました")
