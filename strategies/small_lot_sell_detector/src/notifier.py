"""ログ出力とデスクトップのポップアップ通知を行う。

ログは logs/ ディレクトリに日付ごとのファイル（small_lot_YYYY-MM-DD.log）で保存する。
"""
import logging
import os
from datetime import datetime

logger = logging.getLogger("small_lot_detector")

# ログ保存先: このファイルの場所を基準にした ../logs/（カレントディレクトリに依存しない）
LOG_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "logs"))
LOG_PREFIX = "small_lot"

try:
    from plyer import notification as _plyer_notification
except ImportError:
    _plyer_notification = None


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
    message = (
        f"{alert['symbol']}: 買い気配{alert['buy_price']}円に小口売り{alert['streak']}回連続 "
        f"(直近の推定約定株数 {alert['last_volume_delta']}株)"
    )
    logger.info("[%s] %s", alert["tier"], message)

    if _plyer_notification is None:
        logger.warning("plyerが未インストールのためポップアップ通知はスキップします（pip install plyer）")
        return

    try:
        _plyer_notification.notify(
            title=f"小口売り連続検知 [{alert['tier']}] {alert['symbol']}",
            message=message,
            timeout=10,
        )
    except Exception:
        logger.exception("ポップアップ通知の送信に失敗しました")
