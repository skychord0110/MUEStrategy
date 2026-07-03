"""ログ出力とデスクトップのポップアップ通知を行う。"""
import logging

logger = logging.getLogger("small_lot_detector")

try:
    from plyer import notification as _plyer_notification
except ImportError:
    _plyer_notification = None


def setup_logging(log_file: str = "small_lot_detector.log"):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.FileHandler(log_file, encoding="utf-8"), logging.StreamHandler()],
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
